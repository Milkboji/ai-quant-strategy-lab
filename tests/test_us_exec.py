"""通用美股日线执行器：按策略分账、合并下单可拆回、对账、迁移不下单、两层资金、偏离再平衡。全部离线（假券商）。"""
import json
from datetime import datetime, timezone

import pandas as pd
import pytest

from aiquant import allocation, db
from aiquant.paper import etf_trend as et
from aiquant.paper import us_exec, us_ledger
from tests.us_fake import FakeUS, reset_us

UTC = timezone.utc


def _lab_dep(label: str, sym: str = "QLD", sig: str = "QQQ") -> int:
    cfg = {"kind": "lab", "strategy_key": f"test:{label}", "label": label, "market": "us_equity", "trade_symbol": sym,
           "signal_symbol": sig, "signal_tf": 1440, "portfolio": True}
    return db.ex("INSERT INTO paper_deployments(version_id,config_json,broker,status,created_at,updated_at,is_test) "
                 "VALUES(0,?,?,?,?,?,0)", (json.dumps(cfg, ensure_ascii=False), "alpaca_us", "running", db.now_iso(),
                                           db.now_iso()))


def _signals(monkeypatch, table: dict):
    """table：{部署: regime 或 (regime, 日期)}；收盘价随意（实验室信号由主引擎算，这里只测执行）。"""
    def fake(dep, today, holding):
        v = table[dep["id"]]
        reg = v if isinstance(v, int) else v[0]
        return {"date": str(pd.Timestamp(today) - pd.offsets.BDay(1))[:10], "regime": reg, "entry": bool(reg),
                "exit": not reg, "close": 100.0, "rule": "测试", "stop": None}
    monkeypatch.setattr(us_exec, "signal_for", fake)


@pytest.fixture()
def two_labs(env, monkeypatch):
    reset_us()
    cfg = dict(db.get_setting("etf_trend_config") or {})
    db.set_setting("etf_trend_config", dict(cfg, enabled=False))          # 只看两个实验室部署
    a, b = _lab_dep("美股A"), _lab_dep("美股B")
    fake = FakeUS().install(monkeypatch)
    yield fake, a, b
    db.ex("UPDATE paper_deployments SET status='stopped' WHERE id IN (?,?)", (a, b))
    db.set_setting("etf_trend_config", dict(cfg, enabled=True))


def test_two_strategies_same_symbol_merged_order_split_back_and_reconciled(two_labs, monkeypatch):
    fake, a, b = two_labs
    _signals(monkeypatch, {a: 1, b: 1})
    # 第 1 天：两个策略都要买 QLD → 一笔合并的券商订单（3 万 = 美股 30% × 10 万，两个策略各一半），只成交一半
    fake.fill_ratio["QLD"] = 0.5
    us_exec.step(datetime(2026, 10, 20, 14, 0, tzinfo=UTC))
    assert len(fake.sent) == 1 and fake.sent[0]["side"] == "buy" and abs(fake.sent[0]["notional"] - 30_000) < 1
    allocs = db.q("SELECT * FROM us_order_allocs ORDER BY id")
    assert sorted(x["deployment_id"] for x in allocs) == [a, b] and all(abs(x["req_notional"] - 15_000) < 1 for x in allocs)
    us_exec.sync_orders()                                          # 订单过期（部分成交）→ 按金额比例拆回
    pa, pb = us_ledger.position(a, "QLD"), us_ledger.position(b, "QLD")
    assert abs(pa["qty"] - 75) < 1e-6 and abs(pb["qty"] - 75) < 1e-6 and pa["avg_cost"] == 100
    rec = us_ledger.reconcile({"QLD": fake.pos["QLD"]})
    assert rec["QLD"]["ok"] and rec["QLD"]["by_strategy"] == {a: pytest.approx(75), b: pytest.approx(75)}
    # 第 2 天：各补足到目标（部分成交次日补足），价格 110，再合并成一笔
    fake.fill_ratio["QLD"] = 1.0
    fake.prices["QLD"] = 110.0
    us_exec.step(datetime(2026, 10, 21, 14, 0, tzinfo=UTC))
    assert len(fake.sent) == 2 and abs(fake.sent[1]["notional"] - 15_000) < 1
    us_exec.sync_orders()
    pa = us_ledger.position(a, "QLD")
    assert abs(pa["qty"] - (75 + 7500 / 110)) < 1e-6
    assert abs(pa["avg_cost"] - 15_000 / (75 + 7500 / 110)) < 1e-6           # 移动平均成本
    assert us_ledger.reconcile({"QLD": fake.pos["QLD"]})["QLD"]["ok"]
    # 第 3 天：A 看空卖出全部，B 继续持有 → 卖单只卖 A 的股数，B 的持仓和成本不变
    _signals(monkeypatch, {a: 0, b: 1})
    fake.prices["QLD"] = 120.0
    qa = pa["qty"]
    us_exec.step(datetime(2026, 10, 22, 14, 0, tzinfo=UTC))
    assert fake.sent[-1]["side"] == "sell" and abs(fake.sent[-1]["qty"] - qa) < 1e-6
    us_exec.sync_orders()
    pa, pb = us_ledger.position(a, "QLD"), us_ledger.position(b, "QLD")
    assert pa["qty"] == 0 and abs(pa["realized"] - (120 * qa - 15_000)) < 1e-4       # A 的已实现盈亏
    assert abs(pb["qty"] - qa) < 1e-6 and pb["realized"] == 0
    assert us_ledger.reconcile({"QLD": fake.pos["QLD"]})["QLD"]["ok"]
    assert fake.min_cash >= 0                                                     # 全程不出现负现金
    # 「为什么」按部署分开
    assert us_exec.get_why(us_exec._dep_for(a))["code"] == "sold" and us_exec.get_why(us_exec._dep_for(b))["code"] == "hold"


def test_net_order_and_internal_cross_split_exactly():
    """同一天一个策略卖、另一个买：只发净额订单，相抵部分按同一成交价内部成交；完全相抵时不发券商单。"""
    us_ledger.ensure_tables()
    for t in ("us_orders", "us_order_allocs", "us_ledger"):
        db.ex(f"DELETE FROM {t}")
    us_ledger.record_opening(901, "SSO", 100, 50.0, "测试")
    its = [{"deployment_id": 901, "side": "sell", "qty": 100.0, "purpose": "exit"},
           {"deployment_id": 902, "side": "buy", "notional": 2_500.0, "purpose": "entry"}]
    plan = us_ledger.plan_net(its, 50.0)
    assert plan["side"] == "sell" and plan["qty"] == 50 and plan["cross_qty"] == 50
    oid = us_ledger.create_order("SSO", plan, its, "2026-10-21", 50.0, "c-1")
    us_ledger.update_order(oid, "filled", 50.0, 51.0)
    out = us_ledger.allocate(oid)
    assert {(r["deployment_id"], r["side"], round(r["qty"], 6), r["source"]) for r in out} == \
        {(901, "sell", 100.0, "fill"), (902, "buy", 50.0, "cross")}
    assert us_ledger.allocate(oid) == []                                  # 幂等
    assert us_ledger.reconcile({"SSO": 50.0})["SSO"]["ok"]                # 券商卖出 50 股 = 台账净变化
    assert us_ledger.position(901, "SSO")["realized"] == pytest.approx(100.0)   # (51 − 50) × 100
    # 完全相抵：不发券商单，按参考价内部成交
    its = [{"deployment_id": 902, "side": "sell", "qty": 50.0, "purpose": "exit"},
           {"deployment_id": 903, "side": "buy", "notional": 2_600.0, "purpose": "entry"}]
    plan = us_ledger.plan_net(its, 52.0)
    assert plan["side"] is None
    oid = us_ledger.create_order("SSO", plan, its, "2026-10-22", 52.0, None)
    us_ledger.allocate(oid)
    assert us_ledger.reconcile({"SSO": 50.0})["SSO"]["ok"]
    assert us_ledger.position(903, "SSO")["qty"] == pytest.approx(50.0) and us_ledger.position(902, "SSO")["qty"] == 0


def test_reconcile_mismatch_blocks_symbol(two_labs, monkeypatch):
    fake, a, b = two_labs
    _signals(monkeypatch, {a: 1, b: 1})
    us_ledger.record_opening(a, "QLD", 10, 100.0, "测试")         # 台账 10 股，券商 0 股
    import aiquant.notify as nt
    sent = []
    monkeypatch.setattr(nt, "event", lambda kind, title, body="", dedupe=None: sent.append(kind) or 1)
    us_exec.step(datetime(2026, 10, 20, 14, 0, tzinfo=UTC))
    assert not fake.sent and us_exec.get_why(us_exec._dep_for(a))["code"] == "recon" and "order_failed" in sent


def test_rejected_merged_order_not_retried_and_each_strategy_told(two_labs, monkeypatch):
    fake, a, b = two_labs
    _signals(monkeypatch, {a: 1, b: 1})
    fake.reject.add("QLD")
    now = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
    us_exec.step(now)
    us_exec.step(now)
    assert not fake.sent
    for d in (a, b):
        assert us_exec.get_why(us_exec._dep_for(d))["code"] == "rejected"
        assert us_exec.dep_state(us_exec._dep_for(d))["last_action_date"] == "2026-10-20"
    assert db.q1("SELECT COUNT(*) AS n FROM us_orders WHERE status='rejected'")["n"] == 1


def test_etf_migration_keeps_state_and_places_no_orders(env, monkeypatch):
    """原 ETF 择时 → 通用执行器上的普通部署：持仓记为台账期初、上次动作日 / 节奏 / 借出状态原样沿用，不下任何单。"""
    from aiquant.paper import runner as pr
    reset_us()
    for r in db.q("SELECT id FROM paper_deployments WHERE broker='alpaca_us'"):
        db.ex("DELETE FROM paper_deployments WHERE id=?", (r["id"],))
    db.set_setting("us_exec_migration", None)
    pos = {"qty": 330.938127274, "market_value": 30049.18, "avg_entry_price": 90.65888, "unrealized_pl": 46.7}
    db.set_setting("etf_trend_state", {"last_action_date": "2026-10-28", "signal": {"date": "2026-10-27", "regime": 1},
                                       "action": "保持", "position": pos,
                                       "entry": {"signal_date": "2026-10-20", "target": 30002.49}})
    db.set_setting("rhythm_state::etf", {"block_until": None, "streak": 0, "entries_day": {}})
    fake = FakeUS(cash=69_700.0, prices={"QLD": 90.8})
    fake.pos["QLD"] = pos["qty"]
    days = pd.bdate_range(end="2026-10-28", periods=200).date
    bars = pd.DataFrame({"date": days, "close": [100 + i * 0.2 for i in range(200)]})
    fake.install(monkeypatch, bars={"QQQ": bars, "QLD": bars})
    rep = us_exec.migrate_etf()
    assert rep["checks"] == {"position_same": True, "last_action_kept": True, "lend_kept": True}
    assert rep["orders_submitted_by_migration"] == 0 and not fake.sent
    dep = us_exec.etf_deployment()
    assert dep["broker"] == "alpaca_us" and dep["config"]["kind"] == "etf_sma" and dep["status"] == "running"
    assert us_ledger.position(dep["id"], "QLD")["qty"] == pytest.approx(pos["qty"])
    assert us_exec.migrate_etf() == rep                                        # 幂等
    # 当天（已动作过）再检查：不下单；下一个交易日：看多且持有、偏离 < 5 个百分点 → 保持
    us_exec.step(datetime(2026, 10, 28, 15, 0, tzinfo=UTC))
    assert not fake.sent
    us_exec.step(datetime(2026, 10, 29, 14, 0, tzinfo=UTC))
    assert not fake.sent and et.get_why()["code"] == "hold"
    assert db.get_setting("etf_trend_state")["last_action_date"] == "2026-10-29"
    assert any(a["action"] == "迁移到通用美股执行器" for a in __import__("aiquant.console", fromlist=["x"]).audit_log(20, key="etf"))
    from aiquant.paper import etf_versions
    assert pr.get_deployment(dep["id"])["version_id"] == etf_versions.running()["id"]


def test_deviation_over_threshold_rebalances_next_trading_day(env, monkeypatch):
    """ETF 择时持仓涨到 40%（目标 30%）：收盘后记下偏离 10 个百分点 → 下一交易日开盘后卖出多出的部分。"""
    dep = reset_us(pos_qty=400.0, price=100.0)
    fake = FakeUS(cash=60_000.0, prices={"QLD": 100.0})
    fake.pos["QLD"] = 400.0
    days = pd.bdate_range(end="2026-10-23", periods=200).date
    bars = pd.DataFrame({"date": days, "close": [100 + i * 0.2 for i in range(200)]})
    qld = pd.DataFrame({"date": days, "close": [100.0] * 200})
    fake.install(monkeypatch, bars={"QQQ": bars, "QLD": qld})
    monkeypatch.setattr(us_exec, "lab_bars", lambda sym: (_ for _ in ()).throw(RuntimeError("no local")))
    out = us_exec.after_close(datetime(2026, 10, 23, 20, 30, tzinfo=UTC))     # 周五收盘后
    info = out["deployments"][dep["id"]]
    assert info["actual_pct"] == 40 and info["target_pct"] == 30 and info["dev_pp"] == 10
    assert us_exec.dep_state(dep)["rebalance_due"]["date"] == "2026-10-23"
    assert out["next"] == "2026-10-26"
    us_exec.step(datetime(2026, 10, 26, 14, 0, tzinfo=UTC))                   # 下周一开盘后
    assert fake.sent and fake.sent[0]["side"] == "sell" and abs(fake.sent[0]["qty"] - 100) < 1e-6
    assert et.get_why()["code"] == "rebalance" and "rebalance_due" not in db.get_setting("etf_trend_state")
    us_exec.sync_orders()
    assert us_ledger.position(dep["id"], "QLD")["qty"] == pytest.approx(300.0)
    # 阈值可配置：改成 15 个百分点后，同样的偏离不再再平衡
    allocation.set_config(actor="t", role="trader", rebalance_pp=15)
    try:
        assert allocation.get_config()["rebalance_pp"] == 15
    finally:
        allocation.set_config(actor="t", role="trader", rebalance_pp=5)


def test_two_layer_allocation_and_explain(env, monkeypatch):
    reset_us()
    l1 = allocation.layer1(100_000)
    assert l1["us_target_pct"] == 30 and l1["crypto_pct"] == pytest.approx(100 - l1["reserve_pct"] - 30)
    assert len(l1["steps"]) == 5 and "美股实际占用" in l1["steps"][2]
    assert et.capital_share(100_000) == pytest.approx(l1["us_deduct_pct"])
    with pytest.raises(PermissionError):
        allocation.set_config(actor="g", role="viewer", us_max=20)
    allocation.set_config(actor="t", role="trader", us_max=20, crypto_max=60)
    try:
        l1 = allocation.layer1(100_000)
        assert l1["us_target_pct"] == 20 and l1["crypto_pct"] == 60 and l1["cash_pct"] == pytest.approx(20)
        ex = allocation.explain(100_000)
        assert ex["layer1"]["us_target_pct"] == 20 and ex["us"] and ex["us"][0]["budget"] == pytest.approx(20_000)
        from aiquant.console import audit_log
        assert any(a["action"] == "修改资金分层" for a in audit_log(10, key="capital"))
        # 借出时：美股目标的一半给加密货币（仍受加密货币上限约束）
        db.set_setting("etf_lend", {"state": "lent", "since": db.now_iso(), "lend_pct": 50})
        l1 = allocation.layer1(100_000)
        assert l1["lent_pct"] == 10 and l1["us_deduct_pct"] == 10 and l1["crypto_pct"] == 60
    finally:
        db.set_setting("etf_lend", None)
        allocation.set_config(actor="t", role="trader", us_max=60, crypto_max=100)


def test_console_page_shows_capital_layers_and_us_lab_row(env):
    """策略总台：资金两层分配面板（观察员只读）；实验室美股版本的部署显示在原策略位那一行（美股日线 · 标的）。"""
    from streamlit.testing.v1 import AppTest

    from aiquant import console
    reset_us()
    did = _lab_dep("美股页面测试")
    db.ex("UPDATE paper_deployments SET config_json=json_set(config_json,'$.strategy_key','slot:5') WHERE id=?", (did,))
    try:
        r = next(x for x in console.rows() if x["key"] == "slot:5")
        assert r["deployment"]["id"] == did and r["market"] == "us_equity" and r["trade_symbol"] == "QLD"
        assert r["why"] and "headline" in r["why"]

        def app():
            import streamlit as st

            from aiquant import auth
            from aiquant.ui import login, page_console
            auth.ensure()
            login._install_readonly_guard()
            st.session_state["auth_user"] = "guest_viewer"
            st.session_state["_aq_readonly"] = True
            page_console.render()

        at = AppTest.from_function(app, default_timeout=120)
        at.run()
        assert not at.exception
        assert any("资金两层分配" in e.label for e in at.expander)
        assert any("第一层" in m.value for m in at.markdown)
        save = [b for b in at.button if b.key == "cap_save"]
        assert save and save[0].disabled
    finally:
        db.ex("UPDATE paper_deployments SET status='stopped' WHERE id=?", (did,))
