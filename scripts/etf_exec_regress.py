"""同一份真实日线、同一个假券商：旧 ETF 执行器（e8671a7 的 etf_trend.step）与新通用美股执行器逐日比对下单。
用法：python etf_exec_regress.py <代码树> <etf_daily 目录> <开始> <结束> <输出.json>"""
import json, os, shutil, sys, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
tree, etf_dir, d0, d1, out = sys.argv[1:6]
root = tempfile.mkdtemp(); os.environ.update(AIQUANT_ROOT=root, AIQUANT_DISABLE_PAPER_LOOP="1", AIQUANT_DISABLE_LIVE_FEED="1")
sys.path.insert(0, tree)
(Path(root) / "data").mkdir(parents=True); shutil.copytree(etf_dir, Path(root) / "data" / "etf_daily")
import pandas as pd
from aiquant import db, market as mk
from aiquant.data import us_daily
from aiquant.paper import etf_trend as et
db.init_db()
try:
    from aiquant import actions; actions.init()
except Exception as e:
    print("init", e)
D = {}
for s in ("QQQ", "QLD"):
    x = pd.read_csv(Path(root) / "data" / "etf_daily" / f"{s}.csv"); x["d"] = us_daily.et_date(x["date"]); D[s] = x
NOW = {"t": None}
st = {"cash": 100000.0, "qty": 0.0}
orders = []
def px(t):
    day = t.astimezone(mk.ET).date(); x = D["QLD"]; r = x[x["d"] == day]
    if len(r) and t >= mk.session_open(day):
        return float(r["close"].iloc[0] if t >= mk.session_close(day) else r["open"].iloc[0])
    return float(x[x["d"] < day]["close"].iloc[-1])
def bars(sym, days_=420):
    t = NOW["t"]; day = t.astimezone(mk.ET).date(); x = D[sym]
    done = mk.is_trading_day(day) and t >= mk.session_close(day)
    x = x[(x["d"] < day) | ((x["d"] == day) & done)]
    return pd.DataFrame({"date": x["d"].values, "close": x["close"].values})
def submit(sym, side, notional=None, qty=None, **kw):
    p = px(NOW["t"]); q = notional / p if notional is not None else float(qty)
    if side == "buy":
        assert q * p <= st["cash"] + 1e-6; st["cash"] -= q * p; st["qty"] += q
    else:
        st["qty"] -= q; st["cash"] += q * p
    orders.append({"date": str(NOW["t"].astimezone(mk.ET).date()), "side": side, "notional": round(notional, 2) if notional else None,
                   "qty": round(q, 6), "price": p})
    coid = kw.get("coid") or f"x{len(orders)}"
    if not NEW:   # 旧执行器：订单与成交记在 etf_orders（原 _submit + sync_orders 的结果）
        db.ex("INSERT INTO etf_orders(ts,symbol,side,notional,qty,status,filled_qty,filled_price,client_order_id,signal_date,reason)"
              " VALUES(?,?,?,?,?,?,?,?,?,?,?)", (db.now_iso(), sym, side, notional, qty, "filled", q, p, coid,
                                               kw.get("signal_date"), kw.get("reason")))
    FILLS[coid] = {"client_order_id": coid, "status": "filled", "filled_qty": str(q), "filled_avg_price": str(p)}
    return {"status": "filled", "filled_qty": str(q), "filled_avg_price": str(p), "client_order_id": coid}
FILLS = {}
import importlib.util
NEW = importlib.util.find_spec("aiquant.paper.us_exec") is not None
if NEW and os.environ.get("REBAL_PP"):
    db.set_setting("capital_layers", {"rebalance_pp": float(os.environ["REBAL_PP"])})
et._submit = submit
et._position = lambda s: {"qty": st["qty"], "market_value": st["qty"] * px(NOW["t"]), "avg_entry_price": 0, "unrealized_pl": 0}
et._account = lambda: {"equity": str(st["cash"] + st["qty"] * px(NOW["t"])), "cash": str(st["cash"])}
et._clock = lambda: {"is_open": True}
et.daily_bars = bars
et.update_daily_csv = lambda *a, **k: {}
et.http.paper_orders_unlocked = lambda: True
from aiquant.data import alpaca
_orig = alpaca.paper_get
def pg(path, params=None):
    if path == "/v2/orders:by_client_order_id":
        o = FILLS.get((params or {}).get("client_order_id")); return (200, o) if o else (404, {})
    if path.startswith("/v2/orders"):
        return 404, {}
    return 404, {}
alpaca.paper_get = pg
db.set_setting("etf_trend_config", {"enabled": True, "signal_symbol": "QQQ", "trade_symbol": "QLD", "sma": 150, "band": 0.02,
                                    "budget_pct": 30.0, "lend_pct": 50.0})
for day in mk.trading_days(pd.Timestamp(d0).date(), pd.Timestamp(d1).date()):
    NOW["t"] = mk.session_open(day) + timedelta(minutes=10)
    et.step(NOW["t"])
    NOW["t"] = mk.session_close(day) + timedelta(minutes=20)
    try:
        et.after_close(NOW["t"])
    except Exception as e:
        pass
Path(out).write_text(json.dumps(orders, ensure_ascii=False))
print(len(orders), round(st["cash"] + st["qty"] * px(NOW["t"]), 2))
