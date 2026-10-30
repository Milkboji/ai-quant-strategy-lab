"""美股回测新旧对比（止损 / 移动止损 / 跳空）：同一份真实日线，在指定代码树上跑多个版本，输出逐笔成交。
用法：python us_stop_regress.py <代码树> <etf_daily 目录> <输出.json>"""
import json
import os
import sys
import tempfile

tree, etf_dir, out = sys.argv[1:4]
os.environ["AIQUANT_ROOT"] = tempfile.mkdtemp()
sys.path.insert(0, tree)
import pandas as pd  # noqa: E402

from aiquant import market as mk  # noqa: E402
from aiquant.engine import backtest as bt  # noqa: E402
from aiquant.strategy.schema import validate  # noqa: E402


def bars(sym):
    d = pd.read_csv(os.path.join(etf_dir, f"{sym}.csv"))
    days = pd.to_datetime(d["date"], utc=True).dt.tz_convert(mk.ET).dt.date
    return pd.DataFrame({"ts": pd.to_datetime([mk.session_open(x) for x in days], utc=True),
                         "close_time": pd.to_datetime([mk.session_close(x) for x in days], utc=True),
                         "open": d["open"].astype(float), "high": d["high"].astype(float), "low": d["low"].astype(float),
                         "close": d["close"].astype(float), "volume": d["volume"].astype(float)})


def schema(sig, n, stop=None, trail=None, tp=None):
    s = {"name": f"{sig}{n}", "signal_tf": 1440, "signal_symbol": sig,
         "indicators": [{"id": "ma", "type": "SMA", "params": {"period": n}}],
         "entry": {"mode": "all", "conditions": [{"left": {"ref": "close"}, "op": ">", "right": {"ref": "ma"}}]},
         "exit": {"mode": "any", "conditions": [{"left": {"ref": "close"}, "op": "<", "right": {"ref": "ma"}}]}}
    if stop:
        s["stop_loss_pct"] = stop
    if trail:
        s["trailing"] = {"activate_pct": trail[0], "trail_pct": trail[1]}
    if tp:
        s["take_profit_pct"] = tp
    return s


cases = []
for sig, trade in (("QQQ", "QLD"), ("SPY", "SSO")):
    for n in (5, 10, 50):
        for stop, trail, tp in ((None, None, None), (3, None, None), (5, None, None), (None, (5, 3), None), (5, (8, 4), 20),
                                (None, None, 5), (None, None, 10)):
            cases.append((sig, trade, n, stop, trail, tp))
B = {s: bars(s) for s in ("QQQ", "QLD", "SPY", "SSO")}
res = {}
for sig, trade, n, stop, trail, tp in cases:
    obj, errs = validate(schema(sig, n, stop, trail, tp))
    assert not errs, errs
    df, ds = B[trade], B[sig]
    start, end = pd.Timestamp("2017-01-03", tz="UTC"), pd.Timestamp("2026-10-29", tz="UTC")
    cfg = bt.BTConfig(start=start, end=end, initial_cash=10_000, cost=mk.US_EQUITY.cost(), market=mk.US_EQUITY)
    r = bt.run(df, obj, 1440, None, cfg, df_signal=ds)
    key = f"{sig}->{trade} SMA{n} stop={stop} trail={trail} tp={tp}"
    from aiquant.engine.execution import fill
    gaps = tp_gaps = 0   # 跳空：开盘价已越过止损 / 止盈价 → 按开盘价成交（成交价 = 开盘价扣成本）
    o = dict(zip(df["ts"].astype(str), df["open"]))
    for t in r["trades"]:
        if t["exit_reason"] in ("止损", "移动止损", "止盈"):
            op = o.get(str(pd.Timestamp(t["exit_fill_ts"])))
            if op and abs(fill("sell", op, t["qty"], cfg.cost).price - t["exit_price"]) < 1e-6 * op:
                if t["exit_reason"] == "止盈":
                    tp_gaps += 1
                else:
                    gaps += 1
    res[key] = {"final": round(r["metrics"]["final_equity"], 6), "trades": len(r["trades"]),
                "stops": sum(1 for t in r["trades"] if t["exit_reason"] in ("止损", "移动止损")), "gap_fills": gaps,
                "tps": sum(1 for t in r["trades"] if t["exit_reason"] == "止盈"), "tp_gap_fills": tp_gaps,
                "rows": [[t["entry_fill_ts"], round(t["entry_price"], 6), t["exit_fill_ts"], round(t["exit_price"], 6),
                          t["exit_reason"], round(t["pnl"], 6)] for t in r["trades"]]}
json.dump(res, open(out, "w"), ensure_ascii=False)
print(len(res), "cases")
