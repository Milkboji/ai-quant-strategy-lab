"""短线策略研究（BTC + ETH，15 分钟，只用训练期）。

规则（事先定好，不看验证集 / 测试集）：
- 窗口：2022-01-01 ~ 加密货币验证集开始（research.get_split()["val_start"]）；数据为本地 5 分钟历史（data/history）。
- 成本、交易节奏、持仓上限全部走 research.bt_config（与进化、模拟盘同一入口）。
- 候选只来自下面几种「短线结构」，每种一小组参数（不做细搜）。
- 入选条件：BTC 与 ETH 都满足 净收益 > 0、交易 ≥ 40 笔、训练期内完整年份里至少一半盈利；
  按两者收益回撤比的较小值排序（偏好两边都稳的）。

用法：python scripts/shortterm_research.py <history 目录> <输出.json>
"""
from __future__ import annotations

import itertools
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TRAIN_START = datetime(2022, 1, 1, tzinfo=timezone.utc)


def load5(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, usecols=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def ind(id_, typ, tf="main", **p):
    return {"id": id_, "type": typ, "tf": tf, "params": {k: float(v) for k, v in p.items()}}


def c(l, op, r=None, v=None, mult=1.0):
    right = {"ref": r, "mult": mult} if r is not None else {"value": v}
    return {"left": {"ref": l}, "op": op, "right": right}


def family_pullback(ema_tr, rsi_lo, sl, act, trail):
    """顺势回踩：4 小时趋势向上、15 分钟在长均线上方，RSI 从超卖区拐头向上时买；RSI 过热或跌破长均线离场。"""
    return {
        "name": f"短线-顺势回踩 e{ema_tr}/r{rsi_lo}", "signal_tf": 15, "aux_tf": 240,
        "indicators": [ind("trend4h", "EMA", "aux", period=ema_tr), ind("ema200", "EMA", period=200),
                       ind("rsi", "RSI", period=14)],
        "entry": {"mode": "all", "conditions": [c("aux.close", ">", "trend4h"), c("close", ">", "ema200"),
                                                 c("rsi", "cross_above", v=rsi_lo)]},
        "exit": {"mode": "any", "conditions": [c("rsi", ">", v=78), c("close", "<", "ema200", mult=0.99)]},
        "stop_loss_pct": sl, "trailing": {"activate_pct": act, "trail_pct": trail}, "max_hold_bars": 192,
        "hypothesis": "大周期向上时，短线回调到超卖后的反弹延续概率更高",
    }


def family_squeeze(sq, vol_mult, sl, act, trail):
    """收口突破：布林带刚经历收口、放量冲出上轨，1 小时趋势向上时买；跌回中轨离场。"""
    return {
        "name": f"短线-收口突破 sq{sq}/v{vol_mult}", "signal_tf": 15, "aux_tf": 60,
        "indicators": [ind("sq", "BB_SQUEEZE", period=20, std=2, lookback=200, recent=8),
                       ind("bbu", "BB_UPPER", period=20, std=2), ind("bbm", "BB_MID", period=20, std=2),
                       ind("vavg", "VOL_SMA", period=48), ind("trend1h", "EMA", "aux", period=50)],
        "entry": {"mode": "all", "conditions": [c("sq", "<", v=sq), c("close", "cross_above", "bbu"),
                                                 c("volume", ">", "vavg", mult=vol_mult), c("aux.close", ">", "trend1h")]},
        "exit": {"mode": "any", "conditions": [c("close", "cross_below", "bbm")]},
        "stop_loss_pct": sl, "trailing": {"activate_pct": act, "trail_pct": trail}, "max_hold_bars": 96,
        "hypothesis": "低波动收口后的放量突破，常带来一段持续几小时的单边行情",
    }


def family_donchian(n_in, n_out, sl, act, trail):
    """短周期通道突破：4 小时趋势向上时，突破前 n_in 根最高价买，跌破前 n_out 根最低价卖。"""
    return {
        "name": f"短线-通道突破 {n_in}/{n_out}", "signal_tf": 15, "aux_tf": 240,
        "indicators": [ind("hi", "DONCHIAN_HIGH", period=n_in), ind("lo", "DONCHIAN_LOW", period=n_out),
                       ind("trend4h", "EMA", "aux", period=50), ind("vavg", "VOL_SMA", period=96)],
        "entry": {"mode": "all", "conditions": [c("close", ">", "hi"), c("aux.close", ">", "trend4h"),
                                                 c("volume", ">", "vavg", mult=1.2)]},
        "exit": {"mode": "any", "conditions": [c("close", "<", "lo")]},
        "stop_loss_pct": sl, "trailing": {"activate_pct": act, "trail_pct": trail}, "max_hold_bars": 192,
        "hypothesis": "趋势中的短周期新高突破，用较短的通道低点跟踪离场",
    }


def family_meanrev(pctb, tp, sl):
    """超跌反弹：4 小时趋势向上，15 分钟跌破布林下轨且 RSI 很低时买，固定止盈 / 止损 / 最长 1 天。"""
    return {
        "name": f"短线-超跌反弹 b{pctb}/tp{tp}", "signal_tf": 15, "aux_tf": 240,
        "indicators": [ind("pb", "BB_PCTB", period=20, std=2), ind("rsi", "RSI", period=14),
                       ind("trend4h", "EMA", "aux", period=50)],
        "entry": {"mode": "all", "conditions": [c("pb", "<", v=pctb), c("rsi", "<", v=25), c("aux.close", ">", "trend4h")]},
        "exit": {"mode": "any", "conditions": [c("pb", ">", v=90)]},
        "stop_loss_pct": sl, "take_profit_pct": tp, "max_hold_bars": 96,
        "hypothesis": "上升趋势里的急跌常被快速收复",
    }


def candidates() -> list[dict]:
    out = []
    for e, r, (sl, act, tr) in itertools.product((50, 100), (30, 35), ((2.0, 2.0, 1.2), (3.0, 3.0, 1.5))):
        out.append(family_pullback(e, r, sl, act, tr))
    for sq, vm, (sl, act, tr) in itertools.product((10, 20), (1.5, 2.0), ((1.5, 1.5, 1.0), (2.5, 2.5, 1.5))):
        out.append(family_squeeze(sq, vm, sl, act, tr))
    for (ni, no), (sl, act, tr) in itertools.product(((48, 24), (96, 32)), ((2.0, 2.0, 1.2), (3.0, 3.0, 1.5))):
        out.append(family_donchian(ni, no, sl, act, tr))
    for pb, (tp, sl) in itertools.product((0, -10), ((1.5, 2.0), (2.5, 3.0))):
        out.append(family_meanrev(pb, tp, sl))
    return out


def evaluate(df5: pd.DataFrame, sch: dict, start: datetime, end: datetime, symbol: str) -> dict:
    from aiquant import market as mk
    from aiquant import research
    from aiquant.engine import backtest as bt
    from aiquant.strategy.schema import validate
    obj, errs = validate(sch)
    if errs:
        raise ValueError(errs)
    r = dict(research.get_research(), symbol=symbol)
    cfg = research.bt_config(start, end, r, market=mk.CRYPTO)
    res = bt.run(df5, obj, 15, sch.get("aux_tf"), cfg)
    m = res["metrics"]
    eq = pd.DataFrame(res["equity"], columns=["ts", "eq"])
    eq["ts"] = pd.to_datetime(eq["ts"], utc=True)
    eq = eq.set_index("ts")["eq"]
    years = {}
    for y in range(start.year, end.year + 1):
        seg = eq[(eq.index.year == y)]
        if len(seg) > 1:
            years[str(y)] = round((seg.iloc[-1] / seg.iloc[0] - 1) * 100, 2)
    full = [y for y in years if int(y) < end.year]
    ret, dd = float(m["net_return_pct"]), float(m["max_drawdown_pct"])
    trades = res.get("trades") or []
    fees = sum(float(t.get("fee", 0) or 0) for t in trades) if trades and isinstance(trades[0], dict) else None
    return {"return_pct": round(ret, 2), "max_dd_pct": round(dd, 2),
            "ret_dd": round(ret / abs(dd), 3) if dd else None, "trades": int(m["trades"] or 0),
            "win_rate": m.get("win_rate_pct"), "years": years,
            "positive_full_years": sum(1 for y in full if years[y] > 0), "full_years": len(full),
            "fee_usd": fees}


def main() -> None:
    hist, out = Path(sys.argv[1]), Path(sys.argv[2])
    from aiquant import actions, research
    from aiquant.data import market_data as md
    actions.init()
    end = md.to_utc(research.get_split()["val_start"])
    data = {"BTC/USD": load5(hist / "BTCUSD_5m.csv.gz"), "ETH/USD": load5(hist / "ETHUSD_5m.csv.gz")}
    rows = []
    for sch in candidates():
        row = {"name": sch["name"], "schema": sch}
        for sym, df in data.items():
            d = df[(df["ts"] >= TRAIN_START - pd.Timedelta(days=60)) & (df["ts"] < end)]
            row[sym] = evaluate(d, sch, TRAIN_START, end, sym)
        b, e = row["BTC/USD"], row["ETH/USD"]
        ok = all(x["return_pct"] > 0 and x["trades"] >= 40 and x["positive_full_years"] * 2 >= x["full_years"]
                 for x in (b, e))
        row["qualified"] = ok
        row["rank_key"] = min(b["ret_dd"] or -9, e["ret_dd"] or -9)
        rows.append(row)
        print(f"{sch['name']:<28} BTC {b['return_pct']:>7.1f}% dd {b['max_dd_pct']:>6.1f} n {b['trades']:>4} "
              f"| ETH {e['return_pct']:>7.1f}% dd {e['max_dd_pct']:>6.1f} n {e['trades']:>4} {'OK' if ok else ''}",
              flush=True)
    rows.sort(key=lambda r: (r["qualified"], r["rank_key"]), reverse=True)
    out.write_text(json.dumps({"window": [TRAIN_START.isoformat(), end.isoformat()], "rows": rows},
                              ensure_ascii=False, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
