"""Backtest Engine。

时间语义：
- SIGNAL_TIMEFRAME：策略主周期；信号只在主周期 K 线收盘（BAR_CLOSE_TIME）后基于已完成 K 线计算。
- SIGNAL_TIMESTAMP = 信号 K 线 BAR_CLOSE_TIME。
- EXECUTION_TIMEFRAME = 5 分钟；FILL_TIMESTAMP = SIGNAL_TIMESTAMP 之后第一根可用 5 分钟 K 线的开盘时刻，
  成交中间价 = 该 5 分钟 K 线开盘价。
- 止损/止盈/移动止损在 5 分钟 K 线内检查。5 分钟 OHLC 无法确定盘中先后顺序，统一假设：
  同一根 5 分钟 K 线同时触及止损与止盈时按"先止损"处理（保守）；跳空穿越止损价时按开盘价成交。
  移动止损的最高价只使用此前已完成 5 分钟 K 线的最高价。
- BACKTEST_WINDOW：[start, end)。窗口开始前的数据仅用于指标预热，不产生交易。
- 窗口结束时仍持仓：按最后一根 5 分钟 K 线收盘价平仓（计入全部成本），原因"回测结束平仓"。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd

from ..strategy.schema import StrategySchema
from . import risk
from .execution import CostModel, fill, max_buy_qty
from .rhythm import Gate, Rhythm, daily_context, lookup
from .signals import build_frame, group_signal

ASSUMPTIONS = [
    "信号只使用已收盘的主周期K线（无未来数据）",
    "成交在信号后第一根可用5分钟K线开盘价（作为中间价代理）",
    "同一5分钟K线同时触及止损和止盈时按先止损处理（保守假设，非真实盘中顺序）",
    "跳空穿越止损价时按开盘价成交",
    "移动止损最高价仅使用此前已完成5分钟K线",
    "手续费每边计一次，滑点每笔成交计一次，点差每笔成交计半个价差，不重复扣费",
    "仅做多、1倍、无杠杆、不允许负现金",
    "窗口结束仍持仓时按最后收盘价平仓并计入成本",
    "聚合K线需至少90%的5分钟K线才视为完整，不完整K线不参与指标和信号",
    "信号后15分钟内无可用5分钟K线（数据缺口）时放弃该信号，不在远离信号的价格成交",
]


MAX_FILL_DELAY = np.timedelta64(15, "m")
MAX_FILL_DELAY_DAILY = np.timedelta64(6, "D")     # 美股日线：收盘出信号 → 下一交易日开盘成交（含长周末 / 节假日）


class Cancelled(Exception):
    pass


@dataclass
class BTConfig:
    start: datetime
    end: datetime
    initial_cash: float = 10_000.0
    max_alloc_pct: float = 100.0
    cost: CostModel = CostModel()
    reserve_pct: float = 0.5
    rhythm: Rhythm | None = None
    market: object = None          # aiquant.market.Market；None = 加密货币


ASSUMPTIONS_DAILY = [
    "美股日线：信号只使用已收盘的日线（美东 16:00，半日市 13:00 收盘），无未来数据",
    "成交在信号后下一交易日的开盘价（作为中间价代理）；休市日、节假日按交易日历顺延",
    "止损 / 止盈按日线最高价、最低价检查；同一天同时触及时按先止损处理（保守假设）",
    "跳空穿越止损价时按开盘价成交",
    "成本按美股：佣金 0，滑点与半个点差每笔成交计一次，卖出另计监管费",
    "仅做多、1倍、无杠杆、不允许负现金（杠杆来自杠杆 ETF 本身）",
    "窗口结束仍持仓时按最后收盘价平仓并计入成本",
]


def _naive(s: pd.Series) -> np.ndarray:
    return pd.to_datetime(s, utc=True).dt.tz_localize(None).to_numpy("datetime64[ns]")


def _naive64(t: pd.Timestamp) -> np.datetime64:
    t = pd.Timestamp(t)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    return np.datetime64(t.to_datetime64(), "ns")


def _iso(x) -> str:
    return pd.Timestamp(x).tz_localize("UTC").isoformat()


def _dd(eq: np.ndarray) -> float:
    if len(eq) == 0:
        return 0.0
    peak = np.maximum.accumulate(eq)
    return float(((eq - peak) / peak).min() * 100)


def _downsample(ts: np.ndarray, eq: np.ndarray, n: int = 1500) -> list[list]:
    if len(ts) == 0:
        return []
    k = max(1, len(ts) // n)
    idx = list(range(0, len(ts), k))
    if idx[-1] != len(ts) - 1:
        idx.append(len(ts) - 1)
    return [[_iso(ts[i]), round(float(eq[i]), 4)] for i in idx]


def run(df5: pd.DataFrame, s: StrategySchema, signal_tf: int, aux_tf: int | None, cfg: BTConfig,
        should_stop=None, df_signal: pd.DataFrame | None = None) -> dict:
    """df5：执行 K 线。加密货币为 5 分钟 K 线；美股为日线（带 close_time 列，成交在下一交易日开盘）。
    df_signal：信号品种的 K 线（美股可用另一只 ETF 出信号，如 QQQ → 交易 QLD）；None = 用执行品种本身。"""
    start, end = pd.Timestamp(cfg.start), pd.Timestamp(cfg.end)
    daily = "close_time" in df5.columns
    df5 = df5[df5["ts"] < end].reset_index(drop=True)
    sig_src = df5 if df_signal is None else df_signal[df_signal["ts"] < end].reset_index(drop=True)
    frame = build_frame(sig_src, s, signal_tf, aux_tf, data_end=end)
    bar = pd.Timedelta(days=1) if daily else pd.Timedelta(minutes=5)
    max_delay = MAX_FILL_DELAY_DAILY if daily else MAX_FILL_DELAY
    ent = group_signal(frame, s.entry)
    ex = group_signal(frame, s.exit)
    ct = _naive(frame["close_time"])

    ts = _naive(df5["ts"])
    o = df5["open"].to_numpy(float)
    h = df5["high"].to_numpy(float)
    l = df5["low"].to_numpy(float)
    c = df5["close"].to_numpy(float)
    n = len(df5)
    start64, end64 = _naive64(start), _naive64(end)
    i0 = int(np.searchsorted(ts, start64, "left"))
    if i0 >= n:
        raise ValueError("回测窗口内没有行情数据")

    # 主周期信号 → 执行 5 分钟 K 线索引
    events: dict[int, int] = {}
    in_win = (ct >= start64) & (ct < end64)
    fidx = np.searchsorted(ts, ct, "left")
    dropped_gap = 0
    for mi in np.nonzero(in_win)[0]:
        fi = int(fidx[mi])
        if fi < n:
            if ts[fi] - ct[mi] > max_delay:
                dropped_gap += 1  # 信号后行情缺口过长，不在远离信号的价格成交
                continue
            events[fi] = int(mi)  # 多根主K线落在同一执行K线时保留最新一根

    alloc = min(s.alloc_pct, cfg.max_alloc_pct)
    rh = cfg.rhythm if (cfg.rhythm is not None and cfg.rhythm.active) else None
    gate = Gate(rh, cfg.market) if rh else None
    if rh and (rh.regime_sma_days or rh.vol_target_pct):
        reg_ok, vmult = lookup(daily_context(df5, rh, cfg.market), frame["close_time"])
    else:
        reg_ok, vmult = np.ones(len(frame), bool), np.ones(len(frame))
    rhythm_skipped = 0
    cm = cfg.cost
    cash = cfg.initial_cash
    qty = 0.0
    entry_fill = None
    entry_main = -1
    entry_signal_ts = None
    peak = 0.0
    trades: list[dict] = []
    totals = {"fee": 0.0, "spread": 0.0, "slip": 0.0}
    eq = np.empty(n - i0)
    blocked = 0

    def do_sell(i, mid, reason, signal_ts):
        nonlocal cash, qty, entry_fill
        f = fill("sell", mid, qty, cm)
        rd = risk.check_sell(qty, f.qty)
        assert rd.ok, rd.reason
        cash += f.notional - f.fee
        totals["fee"] += f.fee; totals["spread"] += f.spread_cost; totals["slip"] += f.slippage_cost
        ef = entry_fill
        pnl = (f.notional - f.fee) - (ef["notional"] + ef["fee"])
        trades.append({
            "entry_signal_ts": ef["signal_ts"], "entry_fill_ts": ef["ts"], "entry_price": ef["price"],
            "exit_signal_ts": signal_ts, "exit_fill_ts": _iso(ts[i]) if reason != "回测结束平仓"
            else _iso(pd.Timestamp(ts[i]) + bar),
            "exit_price": f.price, "qty": f.qty,
            "fee": ef["fee"] + f.fee, "spread_cost": ef["spread"] + f.spread_cost,
            "slippage_cost": ef["slip"] + f.slippage_cost, "exit_reason": reason, "pnl": pnl,
            "equity_after": cash,
        })
        qty = 0.0
        entry_fill = None
        if gate is not None:
            gate.on_exit(pd.Timestamp(ts[i]).tz_localize("UTC"), reason, pnl)

    for k, i in enumerate(range(i0, n)):
        if should_stop is not None and k % 4000 == 0 and should_stop():
            raise Cancelled()
        mi = events.get(i)
        exited_this_bar = False
        if mi is not None:
            sig_ts = _iso(ct[mi])
            if qty > 0:
                reason = None
                if ex[mi]:
                    reason = "退出规则"
                elif s.max_hold_bars and (mi - entry_main) >= s.max_hold_bars:
                    reason = "最长持仓"
                if reason:
                    do_sell(i, o[i], reason, sig_ts)
                    exited_this_bar = True
            elif ent[mi] and not ex[mi] and gate is not None and \
                    gate.check(pd.Timestamp(ts[i]).tz_localize("UTC"), bool(reg_ok[mi])) is not None:
                rhythm_skipped += 1
            elif ent[mi] and not ex[mi]:
                q = max_buy_qty(cash, o[i], alloc * float(vmult[mi]), cm, cfg.reserve_pct)
                f = fill("buy", o[i], q, cm)
                rd = risk.check_buy(cash, f.notional, f.fee, alloc, max_alloc_pct=cfg.max_alloc_pct)
                if rd.ok:
                    cash -= f.notional + f.fee
                    qty = f.qty
                    totals["fee"] += f.fee; totals["spread"] += f.spread_cost; totals["slip"] += f.slippage_cost
                    entry_fill = {"ts": _iso(ts[i]), "signal_ts": sig_ts, "price": f.price,
                                  "notional": f.notional, "fee": f.fee, "spread": f.spread_cost,
                                  "slip": f.slippage_cost}
                    entry_main = mi
                    peak = f.price
                    if gate is not None:
                        gate.on_entry(pd.Timestamp(ts[i]).tz_localize("UTC"))
                else:
                    blocked += 1
        if qty > 0 and not exited_this_bar:
            ep = entry_fill["price"]
            stop_px = ep * (1 - s.stop_loss_pct / 100) if s.stop_loss_pct else -np.inf
            reason = "止损"
            if s.trailing and peak >= ep * (1 + s.trailing.activate_pct / 100):
                tpx = peak * (1 - s.trailing.trail_pct / 100)
                if tpx > stop_px:
                    stop_px, reason = tpx, "移动止损"
            tp_px = ep * (1 + s.take_profit_pct / 100) if s.take_profit_pct else np.inf
            if l[i] <= stop_px:
                do_sell(i, min(o[i], stop_px), reason, None)
            elif h[i] >= tp_px:
                do_sell(i, max(o[i], tp_px), "止盈", None)
            else:
                peak = max(peak, h[i])
        eq[k] = cash + qty * c[i]
        if cash < -1e-6:
            raise AssertionError("出现负现金，回测终止")

    if qty > 0:
        do_sell(n - 1, c[n - 1], "回测结束平仓", None)
        eq[-1] = cash

    ets = ts[i0:]
    tr = pd.DataFrame(trades)
    wins = tr[tr["pnl"] > 0] if len(tr) else tr
    losses = tr[tr["pnl"] <= 0] if len(tr) else tr
    gp = float(wins["pnl"].sum()) if len(tr) else 0.0
    gl = float(-losses["pnl"].sum()) if len(tr) else 0.0
    final = float(eq[-1])
    hold_bars = 0
    if len(tr):
        for t in trades:
            hold_bars += (pd.Timestamp(t["exit_fill_ts"]) - pd.Timestamp(t["entry_fill_ts"])) / bar
    metrics = {
        "initial_cash": cfg.initial_cash, "final_equity": final,
        "net_profit": final - cfg.initial_cash,
        "net_return_pct": (final / cfg.initial_cash - 1) * 100,
        "max_drawdown_pct": _dd(eq),
        "trades": len(trades), "win_rate_pct": (len(wins) / len(trades) * 100) if trades else None,
        "profit_factor": (gp / gl) if gl > 0 else (None if gp == 0 else float("inf")),
        "fees": totals["fee"], "spread_cost": totals["spread"], "slippage_cost": totals["slip"],
        "total_cost": totals["fee"] + totals["spread"] + totals["slip"],
        "exposure_pct": hold_bars / max(len(ets), 1) * 100,
        "avg_trade_pnl": (sum(t["pnl"] for t in trades) / len(trades)) if trades else None,
        "orders_blocked_by_risk": blocked,
        "signals_skipped_by_rhythm": rhythm_skipped,
        "signal_bars": int(in_win.sum()),
        "signals_dropped_data_gap": dropped_gap,
        "partial_bars_excluded": int(frame.attrs.get("partial_bars_excluded", 0)),
        "exec_bars": int(len(ets)),
    }
    assumptions = list(ASSUMPTIONS_DAILY if daily else ASSUMPTIONS)
    if rh:
        unit = getattr(cfg.market, "unit_cn", "小时") if cfg.market is not None else "小时"
        assumptions.append("交易节奏（与自动进化、模拟盘相同）：" + "；".join(rh.describe(unit)))
    return {"metrics": metrics, "trades": trades, "equity": _downsample(ets, eq), "assumptions": assumptions}


def buy_and_hold(df5: pd.DataFrame, cfg: BTConfig) -> dict:
    start, end = pd.Timestamp(cfg.start), pd.Timestamp(cfg.end)
    d = df5[(df5["ts"] >= start) & (df5["ts"] < end)].reset_index(drop=True)
    if d.empty:
        raise ValueError("基准区间无数据")
    cm = cfg.cost
    q = max_buy_qty(cfg.initial_cash, d["open"].iloc[0], 100.0, cm, cfg.reserve_pct)
    b = fill("buy", d["open"].iloc[0], q, cm)
    cash = cfg.initial_cash - b.notional - b.fee
    eq = cash + q * d["close"].to_numpy(float)
    sl = fill("sell", d["close"].iloc[-1], q, cm)
    final = cash + sl.notional - sl.fee
    eq[-1] = final
    raw = (d["close"].iloc[-1] / d["open"].iloc[0] - 1) * 100
    return {"metrics": {
        "initial_cash": cfg.initial_cash, "final_equity": final, "net_profit": final - cfg.initial_cash,
        "net_return_pct": (final / cfg.initial_cash - 1) * 100, "raw_price_change_pct": raw,
        "max_drawdown_pct": _dd(eq), "fees": b.fee + sl.fee, "spread_cost": b.spread_cost + sl.spread_cost,
        "slippage_cost": b.slippage_cost + sl.slippage_cost, "trades": 1,
        "entry_price": b.price, "exit_price": sl.price,
    }, "equity": _downsample(_naive(d["ts"]), eq)}
