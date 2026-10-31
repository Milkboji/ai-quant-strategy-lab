"""2 倍杠杆模拟账户：跟随全部 BTC 策略（多头腿按额度占比）、部分卖出按比例平仓、资金费、整体强平、镜像做空腿、做空资格回测。"""
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from aiquant import db, shortside
from aiquant.paper import leverage_shadow as ls
from aiquant.strategy.schema import validate

T0 = datetime(2026, 10, 21, 1, 0, tzinfo=timezone.utc)


@pytest.fixture()
def lev(env):
    ls.ensure_tables()
    db.ex("UPDATE paper_deployments SET status='stopped' WHERE broker='alpaca_paper'")
    db.ex("DELETE FROM settings WHERE key IN ('lev_shadow_state','lev_shadow_config')")
    db.ex("DELETE FROM lev_shadow_trades")
    ids = []

    def dep(budget):
        did = db.ex("INSERT INTO paper_deployments(version_id,config_json,broker,status,created_at,updated_at,is_test) "
                    "VALUES(?,?,?,?,?,?,0)", (1, json.dumps({"label": f"测试{budget}", "budget_usd": budget}),
                                              "alpaca_paper", "running", db.now_iso(), db.now_iso()))
        ids.append(did)
        return did
    db.set_setting("lev_shadow_state", {"started_at": "2026-01-01T00:00:00+00:00"})
    yield dep
    for d in ids:
        db.ex("UPDATE paper_deployments SET status='stopped' WHERE id=?", (d,))
        db.ex("DELETE FROM paper_orders WHERE deployment_id=?", (d,))
    db.ex("DELETE FROM settings WHERE key IN ('lev_shadow_state','lev_shadow_config')")


def _order(did, side, px, qty, tag):
    db.ex("INSERT INTO paper_orders(deployment_id,client_order_id,broker,side,qty,status,filled_qty,filled_price,"
          "signal_ts,created_at,updated_at,is_test) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
          (did, f"lev-{did}-{tag}", "alpaca_paper", side, qty, "filled", qty, px, "x", db.now_iso(), db.now_iso()))


def test_follows_all_btc_strategies_by_budget_share(lev):
    a, b = lev(3000), lev(1000)
    _order(a, "buy", 100000, 0.03, "a1")
    _order(b, "buy", 100000, 0.01, "b1")
    s = ls.update(100000, T0)
    assert set(s["legs"]) == {f"L{a}", f"L{b}"}
    assert s["legs"][f"L{a}"]["qty"] == pytest.approx(2 * 10000 * 0.75 / 100000)    # 2 倍 × 保证金 × 75%
    assert s["legs"][f"L{b}"]["qty"] == pytest.approx(2 * 10000 * 0.25 / 100000)
    _order(a, "sell", 105000, 0.015, "a2")                                             # 卖一半现货 → 平一半多头腿
    s = ls.update(105000, T0 + timedelta(hours=9))
    assert s["legs"][f"L{a}"]["qty"] == pytest.approx(0.075)
    assert s["funding_paid"] > 0
    sm = ls.summary(105000)
    assert sm["n_long"] == 2 and sm["pnl"] > 0
    assert {"开多", "平多", "资金费"} <= {t["kind"] for t in ls.trades()}


def test_account_liquidation(lev):
    a = lev(1000)
    _order(a, "buy", 100000, 0.01, "a1")
    ls.update(100000, T0)
    s = ls.update(49900, T0)                     # 2 倍满仓，跌约 50% → 权益低于维持保证金
    assert not s["legs"] and s["cash"] < 200
    assert ls.trades(1)[0]["kind"] == "强平"


def test_short_leg_opens_and_covers_when_qualified(lev, monkeypatch):
    a = lev(1000)
    df5 = pd.DataFrame({"ts": [pd.Timestamp(T0)], "open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1.0]})
    monkeypatch.setattr(shortside, "status", lambda vid: {"ok": True})
    sig = {"bar": "b1", "open": True, "cover": False, "close": 100000.0, "sl": 3.0, "tp": None, "max_hold": None, "tf": 60}
    monkeypatch.setattr(shortside, "live_signal", lambda dep, d, now: dict(sig))
    s = ls.update(100000, T0, df5)
    assert s["legs"][f"S{a}"]["side"] == "short"
    s = ls.update(99000, T0 + timedelta(minutes=5), df5)            # 同一根K线不重复处理
    assert len([k for k in s["legs"] if k.startswith("S")]) == 1
    sig.update(bar="b2", open=False, cover=True)
    s = ls.update(98000, T0 + timedelta(hours=1), df5)
    assert f"S{a}" not in s["legs"] and s["cash"] > 10000            # 价格下跌 → 空头赚钱
    assert {"开空", "平空"} <= {t["kind"] for t in ls.trades()}
    # 做空止损：价格涨过 开空价 × (1 + 3%)
    sig.update(bar="b3", open=True, cover=False)
    ls.update(100000, T0 + timedelta(hours=2), df5)
    s = ls.update(103500, T0 + timedelta(hours=2, minutes=5), df5)
    assert f"S{a}" not in s["legs"] and "止损" in ls.trades(1)[0]["note"]


def test_inverted_short_version_profits_in_downtrend():
    long = {"name": "t", "signal_tf": 60,
            "indicators": [{"id": "f", "type": "EMA", "params": {"period": 3}},
                           {"id": "s", "type": "EMA", "params": {"period": 8}},
                           {"id": "r", "type": "RSI", "params": {"period": 14}},
                           {"id": "u", "type": "BB_UPPER", "params": {"period": 20, "std": 2}}],
            "entry": {"mode": "all", "conditions": [{"left": {"ref": "f"}, "op": "cross_above", "right": {"ref": "s"}},
                                                     {"left": {"ref": "r"}, "op": ">", "right": {"value": 30}}]},
            "exit": {"mode": "any", "conditions": [{"left": {"ref": "f"}, "op": "cross_below", "right": {"ref": "s"}},
                                                    {"left": {"ref": "close"}, "op": ">", "right": {"ref": "u"}}]},
            "stop_loss_pct": 5}
    inv = shortside.invert(long)
    assert inv["entry"]["conditions"][0]["op"] == "cross_below" and inv["entry"]["conditions"][1]["right"]["value"] == 70
    assert inv["indicators"][3]["type"] == "BB_LOWER" and inv["exit"]["conditions"][1]["op"] == "<"
    sch = shortside.inverted_schema(long)
    n = 300
    t = pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC")
    base = 100 * np.exp(np.cumsum(np.where(np.arange(n) % 40 < 30, -0.004, 0.003)))   # 总体下跌、间有反弹
    fr = pd.DataFrame({"close_time": t + pd.Timedelta(hours=1), "open": base, "high": base * 1.001, "low": base * 0.999,
                       "close": base})
    from aiquant.engine import indicators
    for ind in sch.indicators:
        fr[ind.id] = indicators.compute(fr, ind.type, ind.params).values
    r = shortside.simulate(fr, sch, fee=0.0007, funding_8h=0.0)
    assert r["trades"] >= 3 and r["return_pct"] > 0
    off = shortside.simulate(fr, sch, fee=0.0007, funding_8h=0.0, bear=np.zeros(n, dtype=bool))
    assert off["trades"] == 0                                      # 不是熊市就不开空
