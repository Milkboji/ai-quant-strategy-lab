"""「我的交易系统 v1」回测引擎：日线、只做多、现货，多品种组合，一个品种内分批建仓（每一批有独立的入场价、止损和状态）。

规则（大白话，参数见 Params；规则来源和未纳入的内容见 docs/my_system.md）：
1. 出手时机：过去 N 天是一个低波动的横盘区间（区间 = 这 N 天的最高价 / 最低价；横盘 = 20 日波动率低于过去一年的 30% 分位），
   日线收盘突破区间高点时买入。其他时候不开新仓。
2. 大势过滤：基准（加密货币用 BTC，美股用 SPY）日线收盘低于 200 日均线时不开新仓。
3. 首次建仓：额度的 first_pct，止损放在突破当天日线最低点；止损距离太大、亏到止损会超过额度的 max_loss_pct 时缩小仓位。
4. 加仓（最多 2 次）：突破后第一次回调、出现「横盘缩量」（连续 3 根日线振幅都小于 0.7 倍 ATR，且成交量低于 20 日均量；
   没有成交量时省略这一项）时加额度的 add_pct；加仓部分的止损放在回调低点；之前每一批的止损都已移到保本价才能加；
   总仓位不超过额度的 100%。加仓部分止损时，同时从底仓卖出与这笔亏损等额的金额。
5. 保本：某一批浮盈达到它自己的 1 倍止损距离后，这一批的止损移到成本价（从下一根日线起生效）。
6. 卖出：价格涨到首仓止损距离的 half_r 倍时卖出一半；之后回调到突破位附近（区间高点上下 2% 以内）且再次企稳（阳线收盘），
   带止损（回调低点）把这一半买回，只买回一次；价格进入过去 365 天最高点下方 near_high_pct 以内时卖出剩下的一半；
   第一次卖出之后，剩余仓位用移动止盈（最高价回撤 2 倍 ATR）。
7. 时间止损：持仓后连续 time_stop 根日线没有创新高（加密货币按天、美股按交易日）就全部离场。
8. 同时最多持有 max_positions 个品种。
执行：日线收盘出信号，下一根日线开盘成交；止损 / 止盈 / 移动止盈盘中触发按触发价成交，跳空越过时按开盘价成交；
同一天同时触及止损和目标时按先止损处理（保守）。成本按市场（与主引擎同一个 execution.fill）。
交易节奏：与主引擎同一个 Gate（冷却、连亏刹车），只管开新仓；大势过滤用本系统自己的 200 日均线。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace

import numpy as np
import pandas as pd

from .execution import CostModel, fill
from .rhythm import Gate, Rhythm

ATR_N = 20
BOX_ATR_MULT = 4.0          # 研究用：区间高度上限 = 4 × 20 日 ATR（只在 off 里关掉「box」之外的对照中使用，默认不启用，见 docs/my_system.md）
VOL_N = 20
VOL_PCTL = 0.30
QUIET_ATR = 0.7
QUIET_BARS = 3
REGIME_SMA = 200
TRAIL_ATR = 2.0
REBUY_BAND = 0.02
MAX_ADDS = 2


@dataclass(frozen=True)
class Params:
    box_days: int = 30            # 横盘天数（试 20 / 30 / 60）
    half_r: float = 3.0           # 卖一半的倍数（首仓止损距离的几倍；试 2 / 3 / 5）
    near_high_pct: float = 3.0    # 前高距离 %
    time_stop: int = 20           # 时间止损天数（试 10 / 20 / 40）
    first_pct: float = 20.0       # 首仓比例（额度的 %）
    add_pct: float = 25.0         # 加仓比例（额度的 %，规则给的范围 20~30）
    max_loss_pct: float = 3.0     # 单笔最大亏损（额度的 %）
    max_positions: int = 3        # 最多持有品种数

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Params":
        if not d:
            return cls()
        ok = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        cast = {k: (int(v) if isinstance(cls.__dataclass_fields__[k].default, int) else float(v)) for k, v in ok.items()}
        return cls(**cast)


PARAM_LABELS = {
    "box_days": ("横盘天数", "天", (5, 250)),
    "half_r": ("卖一半的倍数", "倍止损距离", (1, 20)),
    "near_high_pct": ("前高距离", "%", (0.5, 20)),
    "time_stop": ("时间止损天数", "天", (3, 120)),
    "first_pct": ("首仓比例", "% 额度", (5, 100)),
    "add_pct": ("加仓比例", "% 额度", (5, 50)),
    "max_loss_pct": ("单笔最大亏损", "% 额度", (0.5, 10)),
    "max_positions": ("最多持有品种数", "个", (1, 10)),
}


def describe(p: Params, market_key: str = "crypto", symbols: list[str] | None = None) -> list[str]:
    """用大白话复述每一条规则（回测结果、策略实验室模板、策略总台卡片共用）。"""
    unit = "天" if market_key == "crypto" else "个交易日"
    bench = "BTC" if market_key == "crypto" else "SPY"
    return [
        f"1. 只在「5% 时刻」出手：过去 {p.box_days} {unit}是一个低波动的横盘区间（20 日波动率低于过去一年的 30% 分位），"
        f"日线收盘突破这 {p.box_days} {unit}的最高价才买；其他时候不开新仓。",
        f"2. 大势过滤：{bench} 日线收盘低于 200 日均线时不开新仓（持仓照常按规则卖出）。",
        f"3. 首次建仓用额度的 {p.first_pct:g}%，止损放在突破当天日线最低点；如果亏到止损会超过额度的 {p.max_loss_pct:g}%，"
        "自动缩小仓位。",
        f"4. 加仓最多 2 次，每次额度的 {p.add_pct:g}%：突破后第一次回调、连续 3 根日线振幅都小于 0.7 倍 ATR 且缩量时加，"
        "止损放在回调低点；之前每一批的止损都移到保本价后才能加下一批；总仓位不超过额度的 100%；加仓那批止损时，"
        "同时从底仓卖出与这笔亏损等额的金额。",
        "5. 保本：某一批赚到它自己 1 倍止损距离后，这一批的止损移到成本价。",
        f"6. 卖出：涨到首仓止损距离的 {p.half_r:g} 倍卖一半；回调到突破位附近（±2%）再次企稳时带止损买回这一半（只买回一次）；"
        f"涨到过去 365 天最高点下方 {p.near_high_pct:g}% 以内卖剩下的一半；第一次卖出后，剩余仓位用移动止盈"
        "（从最高价回撤 2 倍 ATR）。",
        f"7. 时间止损：持仓后连续 {p.time_stop} {unit}没有创新高，全部卖出。",
        f"8. 同时最多持有 {p.max_positions} 个品种" + (f"（候选：{'、'.join(symbols)}）" if symbols else "") + "。",
        "执行：日线收盘出信号，下一根日线开盘成交；止损和止盈盘中触发按触发价成交，跳空越过按开盘价成交；"
        "同一天同时触及止损和目标按先止损处理。",
    ]


# ---------------------------------------------------------------------------
# 指标（只用当天及以前的日线）
# ---------------------------------------------------------------------------

def indicators(d: pd.DataFrame, p: Params, year_bars: int) -> pd.DataFrame:
    """d：日线 ts / open / high / low / close / volume（按时间排序）。返回加了指标列的副本，所有列都只用到当天为止的数据。"""
    x = d.reset_index(drop=True).copy()
    pc = x["close"].shift(1)
    tr = pd.concat([x["high"] - x["low"], (x["high"] - pc).abs(), (x["low"] - pc).abs()], axis=1).max(axis=1)
    x["atr"] = tr.rolling(ATR_N).mean()
    lr = np.log(x["close"] / pc)
    x["vol20"] = lr.rolling(VOL_N).std() * np.sqrt(year_bars)
    x["vol_q"] = x["vol20"].rolling(year_bars, min_periods=int(year_bars * 0.8)).quantile(VOL_PCTL)
    n = int(p.box_days)
    x["box_hi"] = x["high"].shift(1).rolling(n).max()
    x["box_lo"] = x["low"].shift(1).rolling(n).min()
    x["box_ok"] = (x["box_hi"] - x["box_lo"]) <= BOX_ATR_MULT * x["atr"].shift(1)
    x["quiet_vol"] = x["vol20"].shift(1) < x["vol_q"].shift(1)
    x["breakout"] = (x["close"] > x["box_hi"]) & x["quiet_vol"]
    x["hi365"] = x["high"].rolling(year_bars, min_periods=20).max()
    rng = x["high"] - x["low"]
    small = rng < QUIET_ATR * x["atr"]
    has_vol = "volume" in x and x["volume"].fillna(0).sum() > 0
    if has_vol:
        small = small & (x["volume"] < x["volume"].rolling(20).mean())
    x["quiet3"] = small.rolling(QUIET_BARS).sum() >= QUIET_BARS
    return x


def regime_series(bench: pd.DataFrame) -> pd.Series:
    """基准日线收盘 > 200 日均线（按日期索引；只用当天及以前）。"""
    b = bench.reset_index(drop=True)
    ok = b["close"] > b["close"].rolling(REGIME_SMA).mean()
    return pd.Series(ok.values, index=pd.to_datetime(b["ts"], utc=True))


# ---------------------------------------------------------------------------
# 分批仓位
# ---------------------------------------------------------------------------

@dataclass
class Tranche:
    kind: str              # 首仓 / 加仓1 / 加仓2 / 买回
    qty: float
    entry: float           # 成交价（含成本）
    stop: float
    r: float               # 自己的 1 倍止损距离
    opened: str
    breakeven: bool = False
    pending_be: bool = False


@dataclass
class Pos:
    symbol: str
    tranches: list = field(default_factory=list)
    r0: float = 0.0        # 首仓止损距离
    base_entry: float = 0.0
    box_hi: float = 0.0
    hi_since: float = 0.0  # 持仓以来最高价
    last_new_high_i: int = 0
    prior_high: float = 0.0
    half_sold: bool = False
    rebought: bool = False
    second_half_sold: bool = False
    adds: int = 0
    pulled_back: bool = False
    pull_low: float = float("inf")
    trailing: bool = False
    entry_i: int = 0
    entry_ts: object = None
    half_usd: float = 0.0

    @property
    def qty(self) -> float:
        return sum(t.qty for t in self.tranches)


def _fill_px(side: str, mid: float, qty: float, cm: CostModel, at) -> object:
    return fill(side, mid, qty, cm, at if cm.spread_hourly is not None else None)


def run(bars: dict[str, pd.DataFrame], bench: pd.DataFrame, p: Params, *, cost: CostModel, start, end,
        initial_cash: float = 10_000.0, market_key: str = "crypto", rhythm: Rhythm | None = None, market=None,
        off: frozenset = frozenset(), live: bool = False) -> dict:
    """bars：{品种: 日线（ts=当天开始 UTC, open, high, low, close, volume）}。bench：基准日线（BTC / SPY）。
    off：研究用，关掉某条规则看它有没有用（regime 大势过滤 / quiet 低波动 / adds 加仓 / half 卖一半与买回 /
    nearhigh 前高卖出 / time 时间止损 / be 保本 / trail 移动止盈；box_atr 是反过来「加上」区间高度限制的对照）。模拟盘永远不传。
    live：模拟盘用。最后一根日线是今天还没走完的部分（开盘价 = 今天第一次看到的价格，最高 / 最低 = 到现在为止），
    只执行开盘成交和盘中止损 / 目标，不做收盘决定；窗口结束不平仓；返回各品种的分批状态（state）。
    返回 {metrics, trades（按批次的明细）, equity, yearly, assumptions, params}。"""
    year_bars = 365 if market_key == "crypto" else 252
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    ind = {s: indicators(d, p, year_bars) for s, d in bars.items() if d is not None and len(d)}
    if "quiet" in off:
        for x in ind.values():
            x["breakout"] = x["close"] > x["box_hi"]
    if "box_atr" in off:          # 研究对照：额外要求区间高度不超过 4 倍 ATR（不是规则的一部分）
        for x in ind.values():
            x["breakout"] = x["breakout"] & x["box_ok"]
    reg = regime_series(bench)
    dates = sorted(set().union(*[set(pd.to_datetime(x["ts"], utc=True)) for x in ind.values()]))
    dates = [t for t in dates if start <= t < end]
    rows = {s: {pd.Timestamp(t): i for i, t in enumerate(pd.to_datetime(x["ts"], utc=True))} for s, x in ind.items()}
    rh = replace(rhythm, regime_sma_days=0, vol_target_pct=0.0) if (rhythm is not None and rhythm.active) else None
    gate = Gate(rh, market) if rh is not None and rh.active else None

    cash = float(initial_cash)
    budget = float(initial_cash)
    pos: dict[str, Pos] = {}
    pending: list[dict] = []          # 明天开盘要执行的订单
    trades: list[dict] = []           # 每一批的明细
    events: list[dict] = []
    eq_curve = []
    fees = 0.0
    blocked = {"大势过滤": 0, "持仓已满": 0, "交易节奏": 0, "现金不足": 0}
    last_px: dict[str, float] = {}
    holds: list[float] = []

    def gone(s: str, ts, why: str) -> None:
        """该品种全部离场：记持仓天数和事件。"""
        ps = pos.pop(s)
        holds.append((pd.Timestamp(ts) - pd.Timestamp(ps.entry_ts)).total_seconds() / 86400)
        events.append({"ts": pd.Timestamp(ts).isoformat(), "symbol": s, "event": "全部离场", "why": why})

    def close_tranche(s: str, t: Tranche, qty: float, mid: float, ts, reason: str) -> float:
        nonlocal cash, fees
        qty = min(qty, t.qty)
        if qty <= 1e-12:
            return 0.0
        f = _fill_px("sell", mid, qty, cost, ts)
        cash += f.notional - f.fee
        fees += f.fee
        part = qty / t.qty
        pnl = (f.notional - f.fee) - t.entry * qty
        trades.append({"symbol": s, "tranche": t.kind, "entry_ts": t.opened, "entry_price": round(t.entry, 6),
                       "exit_ts": pd.Timestamp(ts).isoformat(), "exit_price": round(f.price, 6), "qty": qty,
                       "stop": round(t.stop, 6), "exit_reason": reason, "pnl": pnl, "fee": f.fee,
                       "partial": part < 0.999})
        t.qty -= qty
        return pnl

    def open_tranche(s: str, kind: str, mid: float, usd: float, stop: float, ts) -> Tranche | None:
        nonlocal cash, fees
        if stop >= mid or usd <= 0:
            return None
        usd = min(usd, cash * 0.995)
        if usd < 10:
            blocked["现金不足"] += 1
            return None
        unit = mid * (1 + (cost.spread_at(ts if cost.spread_hourly is not None else None) / 2 + cost.slippage_bps) / 1e4) \
            * (1 + cost.fee_bps / 1e4)
        q = usd / unit
        f = _fill_px("buy", mid, q, cost, ts)
        cash -= f.notional + f.fee
        fees += f.fee
        entry = (f.notional + f.fee) / q
        return Tranche(kind, q, entry, stop, max(entry - stop, entry * 1e-4), pd.Timestamp(ts).isoformat())

    def exit_all(s: str, mid: float, ts, reason: str) -> None:
        ps = pos[s]
        pnl = 0.0
        for t in ps.tranches:
            pnl += close_tranche(s, t, t.qty, mid, ts, reason)
        gone(s, ts, reason)
        if gate is not None:
            gate.on_exit(pd.Timestamp(ts), "止损" if "止损" in reason else reason, pnl)

    for di, today in enumerate(dates):
        # ---- 1) 开盘：执行昨天收盘决定的订单 ----
        todo, pending = pending, []
        for o in todo:
            s = o["symbol"]
            i = rows[s].get(today)
            if i is None:
                pending.append(o)          # 该品种今天没有日线（例如美股休市），顺延
                continue
            x = ind[s].iloc[i]
            op = float(x["open"])
            if o["kind"] == "entry":
                if s in pos or len(pos) >= p.max_positions:
                    blocked["持仓已满"] += 1
                    continue
                stop = o["stop"]
                if op <= stop:
                    continue
                risk_usd = budget * p.max_loss_pct / 100
                usd = min(budget * p.first_pct / 100, risk_usd / ((op - stop) / op))
                t = open_tranche(s, "首仓", op, usd, stop, today)
                if t is None:
                    continue
                pos[s] = Pos(s, [t], r0=t.r, base_entry=t.entry, box_hi=o["box_hi"], hi_since=float(x["high"]),
                             last_new_high_i=i, prior_high=o["prior_high"], entry_i=i, entry_ts=today)
                events.append({"ts": today.isoformat(), "symbol": s, "event": "首次建仓"})
                if gate is not None:
                    gate.on_entry(today)
            elif o["kind"] in ("add", "rebuy") and s in pos:
                ps = pos[s]
                stop = o["stop"]
                if op <= stop:
                    continue
                used = sum(t.qty * t.entry for t in ps.tranches)
                room = budget - used
                if o["kind"] == "add":
                    usd = min(budget * p.add_pct / 100, room, budget * p.max_loss_pct / 100 / ((op - stop) / op))
                    kind = f"加仓{ps.adds + 1}"
                else:
                    usd = min(o["usd"], room, budget * p.max_loss_pct / 100 / ((op - stop) / op))
                    kind = "买回"
                t = open_tranche(s, kind, op, usd, stop, today)
                if t is None:
                    continue
                ps.tranches.append(t)
                if o["kind"] == "add":
                    ps.adds += 1
                    ps.pulled_back, ps.pull_low = False, float("inf")
                else:
                    ps.rebought = True
                events.append({"ts": today.isoformat(), "symbol": s, "event": kind})
            elif o["kind"] == "exit_all" and s in pos:
                exit_all(s, op, today, o["reason"])
            elif o["kind"] == "sell_half_other" and s in pos:
                ps = pos[s]
                q = ps.qty / 2
                for t in sorted(ps.tranches, key=lambda t: t.entry, reverse=True):
                    take = min(q, t.qty)
                    close_tranche(s, t, take, op, today, "接近前高卖出另一半")
                    q -= take
                    if q <= 1e-12:
                        break
                ps.tranches = [t for t in ps.tranches if t.qty > 1e-12]
                ps.second_half_sold, ps.trailing = True, True
                if not ps.tranches:
                    gone(s, today, "接近前高卖出另一半")

        # ---- 2) 盘中：止损、目标（按每个品种当天日线） ----
        for s in list(pos):
            i = rows[s].get(today)
            if i is None:
                continue
            x = ind[s].iloc[i]
            op, hi, lo, cl, atr = (float(x[k]) for k in ("open", "high", "low", "close", "atr"))
            ps = pos[s]
            for t in ps.tranches:              # 昨天达到 1R 的批次，今天起止损在成本价
                if t.pending_be:
                    t.stop, t.breakeven, t.pending_be = max(t.stop, t.entry), True, False
            if ps.trailing and "trail" not in off:
                tr = ps.hi_since - TRAIL_ATR * atr if atr == atr else None
                if tr:
                    for t in ps.tranches:
                        t.stop = max(t.stop, tr)
            # 止损（先止损）
            hit = [t for t in ps.tranches if lo <= t.stop]
            for t in sorted(hit, key=lambda t: t.stop, reverse=True):
                px = min(op, t.stop)
                reason = "移动止盈" if ps.trailing and t.stop > t.entry else ("保本止损" if t.breakeven else "止损")
                loss = close_tranche(s, t, t.qty, px, today, f"{t.kind}{reason}")
                if t.kind.startswith("加仓") and loss < 0:
                    base = next((b for b in ps.tranches if b.kind == "首仓" and b.qty > 1e-12), None)
                    if base is not None:
                        close_tranche(s, base, min(base.qty, -loss / px), px, today, "加仓止损，底仓卖出等额亏损")
            ps.tranches = [t for t in ps.tranches if t.qty > 1e-12]
            if not ps.tranches:
                gone(s, today, "止损 / 移动止盈")
                if gate is not None:
                    gate.on_exit(today, "止损", -1.0)
                continue
            # 卖一半：涨到首仓止损距离的 half_r 倍
            tgt = ps.base_entry + p.half_r * ps.r0
            if not ps.half_sold and "half" not in off and hi >= tgt:
                px = max(op, tgt)
                q = ps.qty / 2
                for t in sorted(ps.tranches, key=lambda t: t.entry, reverse=True):
                    take = min(q, t.qty)
                    close_tranche(s, t, take, px, today, f"涨到 {p.half_r:g} 倍止损距离卖一半")
                    q -= take
                    if q <= 1e-12:
                        break
                ps.tranches = [t for t in ps.tranches if t.qty > 1e-12]
                ps.half_sold, ps.trailing = True, True
                ps.half_usd = _sold_usd(trades, s, "卖一半")
                events.append({"ts": today.isoformat(), "symbol": s, "event": "卖一半"})
            # 新高、保本
            if hi > ps.hi_since:
                ps.hi_since, ps.last_new_high_i = hi, i
                if ps.half_sold:
                    ps.pulled_back = False
            for t in ps.tranches:
                if not t.breakeven and "be" not in off and hi >= t.entry + t.r:
                    t.pending_be = True
            if not ps.tranches:
                gone(s, today, "卖出")
            last_px[s] = cl

        if live and di == len(dates) - 1:      # 今天还没收盘：不做收盘决定
            val = sum(ps.qty * last_px.get(s, ps.base_entry) for s, ps in pos.items())
            eq_curve.append((today, cash + val))
            break
        # ---- 3) 收盘：明天的决定 ----
        bench_ok = bool(reg.get(today, False)) if today in reg.index else _asof(reg, today)
        for s in list(pos):
            i = rows[s].get(today)
            if i is None:
                continue
            x = ind[s].iloc[i]
            ps = pos[s]
            hi, lo, cl, atr = (float(x[k]) for k in ("high", "low", "close", "atr"))
            if "time" not in off and i - ps.last_new_high_i >= p.time_stop:
                pending.append({"symbol": s, "kind": "exit_all", "reason": f"时间止损：{p.time_stop} 根日线没有创新高"})
                continue
            if ("nearhigh" not in off and not ps.second_half_sold and ps.prior_high > ps.base_entry * (1 + p.near_high_pct / 100)
                    and hi >= ps.prior_high * (1 - p.near_high_pct / 100)):
                pending.append({"symbol": s, "kind": "sell_half_other"})
                continue
            # 回调：创新高后的第一次收盘下跌
            if cl < hi and i > ps.entry_i and cl < float(ind[s].iloc[i - 1]["close"]):
                ps.pulled_back = True
            if ps.pulled_back:
                ps.pull_low = min(ps.pull_low, lo)
            # 加仓：回调后横盘缩量，之前每一批都已保本
            if ("adds" not in off and not ps.half_sold and ps.adds < MAX_ADDS and ps.pulled_back and bool(x["quiet3"])
                    and (all(t.breakeven for t in ps.tranches) or "be" in off)
                    and sum(t.qty * t.entry for t in ps.tranches) < budget * 0.999):
                pending.append({"symbol": s, "kind": "add", "stop": ps.pull_low})
            # 买回：卖一半之后回到突破位附近并企稳
            if ("half" not in off and ps.half_sold and not ps.rebought and not ps.second_half_sold
                    and lo <= ps.box_hi * (1 + REBUY_BAND) and cl >= ps.box_hi * (1 - REBUY_BAND)
                    and cl > float(x["open"])):
                pending.append({"symbol": s, "kind": "rebuy", "stop": lo, "usd": ps.half_usd})
        # 新开仓（按突破强度排序）
        cands = []
        for s, x_ in ind.items():
            if s in pos:
                continue
            i = rows[s].get(today)
            if i is None:
                continue
            x = x_.iloc[i]
            if bool(x["breakout"]):
                cands.append((float(x["close"]) / float(x["box_hi"]), s, x))
        for _, s, x in sorted(cands, key=lambda c: -c[0]):
            if not bench_ok and "regime" not in off:
                blocked["大势过滤"] += 1
                continue
            if len(pos) + sum(1 for o in pending if o["kind"] == "entry") >= p.max_positions:
                blocked["持仓已满"] += 1
                continue
            if gate is not None and gate.check(today + pd.Timedelta(days=1)) is not None:
                blocked["交易节奏"] += 1
                continue
            pending.append({"symbol": s, "kind": "entry", "stop": float(x["low"]), "box_hi": float(x["box_hi"]),
                            "prior_high": float(ind[s]["high"].iloc[max(0, rows[s][today] - year_bars):rows[s][today]].max()
                                                if rows[s][today] > 0 else x["high"])})
        # 权益（收盘）
        val = 0.0
        for s, ps in pos.items():
            i = rows[s].get(today)
            px = float(ind[s].iloc[i]["close"]) if i is not None else last_px.get(s, ps.base_entry)
            last_px[s] = px
            val += ps.qty * px
        eq_curve.append((today, cash + val))

    if live:
        rep = _report(eq_curve, trades, events, fees, initial_cash, year_bars, p, blocked, market_key, list(bars), holds)
        rep["state"] = {s: {"qty": ps.qty, "base_entry": ps.base_entry, "r0": ps.r0, "half_sold": ps.half_sold,
                            "trailing": ps.trailing, "hi_since": ps.hi_since,
                            "tranches": [{"kind": t.kind, "qty": t.qty, "entry": t.entry, "stop": t.stop,
                                          "breakeven": t.breakeven, "opened": t.opened} for t in ps.tranches]}
                        for s, ps in pos.items()}
        rep["pending"] = pending
        rep["cash"] = cash
        return rep
    # 窗口结束：按最后收盘价平仓
    if dates:
        for s in list(pos):
            exit_all(s, last_px.get(s, pos[s].base_entry), dates[-1], "回测结束平仓")
        eq_curve[-1] = (dates[-1], cash)
    return _report(eq_curve, trades, events, fees, initial_cash, year_bars, p, blocked, market_key, list(bars), holds)


def _asof(s: pd.Series, t) -> bool:
    x = s[s.index <= t]
    return bool(x.iloc[-1]) if len(x) else False


def _sold_usd(trades: list[dict], s: str, key: str) -> float:
    """最近一次「卖一半」卖出的金额（买回时按这个金额买）。"""
    out, seen = 0.0, False
    for t in reversed(trades):
        if t["symbol"] == s and key in t["exit_reason"]:
            out += t["exit_price"] * t["qty"]
            seen = True
        elif seen:
            break
    return out


def _report(eq_curve, trades, events, fees, initial_cash, year_bars, p, blocked, market_key, symbols, holds) -> dict:
    eq = pd.Series([e for _, e in eq_curve], index=[t for t, _ in eq_curve], dtype=float)
    if eq.empty:
        return {"metrics": {}, "trades": [], "equity": [], "yearly": [], "params": p.to_dict()}
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    final = float(eq.iloc[-1])
    cagr = ((final / initial_cash) ** (1 / years) - 1) * 100 if final > 0 else -100.0
    dd = float(((eq / eq.cummax()) - 1).min() * 100)
    pnl = sum(t["pnl"] for t in trades)
    gross = sum(abs(t["pnl"]) for t in trades) or 1.0
    rounds = [e for e in events if e["event"] == "首次建仓"]
    yearly = []
    for y, g in eq.groupby(eq.index.year):
        prev = eq[eq.index < g.index[0]]
        base = float(prev.iloc[-1]) if len(prev) else initial_cash
        yearly.append({"year": int(y), "ret": round((float(g.iloc[-1]) / base - 1) * 100, 2)})
    m = {
        "initial_cash": initial_cash, "final_equity": final, "net_return_pct": (final / initial_cash - 1) * 100,
        "cagr_pct": cagr, "max_drawdown_pct": dd, "ret_dd": (cagr / abs(dd)) if dd < 0 else None,
        "entries": len(rounds), "tranche_trades": len(trades), "fees": fees,
        "fee_share_pct": fees / (abs(pnl) + fees) * 100 if (abs(pnl) + fees) else 0.0,
        "avg_hold_days": float(np.mean(holds)) if holds else 0.0, "win_rate_pct": (
            sum(1 for t in trades if t["pnl"] > 0) / len(trades) * 100 if trades else None),
        "blocked": blocked, "exposure_note": "额度 = 初始资金；最多同时持有 " + str(p.max_positions) + " 个品种",
    }
    return {"metrics": m, "trades": trades, "events": events,
            "equity": [[t.isoformat(), round(v, 4)] for t, v in eq_curve], "yearly": yearly, "params": p.to_dict(),
            "assumptions": describe(p, market_key, symbols)}
