"""2 倍杠杆模拟账户：开平仓、手续费、资金费、强平。"""
from datetime import datetime, timezone

from aiquant import db
from aiquant.paper import leverage_shadow as ls


def _order(did, side, px, oid_suffix):
    db.ex("INSERT INTO paper_orders(deployment_id,client_order_id,broker,side,qty,status,filled_qty,filled_price,"
          "signal_ts,created_at,updated_at,is_test) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
          (did, f"lev-{did}-{oid_suffix}", "mock", side, 0.1, "filled", 0.1, px, "x", db.now_iso(), db.now_iso()))


def test_shadow_follows_trades_with_leverage(env):
    ls.ensure_tables()
    did = 9001
    db.set_setting("lev_shadow_config", {"source_deployment": did, "margin0": 10000.0})
    db.set_setting("lev_shadow_state", None)
    db.ex("DELETE FROM settings WHERE key='lev_shadow_state'")
    t0 = datetime(2026, 10, 21, 1, 0, tzinfo=timezone.utc)
    ls.update(100000, t0)                          # 初始化：从现在起跟随
    _order(did, "buy", 100000, "b")
    s = ls.update(100000, t0)
    assert abs(s["qty"] - (10000 - 14) * 2 / 100000) < 1e-9        # 2 倍名义，扣 0.07% 手续费
    s = ls.update(101000, datetime(2026, 10, 21, 9, 1, tzinfo=timezone.utc))   # 跨过 08:00 资金费时点
    assert s["funding_paid"] > 0
    _order(did, "sell", 105000, "s")
    s = ls.update(105000, datetime(2026, 10, 21, 10, tzinfo=timezone.utc))
    assert s["qty"] == 0
    # 价格涨 5%，2 倍杠杆约 +10%，扣手续费和资金费后仍应 > +9%
    assert s["cash"] > 10900
    kinds = [t["kind"] for t in ls.trades()]
    assert {"开多", "平多", "资金费"} <= set(kinds)


def test_shadow_liquidation(env):
    did = 9002
    db.set_setting("lev_shadow_config", {"source_deployment": did, "margin0": 10000.0})
    db.ex("DELETE FROM settings WHERE key='lev_shadow_state'")
    t0 = datetime(2026, 10, 22, 1, tzinfo=timezone.utc)
    ls.update(100000, t0)
    _order(did, "buy", 100000, "b")
    ls.update(100000, t0)
    lp = ls.liq_price(100000, 2.0, 0.004)
    s = ls.update(lp - 1, t0)
    assert s["qty"] == 0 and s["cash"] == 0
    assert ls.trades(1)[0]["kind"] == "强平"
