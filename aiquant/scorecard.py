"""策略成绩单：每天用同一份数据重新回测每个已部署版本的实际运行区间，逐笔对比模拟盘。

对比内容（每个部署的当前版本，从它开始运行的时刻算起）
- 信号：回测和模拟盘的成交逐笔配对（同方向、时间最近、在容差内）。
  一致 / 时间偏差 / 漏单（回测有、模拟盘没有）/ 多单（模拟盘有、回测没有）。
  漏单和多单按原因归类：服务中断导致（不算策略偏差）、暂停或额度为 0、资金调整（加仓 / 减仓 / 收回额度）、
  信号过期、交易节奏状态不同、现金不足、策略偏差。
- 成交价偏差：模拟盘成交价相对回测成交价（bps，正数 = 比回测差）。
- 成本偏差：实际成本（成交价相对中间价 + 手续费）vs 回测成本模型（半个点差 + 滑点 + 手续费），bps。
  BTC 中间价 = 成交所在 5 分钟 K 线开盘价；美股 = 下单时的参考价。加密货币卖出的手续费从美元里扣，订单上看不到，
  按费率估计（标注「估计」）。
- 累计收益：实际（模拟盘持仓的时间加权收益）vs 预期（同一区间的回测）vs 基准（交易品种买入持有）。
偏差超过阈值时发通知（每个偏差只通知一次），策略总台卡片显示原因分类。
样本太少（成交 < MIN_TRADES 笔或运行 < MIN_DAYS 天）时明确提示「样本不足，暂不能下结论」。

这是监控，不是研究：运行区间都在数据分区冻结之后（前向数据），不记录留出集触及，结果不参与进化。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from . import db

log = logging.getLogger("aiquant.scorecard")
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scorecards (
  id INTEGER PRIMARY KEY AUTOINCREMENT, deployment_id INTEGER NOT NULL, version_id INTEGER, day TEXT NOT NULL,
  created_at TEXT NOT NULL, data_json TEXT NOT NULL, UNIQUE(deployment_id, version_id, day));
"""
DEFAULTS = {
    "price_bps": {"crypto": 30.0, "us_equity": 25.0},   # 单笔成交价偏差阈值
    "cost_bps": 10.0,                                   # 平均实际成本比模型高出的阈值
    "return_pp": 2.0,                                   # 累计收益 实际 − 预期 的阈值（百分点）
    "time_min": {"crypto": 30, "us_equity": 1440},      # 配对后时间差超过它算「时间偏差」
    "min_trades": 5, "min_days": 20, "run_hour_utc": 22,
}
NON_STRATEGY = {"中断导致", "暂停或额度为 0", "资金调整", "继承持仓"}   # 不算策略偏差、不通知
RUN_KEY = "scorecard_last_run"
import threading as _th  # noqa: E402
_running = _th.Event()


def ensure_tables() -> None:
    with db.tx() as c:
        c.executescript(SCHEMA_SQL)


def get_config() -> dict:
    c = json.loads(json.dumps(DEFAULTS))
    c.update(db.get_setting("scorecard_config") or {})
    return c


# ---------------------------------------------------------------------------
# 运行区间
# ---------------------------------------------------------------------------

def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def segment_start(dep: dict) -> pd.Timestamp:
    """当前版本开始运行的时刻：切换时间（live_since）→ 审计里的部署记录 → 部署创建时间。ETF 择时用版本的部署时间。"""
    cfg = dep["config"]
    if dep["broker"] == "alpaca_us" and cfg.get("kind") == "etf_sma":
        r = db.q1("SELECT deployed_at FROM etf_versions WHERE id=?", (dep["version_id"],)) if _has("etf_versions") else None
        if r and r["deployed_at"]:
            return _ts(r["deployed_at"])
        if _has("etf_orders"):     # 登记前就在运行的参数（v1）：从 ETF 择时第一笔成交的那天算起
            o = db.q1("SELECT MIN(ts) AS t FROM etf_orders WHERE filled_qty>0")
            if o and o["t"]:
                return _ts(o["t"]).normalize()
    if cfg.get("live_since"):
        return _ts(cfg["live_since"])
    if _has("strategy_audit"):
        r = db.q1("SELECT ts FROM strategy_audit WHERE deployment_id=? AND to_version_id=? ORDER BY id DESC LIMIT 1",
                  (dep["id"], dep["version_id"]))
        if r:
            return _ts(r["ts"])
    return _ts(dep["created_at"])


def _has(table: str) -> bool:
    return bool(db.q1("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)))


def market_of(dep: dict):
    from . import market as mk
    return mk.US_EQUITY if dep["broker"] == "alpaca_us" else mk.CRYPTO


# ---------------------------------------------------------------------------
# 模拟盘成交
# ---------------------------------------------------------------------------

def paper_fills(dep: dict, start: pd.Timestamp) -> list[dict]:
    """[{ts, side, qty, price, key（信号时刻）, req_qty, purpose, ref_price}]"""
    out = []
    if dep["broker"] == "alpaca_us":
        if not _has("us_order_allocs"):
            return []
        from . import market as mk
        if dep["config"].get("kind") == "etf_sma" and _has("etf_orders"):   # 迁移前的 ETF 择时成交（旧执行器）
            moved = {r["client_order_id"] for r in db.q("SELECT client_order_id FROM us_orders")} if _has("us_orders") else set()
            for r in db.q("SELECT * FROM etf_orders WHERE filled_qty>0 ORDER BY id"):
                ts = _ts(r["ts"])
                if ts < start or r["client_order_id"] in moved:
                    continue
                day = ts.tz_convert(mk.ET).date()
                out.append({"ts": ts, "side": r["side"], "qty": float(r["filled_qty"]), "price": float(r["filled_price"]),
                            "key": _ts(mk.session_open(day)), "day": str(day), "purpose": "",
                            "ref_price": None, "req_qty": None})
        rows = db.q("SELECT a.*, o.updated_at AS o_upd, o.created_at AS o_created FROM us_order_allocs a "
                    "LEFT JOIN us_orders o ON o.id=a.order_id WHERE a.deployment_id=? AND a.filled_qty>0 ORDER BY a.id",
                    (dep["id"],))
        for r in rows:
            day = pd.Timestamp(r["trade_date"]).date() if r.get("trade_date") else None
            ts = _ts(r["o_created"] or r["created_at"])
            if ts < start:
                continue
            key = _ts(mk.session_open(day)) if day else ts
            out.append({"ts": ts, "side": r["side"], "qty": float(r["filled_qty"]), "price": float(r["filled_price"]),
                        "key": key, "day": str(day) if day else None, "purpose": r.get("purpose") or "",
                        "ref_price": float(r["ref_price"]) if r.get("ref_price") else None, "req_qty": None})
        return out
    rows = db.q("SELECT * FROM paper_orders WHERE deployment_id=? AND is_test=0 AND filled_qty>0 "
                "AND status IN ('filled','reconciled') ORDER BY id", (dep["id"],))
    for r in rows:
        ts = _ts(r["created_at"])
        if ts < start:
            continue
        out.append({"ts": ts, "side": r["side"], "qty": float(r["filled_qty"]), "price": float(r["filled_price"]),
                    "key": _ts(r["signal_ts"]) if r.get("signal_ts") else ts, "purpose": "",
                    "req_qty": float(r["qty"]) if r.get("qty") else None, "ref_price": None})
    return out


# ---------------------------------------------------------------------------
# 回测（同一份数据、同一区间、当前成本口径与交易节奏）
# ---------------------------------------------------------------------------

def _rebacktest(dep: dict, start: pd.Timestamp, now: pd.Timestamp) -> dict:
    """返回 {fills, curve: [(ts, 收益率%)], cost, prices: DataFrame[ts, open, close] 交易品种, symbol}。"""
    from . import market as mk
    from . import research
    from .engine import backtest as bt
    from .strategy import registry
    from .strategy.schema import validate
    cfg = dep["config"]
    if dep["broker"] == "alpaca_us" and cfg.get("kind") == "etf_sma":
        return _rebacktest_etf(dep, start, now)
    v = registry.get_version(dep["version_id"])
    obj, errs = validate(v["schema"])
    if errs:
        raise ValueError("版本校验失败：" + "；".join(errs))
    r = research.get_research()
    if dep["broker"] == "alpaca_us":
        from .data import us_daily
        m = mk.US_EQUITY
        sym = cfg.get("trade_symbol")
        sig = cfg.get("signal_symbol") or sym
        df = us_daily.to_bars(us_daily.load(sym))
        dsig = us_daily.to_bars(us_daily.load(sig)) if sig != sym else None
        if dsig is not None:
            dsig = dsig[dsig["close_time"] <= now].reset_index(drop=True)
        df = df[df["close_time"] <= now].reset_index(drop=True)
        tf, aux = 1440, None
        r = dict(r, symbol=sym)
    else:
        from .data import market_data as md
        m, sym, dsig = mk.CRYPTO, "BTC/USD", None
        df = md.load_master()
        df = df[(df["ts"] >= start - pd.Timedelta(days=420)) & (df["ts"] + pd.Timedelta(minutes=5) <= now)].reset_index(drop=True)
        tf, aux = int(cfg.get("signal_tf") or obj.signal_tf or 60), cfg.get("aux_tf") or obj.aux_tf
    end = now
    bcfg = research.bt_config(start.to_pydatetime(), end.to_pydatetime(), dict(r, initial_cash=10_000.0, max_alloc_pct=100.0), m)
    res = bt.run(df, obj, tf, aux, bcfg, df_signal=dsig)
    cm = bcfg.cost
    fills = []
    for n_, t in enumerate(res["trades"]):
        et = _ts(t["entry_fill_ts"])
        fills.append({"side": "buy", "ts": et, "key": _ts(t["entry_signal_ts"]) if t.get("entry_signal_ts") else et,
                      "price": float(t["entry_price"]), "mid": _mid(float(t["entry_price"]), "buy", cm, et),
                      "reason": "入场规则", "trade": n_})
        if t["exit_reason"] == "回测结束平仓":
            continue
        xt = _ts(t["exit_fill_ts"])
        fills.append({"side": "sell", "ts": xt, "key": _ts(t["exit_signal_ts"]) if t.get("exit_signal_ts") else xt,
                      "price": float(t["exit_price"]), "mid": _mid(float(t["exit_price"]), "sell", cm, xt),
                      "reason": t["exit_reason"], "trade": n_})
    curve = [(_ts(ts), (float(eq) / 10_000.0 - 1) * 100) for ts, eq in res["equity"]]
    px = df[df["ts"] >= start - pd.Timedelta(days=3)][["ts", "open", "close"]].reset_index(drop=True)
    return {"fills": fills, "curve": curve, "cost": cm, "prices": px, "symbol": sym, "market": m,
            "metrics": res["metrics"]}


def _mid(price: float, side: str, cm, at) -> float:
    half = cm.spread_at(at) / 2 / 1e4
    slip = cm.slippage_bps / 1e4
    return price / (1 + (1 if side == "buy" else -1) * (half + slip))


def _rebacktest_etf(dep: dict, start: pd.Timestamp, now: pd.Timestamp) -> dict:
    from . import challenger
    from . import market as mk
    from . import rhythm
    from . import spread_model
    from .engine import regime_bt
    from .paper import etf_trend
    c = etf_trend.get_config()
    sym = c.get("trade_symbol")
    df = challenger._etf_data()
    if df is None or not len(df):
        raise ValueError("没有 ETF 日线")
    ed = now.tz_convert(mk.ET).date()
    df = df[df.index.date <= ed]
    spread = spread_model.open_spread(sym)
    sim = regime_bt.simulate(df, int(c["sma"]), float(c["band"]), market=mk.US_EQUITY, rhythm=rhythm.current(mk.US_EQUITY),
                             spread_bps=spread)
    sd = start.tz_convert(mk.ET).date()
    sim = sim[sim.index.date >= sd]
    cm = mk.US_EQUITY.cost()
    sp = cm.spread_bps if spread is None else spread
    k = (sp / 2 + cm.slippage_bps) / 1e4
    fills, curve, eq = [], [], 1.0
    first = True
    for d, row in sim.iterrows():
        t = _ts(mk.session_open(d.date()))
        if first and float(row["size"]) > 0 and row["event"] != "buy":
            # 模拟盘从空仓开始：运行第一天均线状态已是「持有」→ 执行器当天开盘买入。回测同样从空仓开始，在第一天开盘买入
            o = float(df.loc[d, "open"])
            fills.append({"side": "buy", "ts": t, "key": t, "price": o * (1 + k), "mid": o, "reason": "均线择时（开始运行时已是持有状态）"})
            eq *= 1 - float(row["size"]) * k
        first = False
        if row["event"] in ("buy", "sell"):
            o = float(df.loc[d, "open"])
            side = row["event"]
            fills.append({"side": side, "ts": t, "key": t, "price": o * (1 + k if side == "buy" else 1 - k), "mid": o,
                          "reason": "均线择时"})
        eq *= 1 + float(row["pr"])
        curve.append((_ts(mk.session_close(d.date())), (eq - 1) * 100))
    px = pd.DataFrame({"ts": [_ts(mk.session_open(d.date())) for d in df.index], "open": df["open"].values,
                       "close": df["close"].values})
    px = px[px["ts"] >= start - pd.Timedelta(days=5)].reset_index(drop=True)
    from dataclasses import replace
    cm2 = replace(cm, spread_bps=sp)
    return {"fills": fills, "curve": curve, "cost": cm2, "prices": px, "symbol": sym, "market": mk.US_EQUITY, "metrics": {}}


# ---------------------------------------------------------------------------
# 配对与归类
# ---------------------------------------------------------------------------

def _tolerance(m, dep: dict) -> pd.Timedelta:
    if m.key != "crypto":
        return pd.Timedelta(days=4)
    tf = int(dep["config"].get("signal_tf") or 60)
    return pd.Timedelta(minutes=max(2 * tf, 120))


def match(bt_fills: list[dict], paper: list[dict], tol: pd.Timedelta) -> tuple[list, list, list]:
    """同方向、按时间顺序、最近的配对（容差内）。返回 (pairs, missed（回测有）, extra（模拟盘有）)。"""
    used = set()
    pairs, missed = [], []
    for b in sorted(bt_fills, key=lambda x: x["key"]):
        best, bd = None, None
        for j, p in enumerate(paper):
            if j in used or p["side"] != b["side"] or p.get("purpose") in ("add", "topup", "cap", "reclaim", "lend"):
                continue
            d = abs(p["key"] - b["key"])
            if d <= tol and (bd is None or d < bd):
                best, bd = j, d
        if best is None:
            missed.append(b)
        else:
            used.add(best)
            pairs.append((b, paper[best]))
    extra = [p for j, p in enumerate(paper) if j not in used]
    return pairs, missed, extra


def _outages():
    from . import uptime
    try:
        return [(_ts(r["start"]), _ts(r["end"])) for r in uptime.outages()]
    except Exception:  # noqa: BLE001
        return []


def _in_outage(t: pd.Timestamp, outs, before: pd.Timedelta, after: pd.Timedelta) -> bool:
    return any(s < t + after and e > t - before for s, e in outs)


_EVENT_CATS = [("过期", "信号过期（服务延迟）"), ("交易节奏", "交易节奏状态不同"), ("现金", "现金不足"),
               ("收回", "额度收回中"), ("额度为 0", "暂停或额度为 0"), ("暂停新开仓", "暂停或额度为 0"),
               ("行情过期", "行情缺失"), ("对账不一致", "持仓对账不一致"), ("未终结订单", "等待未终结订单")]


def classify_missed(dep: dict, b: dict, m, outs) -> str:
    """回测有、模拟盘没有的成交。"""
    t = b["key"]
    if m.key == "crypto":
        if _in_outage(t, outs, pd.Timedelta(minutes=1), pd.Timedelta(minutes=15)):
            return "中断导致"
        lo, hi = t - pd.Timedelta(minutes=1), t + pd.Timedelta(minutes=20)
    else:
        if _in_outage(b["ts"], outs, pd.Timedelta(minutes=10), pd.Timedelta(minutes=40)):
            return "中断导致"
        lo, hi = b["ts"] - pd.Timedelta(hours=1), b["ts"] + pd.Timedelta(hours=7)
    evs = db.q("SELECT message FROM paper_events WHERE deployment_id=? AND ts>=? AND ts<=? ORDER BY id",
               (dep["id"], lo.isoformat(), hi.isoformat()))
    for e in evs:
        for k, cat in _EVENT_CATS:
            if k in e["message"]:
                return cat
    if b["side"] == "buy" and (dep["status"] == "paused_entries" or
                               (dep["broker"] == "alpaca_paper" and float(dep["config"].get("budget_usd") or 1) <= 0)):
        return "暂停或额度为 0"
    return "策略偏差"


def classify_extra(dep: dict, p: dict, first_bt_side: str | None) -> str:
    if p.get("purpose") in ("add", "topup", "cap", "reclaim", "lend"):
        return "资金调整"
    if p["side"] == "sell" and first_bt_side in (None, "buy") and p.get("inherited"):
        return "继承持仓"
    if p.get("purpose") in ("stop", "guard"):
        return "止损执行差异"
    return "策略偏差"


# ---------------------------------------------------------------------------
# 收益曲线
# ---------------------------------------------------------------------------

def _grid(start: pd.Timestamp, now: pd.Timestamp, m) -> list[pd.Timestamp]:
    """每天一个点：加密货币 UTC 0 点（最后一个点 = now）；美股每个交易日收盘。"""
    from . import market as mk
    pts = []
    if m.key == "crypto":
        d = (start.normalize() + pd.Timedelta(days=1))
        while d < now:
            pts.append(d)
            d += pd.Timedelta(days=1)
        pts.append(now)
        return pts
    for day in mk.trading_days(start.tz_convert(mk.ET).date(), now.tz_convert(mk.ET).date()):
        c = _ts(mk.session_close(day))
        if c > start:
            pts.append(min(c, now))
    if not pts or pts[-1] < now:
        pts.append(now)
    return pts


def _price_at(px: pd.DataFrame, t: pd.Timestamp, m) -> float | None:
    """t 时刻（含）之前最后一个收盘价。美股日线 ts = 开盘时间，收盘在当天收盘时刻。"""
    if px is None or not len(px):
        return None
    if m.key == "crypto":
        x = px[px["ts"] + pd.Timedelta(minutes=5) <= t]
    else:
        from . import market as mk
        cl = px["ts"].map(lambda s: _ts(mk.session_close(s.tz_convert(mk.ET).date())))
        x = px[cl <= t]
    return float(x["close"].iloc[-1]) if len(x) else None


def _start_price(px: pd.DataFrame, start: pd.Timestamp, m) -> float | None:
    x = px[px["ts"] >= start] if m.key == "crypto" else px[px["ts"] >= start.normalize()]
    if len(x):
        return float(x["open"].iloc[0])
    return float(px["close"].iloc[-1]) if len(px) else None


def actual_curve(fills: list[dict], px: pd.DataFrame, grid: list[pd.Timestamp], m, fee_bps: dict,
                 start_qty: float = 0.0, start_value_px: float | None = None) -> list[float]:
    """模拟盘持仓的时间加权收益（%），在每笔成交处切分子区间连乘：
    - 两次事件之间：r = 现值 ÷ 上次事件后的市值 − 1（成交时用成交价估值，日终用收盘价）；
    - 买入：r = (买入后市值 − 买入前市值 − 买入花费) ÷ (买入前市值 + 买入花费)，即买入时的成本损耗；
    - 卖出：r = (卖出后市值 + 卖出所得 − 卖出前市值) ÷ 卖出前市值。
    空仓的区间收益为 0，与回测「满仓 / 空仓」的口径一致；同一天里多次进出也按顺序连乘。"""
    qty = start_qty
    prev_val = start_qty * (start_value_px or 0.0)
    eq, out, i = 1.0, [], 0
    fs = sorted(fills, key=lambda f: f["ts"])

    def mark(px_now: float) -> None:
        nonlocal eq, prev_val
        if prev_val > 1e-9 and qty > 1e-12:
            eq *= (qty * px_now) / prev_val
        prev_val = qty * px_now if qty > 1e-12 else 0.0

    for g in grid:
        while i < len(fs) and fs[i]["ts"] <= g:
            f = fs[i]
            mark(f["price"])
            before = prev_val
            if f["side"] == "buy":
                gross = f.get("req_qty") or f["qty"]
                cost = f["price"] * gross * (1 + fee_bps.get("buy_usd", 0) / 1e4)
                qty += f["qty"]
                after = qty * f["price"]
                eq *= 1 + (after - before - cost) / (before + cost)
            else:
                q = min(f["qty"], qty)
                proceeds = f["price"] * q * (1 - fee_bps.get("sell", 0) / 1e4)
                qty = max(0.0, qty - f["qty"])
                after = qty * f["price"]
                if before > 1e-9:
                    eq *= 1 + (after + proceeds - before) / before
            prev_val = after if qty > 1e-12 else 0.0
            i += 1
        p = _price_at(px, g, m)
        if p is not None:
            mark(p)
        out.append((eq - 1) * 100)
    return out


def _curve_at(curve: list[tuple], grid: list[pd.Timestamp]) -> list[float]:
    if not curve:
        return [0.0] * len(grid)
    ts = [c[0] for c in curve]
    out = []
    for g in grid:
        k = int(np.searchsorted(np.array([t.value for t in ts]), g.value, "right")) - 1
        out.append(float(curve[k][1]) if k >= 0 else 0.0)
    return out


# ---------------------------------------------------------------------------
# 一个部署的成绩单
# ---------------------------------------------------------------------------

def build(dep: dict, now: datetime | None = None) -> dict:
    """生成一个部署（当前版本、实际运行区间）的成绩单。"""
    from . import market as mk
    from . import spread_model
    now_ts = _ts(now or datetime.now(timezone.utc))
    conf = get_config()
    m = market_of(dep)
    start = segment_start(dep)
    bt_ = _rebacktest(dep, start, now_ts)
    paper = paper_fills(dep, start)
    inherited_qty = 0.0
    if dep["broker"] == "alpaca_us" and _has("us_ledger"):
        r = db.q1("SELECT COALESCE(SUM(CASE WHEN side='buy' THEN qty ELSE -qty END),0) AS q FROM us_ledger "
                  "WHERE deployment_id=? AND ts<?", (dep["id"], start.isoformat()))
        inherited_qty = max(0.0, float(r["q"] or 0)) if r else 0.0
    for p in paper:
        p["inherited"] = inherited_qty > 0
    pairs, missed, extra = match(bt_["fills"], paper, _tolerance(m, dep))
    outs = _outages()
    cm = bt_["cost"]
    tl = pd.Timedelta(minutes=conf["time_min"][m.key])
    px = bt_["prices"]
    rows, alerts = [], []
    price_th = conf["price_bps"][m.key]
    cost_act, cost_mod = [], []
    for b, p in pairs:
        sgn = 1 if b["side"] == "buy" else -1
        dev = sgn * (p["price"] - b["price"]) / b["price"] * 1e4
        dt = p["key"] - b["key"]
        mid = _paper_mid(p, px, m)
        model = cm.spread_at(p["ts"] if cm.spread_hourly is not None else None) / 2 + cm.slippage_bps
        act = sgn * (p["price"] - mid) / mid * 1e4 if mid else None
        fee_model = cm.fee_bps + (cm.sell_fee_bps if b["side"] == "sell" else 0.0)
        fee_act, fee_est = _paper_fee(p, cm, b["side"])
        if act is not None:
            cost_act.append(act + fee_act)
            cost_mod.append(model + fee_model)
        st = "一致" if abs(dt) <= tl else "时间偏差"
        row = {"status": st, "side": b["side"], "bt_ts": b["ts"].isoformat(), "paper_ts": p["ts"].isoformat(),
               "bt_key": b["key"].isoformat(), "paper_key": p["key"].isoformat(), "dt_min": round(dt.total_seconds() / 60, 1),
               "bt_price": round(b["price"], 6), "paper_price": round(p["price"], 6), "price_dev_bps": round(dev, 2),
               "cost_act_bps": round(act + fee_act, 2) if act is not None else None, "cost_model_bps": round(model + fee_model, 2),
               "fee_estimated": fee_est, "reason": b.get("reason"), "category": None}
        if st == "时间偏差":
            row["category"] = "时间偏差"
            alerts.append(("时间偏差", f"{b['key']:%m-%d %H:%M}", f"{_side(b['side'])}信号时间相差 {row['dt_min']:.0f} 分钟"))
        if abs(dev) > price_th:
            row["category"] = row["category"] or "成交价偏差"
            alerts.append(("成交价偏差", f"{p['ts']:%m-%d %H:%M}-{b['side']}",
                           (f"{_side(b['side'])}成交价比回测差 " if dev > 0 else f"{_side(b['side'])}成交价比回测好 ")
                           + f"{abs(dev):.1f} bps（阈值 {price_th:g}）"))
        rows.append(row)
    first_side = min(bt_["fills"], key=lambda x: x["key"])["side"] if bt_["fills"] else None
    entry_cat: dict = {}
    for b in sorted(missed, key=lambda x: x["key"]):
        if b["side"] == "sell" and b.get("trade") is not None and b["trade"] in entry_cat:
            cat = entry_cat[b["trade"]]          # 入场就没跟上（例如中断）→ 对应的离场也没有，同一原因
        else:
            cat = classify_missed(dep, b, m, outs)
        if b["side"] == "buy" and b.get("trade") is not None:
            entry_cat[b["trade"]] = cat
        rows.append({"status": "漏单", "side": b["side"], "bt_ts": b["ts"].isoformat(), "paper_ts": None,
                     "bt_key": b["key"].isoformat(), "bt_price": round(b["price"], 6), "category": cat, "reason": b.get("reason")})
        if cat not in NON_STRATEGY:
            alerts.append((f"漏单·{cat}", f"{b['key']:%m-%d %H:%M}-{b['side']}", f"回测在 {_bj(b['key'])} {_side(b['side'])}，模拟盘没有（{cat}）"))
    for p in extra:
        cat = classify_extra(dep, p, first_side)
        rows.append({"status": "多单", "side": p["side"], "bt_ts": None, "paper_ts": p["ts"].isoformat(),
                     "paper_key": p["key"].isoformat(), "paper_price": round(p["price"], 6), "category": cat,
                     "reason": p.get("purpose") or None})
        if cat not in NON_STRATEGY:
            alerts.append((f"多单·{cat}", f"{p['ts']:%m-%d %H:%M}-{p['side']}", f"模拟盘在 {_bj(p['ts'])} {_side(p['side'])}，回测没有（{cat}）"))
    rows.sort(key=lambda x: x.get("bt_key") or x.get("paper_key") or "")
    # 成本
    avg_act = float(np.mean(cost_act)) if cost_act else None
    avg_mod = float(np.mean(cost_mod)) if cost_mod else None
    if avg_act is not None and avg_act - avg_mod > conf["cost_bps"]:
        alerts.append(("成本偏差", "avg", f"平均实际成本 {avg_act:.1f} bps，比模型 {avg_mod:.1f} bps 高 {avg_act - avg_mod:.1f}"
                                           f"（阈值 {conf['cost_bps']:g}）"))
    # 收益曲线
    grid = _grid(start, now_ts, m)
    fee = {"buy_usd": 0.0, "sell": cm.fee_bps + cm.sell_fee_bps} if m.key == "crypto" else {"buy_usd": 0.0, "sell": cm.sell_fee_bps}
    p0 = _start_price(px, start, m)
    a = actual_curve(paper, px, grid, m, fee, start_qty=inherited_qty, start_value_px=p0)
    e = _curve_at(bt_["curve"], grid)
    bench = [((_price_at(px, g, m) or p0) / p0 - 1) * 100 if p0 else 0.0 for g in grid]
    gap = (a[-1] - e[-1]) if a and e else 0.0
    if abs(gap) > conf["return_pp"]:
        alerts.append(("收益偏差", "ret", f"累计收益 实际 {a[-1]:+.2f}% vs 回测 {e[-1]:+.2f}%，相差 {gap:+.2f} 个百分点"
                                         f"（阈值 {conf['return_pp']:g}）"))
    days = _forward_days(start, now_ts, m)
    n_trades = len(paper)
    insufficient = n_trades < conf["min_trades"] or days < conf["min_days"]
    cats = {}
    for r in rows:
        if r.get("category"):
            cats[r["category"]] = cats.get(r["category"], 0) + 1
    strat = [k for k in cats if k not in NON_STRATEGY]
    if insufficient:
        verdict = "样本不足，暂不能下结论"
    elif not alerts:
        verdict = "与回测一致"
    else:
        verdict = "有偏差：" + "、".join(sorted({a_[0].split("·")[-1] if a_[0].startswith(("漏单", "多单")) else a_[0]
                                                for a_ in alerts}))
    return {
        "deployment_id": dep["id"], "version_id": dep["version_id"], "label": _label(dep), "market": m.key,
        "symbol": bt_["symbol"], "start": start.isoformat(), "end": now_ts.isoformat(), "forward_days": days,
        "day_unit": "天" if m.key == "crypto" else "个交易日", "paper_fills": n_trades,
        "bt_fills": len(bt_["fills"]), "matched": sum(1 for r in rows if r["status"] in ("一致", "时间偏差")),
        "consistent": sum(1 for r in rows if r["status"] == "一致"),
        "missed": len(missed), "extra": len(extra), "categories": cats, "strategy_categories": strat,
        "avg_cost_act_bps": round(avg_act, 2) if avg_act is not None else None,
        "avg_cost_model_bps": round(avg_mod, 2) if avg_mod is not None else None,
        "return_actual_pct": round(a[-1], 3) if a else 0.0, "return_expected_pct": round(e[-1], 3) if e else 0.0,
        "return_bench_pct": round(bench[-1], 3) if bench else 0.0, "return_gap_pp": round(gap, 3),
        "curve": [[g.isoformat(), round(x, 4), round(y, 4), round(z, 4)] for g, x, y, z in zip(grid, a, e, bench)],
        "rows": rows, "alerts": [{"kind": k, "key": kk, "text": t} for k, kk, t in alerts],
        "insufficient": insufficient, "verdict": verdict, "cost_basis": getattr(cm, "basis", "fixed_v1"),
        "cost_basis_label": spread_model.basis_label(getattr(cm, "basis", "fixed_v1")),
        "inherited_qty": inherited_qty, "thresholds": conf,
    }


def _forward_days(start: pd.Timestamp, now: pd.Timestamp, m) -> int:
    from . import market as mk
    if m.key == "crypto":
        return int((now - start).total_seconds() // 86400)
    return len([d for d in mk.trading_days(start.tz_convert(mk.ET).date(), now.tz_convert(mk.ET).date())
                if _ts(mk.session_close(d)) <= now and _ts(mk.session_open(d)) >= start - pd.Timedelta(hours=12)])


def _paper_mid(p: dict, px: pd.DataFrame, m) -> float | None:
    """模拟盘成交时的中间价代理：BTC = 成交所在 5 分钟 K 线开盘价；美股 = 下单时的参考价（没有则当天开盘价）。"""
    if m.key != "crypto":
        if p.get("ref_price"):
            return p["ref_price"]
        x = px[px["ts"] <= p["ts"]]
        return float(x["open"].iloc[-1]) if len(x) else None
    x = px[(px["ts"] <= p["ts"]) & (px["ts"] + pd.Timedelta(minutes=5) > p["ts"])]
    return float(x["open"].iloc[0]) if len(x) else None


def _paper_fee(p: dict, cm, side: str) -> tuple[float, bool]:
    """(手续费 bps, 是否为估计)。BTC 买入的手续费从所得币里扣：请求数量 − 实际到手；卖出从美元扣，按费率估计。"""
    if cm.fee_bps == 0 and side == "buy":
        return 0.0, False
    if side == "buy" and p.get("req_qty") and p["req_qty"] > p["qty"]:
        return (p["req_qty"] - p["qty"]) / p["req_qty"] * 1e4, False
    return cm.fee_bps + (cm.sell_fee_bps if side == "sell" else 0.0), True


def _side(s: str) -> str:
    return "买入" if s == "buy" else "卖出"


def _bj(t: pd.Timestamp) -> str:
    return (t + pd.Timedelta(hours=8)).strftime("%m-%d %H:%M") + " 北京时间"


def _label(dep: dict) -> str:
    if dep["broker"] == "alpaca_us":
        from .paper import us_exec
        return us_exec.label(dep)
    return dep["config"].get("label") or f"#{dep['id']}"


# ---------------------------------------------------------------------------
# 每天运行、保存、通知
# ---------------------------------------------------------------------------

def deployments() -> list[dict]:
    from .paper import runner as pr
    return [pr.get_deployment(r["id"]) for r in db.q(
        "SELECT id FROM paper_deployments WHERE broker IN ('alpaca_paper','alpaca_us') AND is_test=0 "
        "AND status IN ('running','paused_entries') ORDER BY id")]


def run_all(now: datetime | None = None, notify_: bool = True) -> list[dict]:
    ensure_tables()
    now = now or datetime.now(timezone.utc)
    out = []
    for dep in deployments():
        try:
            sc = build(dep, now)
        except Exception as e:  # noqa: BLE001
            log.warning("scorecard failed for #%s", dep["id"], exc_info=True)
            sc = {"deployment_id": dep["id"], "version_id": dep["version_id"], "label": _label(dep),
                  "error": f"{type(e).__name__}: {str(e)[:200]}", "verdict": "成绩单生成失败", "alerts": [],
                  "insufficient": True}
        save(sc, now)
        if notify_ and not sc.get("error"):
            _notify(sc)
        out.append(sc)
    db.set_setting(RUN_KEY, {"at": db.now_iso(), "n": len(out)})
    return out


def save(sc: dict, now: datetime) -> None:
    day = now.strftime("%Y-%m-%d")
    db.ex("INSERT OR REPLACE INTO scorecards(deployment_id,version_id,day,created_at,data_json) VALUES(?,?,?,?,?)",
          (sc["deployment_id"], sc.get("version_id"), day, db.now_iso(), json.dumps(sc, ensure_ascii=False, default=str)))


def latest(did: int) -> dict | None:
    ensure_tables()
    r = db.q1("SELECT * FROM scorecards WHERE deployment_id=? ORDER BY day DESC, id DESC LIMIT 1", (did,))
    return json.loads(r["data_json"]) if r else None


def latest_all() -> dict[int, dict]:
    ensure_tables()
    out = {}
    for r in db.q("SELECT deployment_id, data_json FROM scorecards WHERE id IN "
                  "(SELECT MAX(id) FROM scorecards GROUP BY deployment_id)"):
        out[r["deployment_id"]] = json.loads(r["data_json"])
    return out


def _notify(sc: dict) -> int:
    """每个偏差只通知一次（按 部署 / 版本 / 类型 / 时刻 记住）。样本不足时只通知信号类偏差（漏单、多单、时间偏差）。"""
    if not sc.get("alerts"):
        return 0
    key = f"scorecard_notified::{sc['deployment_id']}::{sc.get('version_id')}"
    seen = set(db.get_setting(key) or [])
    new = []
    for a in sc["alerts"]:
        k = f"{a['kind']}|{a['key']}"
        if k in seen:
            continue
        if sc.get("insufficient") and a["kind"] in ("成本偏差", "收益偏差"):
            continue
        if a["kind"] in ("成本偏差", "收益偏差"):           # 汇总类：数值会变，按天通知一次
            k = f"{a['kind']}|{sc['end'][:10]}"
            if k in seen:
                continue
        new.append((k, a))
    if not new:
        return 0
    from . import notify
    body = "\n".join(f"• {a['kind'].split('·')[0]}：{a['text']}" for _, a in new[:8])
    body += f"\n（累计收益 实际 {sc['return_actual_pct']:+.2f}% / 回测 {sc['return_expected_pct']:+.2f}% / 基准 " \
            f"{sc['return_bench_pct']:+.2f}%；运行 {sc['forward_days']} {sc['day_unit']}，成交 {sc['paper_fills']} 笔）"
    try:
        notify.event("scorecard", f"策略成绩单偏差：{sc['label']}", body,
                     dedupe=f"sc-{sc['deployment_id']}-{new[0][0]}")
    except Exception:  # noqa: BLE001
        log.exception("scorecard notify failed")
    from .paper import runner as pr
    pr.event(sc["deployment_id"], "WARN", f"策略成绩单：{sc['verdict']}；" + "；".join(a["text"] for _, a in new[:4]))
    seen |= {k for k, _ in new}
    db.set_setting(key, sorted(seen)[-500:])
    return len(new)


def maybe_daily(now: datetime | None = None) -> bool:
    """每天一次（UTC run_hour_utc 点之后：美股收盘、日线入库之后；北京时间早上 6 点）。"""
    now = now or datetime.now(timezone.utc)
    if now.hour < int(get_config()["run_hour_utc"]):
        return False
    last = db.get_setting(RUN_KEY) or {}
    if (last.get("at") or "")[:10] == now.strftime("%Y-%m-%d") or _running.is_set():
        return False
    db.set_setting(RUN_KEY, {"at": db.now_iso(), "n": 0, "state": "running"})
    _running.set()

    def work():
        try:
            run_all(now)
        except Exception:  # noqa: BLE001
            log.exception("scorecard daily run failed")
        finally:
            _running.clear()
    import threading
    threading.Thread(target=work, daemon=True, name="aiquant-scorecard").start()
    return True


def card_summary(did: int) -> dict | None:
    """策略总台卡片：一句话 + 颜色。"""
    sc = latest(did)
    if not sc:
        return None
    if sc.get("error"):
        return {"text": "成绩单：生成失败", "tone": "gray", "detail": sc["error"]}
    if sc.get("insufficient"):
        tone, text = "gray", f"成绩单：样本不足（{sc['forward_days']} {sc['day_unit']}，{sc['paper_fills']} 笔）"
    elif not sc.get("alerts"):
        tone, text = "green", "成绩单：与回测一致"
    else:
        tone, text = "orange", "成绩单：" + sc["verdict"].replace("有偏差：", "偏差 · ")
    cats = sc.get("categories") or {}
    detail = "；".join(f"{k} {v}" for k, v in cats.items())
    return {"text": text, "tone": tone, "detail": detail, "verdict": sc["verdict"]}
