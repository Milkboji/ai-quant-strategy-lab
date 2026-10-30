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


def _etf_holding(monkeypatch, qty=400.0, price=100.0, cash=60_000.0, target_pct=30.0):
    dep = reset_us(pos_qty=qty, price=price)
    db.set_setting("etf_trend_state", {"entry": {"signal_date": "2026-10-01", "target": qty * price,
                                                 "target_pct": target_pct}})
    fake = FakeUS(cash=cash, prices={"QLD": price})
    fake.pos["QLD"] = qty
    days = pd.bdate_range(end="2026-10-23", periods=200).date
    bars = pd.DataFrame({"date": days, "close": [100 + i * 0.2 for i in range(200)]})
    fake.install(monkeypatch, bars={"QQQ": bars, "QLD": pd.DataFrame({"date": days, "close": [price] * 200})})
    return dep, fake


def test_price_drift_not_rebalanced_target_increase_adds_only(env, monkeypatch):
    """(c) 只在目标比例变化时调整、只加不减：价格涨到 40%（目标 30%）不卖；目标提高到 40% → 下一交易日按差额加仓；
    目标降到 20% → 不卖，等下一次开仓按新目标。"""
    dep, fake = _etf_holding(monkeypatch)
    us_exec.step(datetime(2026, 10, 26, 14, 0, tzinfo=UTC))              # 实际 40%、目标 30%
    assert not fake.sent and et.get_why()["code"] == "hold"
    allocation.set_config(actor="t", role="trader", us_target=40)
    try:
        us_exec.step(datetime(2026, 10, 27, 14, 0, tzinfo=UTC))
        assert len(fake.sent) == 1 and fake.sent[0]["side"] == "buy" and abs(fake.sent[0]["notional"] - 10_000) < 1
        assert et.get_why()["code"] == "add"
        e = db.get_setting("etf_trend_state")["entry"]
        assert e["target_pct"] == pytest.approx(40) and e["target"] == pytest.approx(50_000)
        us_exec.sync_orders()
        allocation.set_config(actor="t", role="trader", us_target=20)
        us_exec.step(datetime(2026, 10, 28, 14, 0, tzinfo=UTC))
        assert len(fake.sent) == 1 and et.get_why()["code"] == "hold"           # 目标降低：不卖
    finally:
        allocation.set_config(actor="t", role="trader", us_target=30)


def test_position_cap_trims_us_strategy_to_50pct(env, monkeypatch):
    """单个美股策略持仓超过账户权益的 50%（可配置）→ 减到 50%。"""
    dep, fake = _etf_holding(monkeypatch, qty=700.0, cash=30_000.0)       # 70,000 / 100,000 = 70%
    us_exec.step(datetime(2026, 10, 26, 14, 0, tzinfo=UTC))
    assert fake.sent and fake.sent[0]["side"] == "sell" and abs(fake.sent[0]["qty"] - 200) < 1e-6
    assert et.get_why()["code"] == "cap"
    us_exec.sync_orders()
    assert us_ledger.position(dep["id"], "QLD")["qty"] == pytest.approx(500)
    allocation.set_config(actor="t", role="trader", position_cap_pct=60)
    try:
        assert allocation.position_cap(100_000) == 60_000
    finally:
        allocation.set_config(actor="t", role="trader", position_cap_pct=50)


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


def test_trial_budget_from_cash_layer_not_from_formal_strategies(env, monkeypatch):
    """新部署进入试用期：固定 5% 从现金层出、合计 ≤ 15%，现金层不够就为 0 并提示；正式策略的额度不变；满 60 个交易日转正式。"""
    from aiquant.paper import runner as pr
    reset_us()
    a, b, c, d = (_lab_dep(f"试用{i}") for i in range(4))
    for did in (a, b, c, d):
        dep = pr.get_deployment(did)
        db.ex("UPDATE paper_deployments SET config_json=? WHERE id=?",
              (json.dumps(allocation.start_trial(dep["config"], "us_equity", datetime(2026, 10, 1, 12, tzinfo=UTC)),
                          ensure_ascii=False), did))
    try:
        formal_before = allocation.plan_us(100_000)[us_exec.etf_deployment()["id"]]["budget"]
        tp = allocation.trial_plan(100_000)
        assert all(tp[x]["budget"] == 0 for x in (a, b, c, d)) and "现金层没有空余" in tp[a]["why"]
        assert "加密货币上限 %" in tp[a]["why"] and "调到 51%" in tp[a]["why"]
        assert "加密货币上限 %" in allocation.explain(100_000)["layer1"]["trial_hint"]
        allocation.set_config(actor="t", role="trader", crypto_max=46)     # 腾出 20% 现金层（现金保留 4% 之外）
        tp = allocation.trial_plan(100_000)
        assert [tp[x]["pct"] for x in (a, b, c, d)] == [5, 5, 5, 0]      # 合计不超过 15%
        assert tp[a]["status"]["text"].startswith("试用中（")
        plan = allocation.plan_us(100_000)
        assert plan[a]["budget"] == pytest.approx(5_000) and plan[a].get("trial")
        assert plan[us_exec.etf_deployment()["id"]]["budget"] == pytest.approx(formal_before)   # 正式策略不被摊薄
        st = allocation.trial_status(pr.get_deployment(a), datetime(2026, 12, 24, 19, tzinfo=UTC))
        assert st["trial"] is False and st.get("due")            # 10-01 起第 60 个交易日 = 12-24（半日市 13:00 收盘，跳过感恩节）
        assert allocation.trial_status(pr.get_deployment(a), datetime(2026, 12, 23, 22, tzinfo=UTC))["text"] == "试用中（59/60）"
        assert allocation.trial_status(pr.get_deployment(a), datetime(2026, 10, 30, 22, tzinfo=UTC))["text"] == "试用中（22/60）"
        out = allocation.graduate_trials(datetime(2026, 12, 24, 19, tzinfo=UTC))
        assert any(f"#{a}" in x for x in out)
        assert allocation.trial_status(pr.get_deployment(a))["text"] == "正式"
    finally:
        allocation.set_config(actor="t", role="trader", crypto_max=100)
        db.ex("UPDATE paper_deployments SET status='stopped' WHERE id IN (?,?,?,?)", (a, b, c, d))


def _stop_version(stop=5.0, tp=None, trailing=None):
    from aiquant.strategy import registry
    sch = {"name": "美股止损测试", "signal_tf": 1440, "signal_symbol": "QQQ",
           "indicators": [{"id": "ma", "type": "SMA", "params": {"period": 5}}],
           "entry": {"mode": "all", "conditions": [{"left": {"ref": "close"}, "op": ">", "right": {"ref": "ma"}}]},
           "exit": {"mode": "any", "conditions": [{"left": {"ref": "close"}, "op": "<", "right": {"ref": "ma"}}]},
           "stop_loss_pct": stop}
    if tp:
        sch["take_profit_pct"] = tp
    if trailing:
        sch["trailing"] = trailing
    return registry.freeze(None, sch, source="test")


def _stop_dep(monkeypatch, v, qty=100.0, ep=100.0, bars=None):
    reset_us()
    cfg = dict(db.get_setting("etf_trend_config") or {})
    db.set_setting("etf_trend_config", dict(cfg, enabled=False))
    did = db.ex("INSERT INTO paper_deployments(version_id,config_json,broker,status,created_at,updated_at,is_test) "
                "VALUES(?,?,?,?,?,?,0)", (v["id"], json.dumps({"kind": "lab", "label": "止损测试", "trade_symbol": "QLD",
                                                             "signal_symbol": "QQQ"}), "alpaca_us", "running",
                                          db.now_iso(), db.now_iso()))
    us_ledger.record_opening(did, "QLD", qty, ep, "测试")
    from aiquant.paper import runner as pr
    us_exec._save_state(pr.get_deployment(did), {"entry": {"signal_date": "2026-10-19", "target": qty * ep, "target_pct": 10}})
    fake = FakeUS(cash=90_000, prices={"QLD": ep})
    fake.pos["QLD"] = qty
    fake.install(monkeypatch)
    b = bars if bars is not None else []
    monkeypatch.setattr(us_exec, "lab_bars", lambda sym: pd.DataFrame(
        {"close_time": pd.to_datetime([f"{d}T20:00:00Z" for d, _, _ in b], utc=True),
         "low": [x for _, x, _ in b], "high": [h for _, _, h in b], "close": [x for _, x, _ in b]}))
    return did, fake, cfg


def test_broker_stop_order_placed_canceled_before_exit_and_fill_recorded(env, monkeypatch):
    """止损：持仓后在券商挂当日有效的止损单（止损价 = 入场价 × (1 − 5%)）；收盘后挂下一交易日的；
    自己卖出前先撤单；止损单成交 → 台账卖出、记离场。"""
    v = _stop_version(5.0)
    did, fake, cfg = _stop_dep(monkeypatch, v, bars=[("2026-10-20", 99.0, 101.0)])
    from aiquant.paper import runner as pr
    try:
        out = us_exec.maintain_stops(datetime(2026, 10, 21, 14, 0, tzinfo=UTC))
        assert out and fake.sent[-1]["type"] == "stop" and fake.sent[-1]["stop"] == pytest.approx(95.0)
        assert us_ledger.open_stop(did)["trade_date"] == "2026-10-21"
        assert not us_ledger.has_open(did)                                   # 挂着的止损单不挡住正常动作
        assert us_exec.maintain_stops(datetime(2026, 10, 21, 15, 0, tzinfo=UTC)) == []   # 已挂好，不重复
        # 收盘后：挂下一交易日的（跳空低开时开盘就触发）
        us_exec.maintain_stops(datetime(2026, 10, 21, 21, 0, tzinfo=UTC))
        assert us_ledger.open_stop(did)["trade_date"] == "2026-10-22" and len(fake.canceled) == 1
        # 次日跳空低开到 90（低于止损价 95）：按开盘价 90 成交
        fake.trigger_stops("QLD", low=89.0, open_px=90.0)
        us_exec.sync_orders()
        assert us_ledger.position(did, "QLD")["qty"] == 0
        row = db.q1("SELECT price, source FROM us_ledger WHERE deployment_id=? AND side='sell'", (did,))
        assert row["price"] == pytest.approx(90.0) and row["source"] == "fill"
        assert us_exec.get_why(pr.get_deployment(did))["code"] == "stopped"
        assert us_exec.dep_state(pr.get_deployment(did))["entry"] is None
    finally:
        db.ex("UPDATE paper_deployments SET status='stopped' WHERE id=?", (did,))
        db.set_setting("etf_trend_config", cfg)


def test_exit_signal_cancels_resting_stop_first(env, monkeypatch):
    v = _stop_version(5.0)
    did, fake, cfg = _stop_dep(monkeypatch, v, bars=[("2026-10-20", 99.0, 101.0)])
    try:
        us_exec.maintain_stops(datetime(2026, 10, 21, 13, 0, tzinfo=UTC))           # 开盘前挂好
        _signals(monkeypatch, {did: 0})
        us_exec.step(datetime(2026, 10, 21, 14, 0, tzinfo=UTC))                    # 看空：卖出全部
        assert len(fake.canceled) == 1 and fake.sent[-1]["side"] == "sell" and fake.sent[-1].get("type") is None
        us_exec.sync_orders()
        assert us_ledger.position(did, "QLD")["qty"] == 0 and not fake.resting
    finally:
        db.ex("UPDATE paper_deployments SET status='stopped' WHERE id=?", (did,))
        db.set_setting("etf_trend_config", cfg)


def test_stop_falls_back_to_polling_when_broker_rejects_and_take_profit_polled(env, monkeypatch):
    """券商拒绝止损单（碎股不支持）→ 改为交易时段内每 5 分钟检查最新价，触发后立即下市价单；止盈同样轮询。"""
    from aiquant.paper import runner as pr
    v = _stop_version(5.0, tp=10.0)
    did, fake, cfg = _stop_dep(monkeypatch, v, bars=[("2026-10-20", 99.0, 101.0)])
    fake.reject_stops = "Alpaca Paper 拒绝订单 422: fractional orders must be market orders"
    import aiquant.notify as nt
    monkeypatch.setattr(nt, "event", lambda *a, **k: 1)
    try:
        us_exec.maintain_stops(datetime(2026, 10, 21, 14, 0, tzinfo=UTC))
        assert pr.get_deployment(did)["config"]["stop_mode"] == "poll"
        db.set_setting("us_guard_last", None)
        fake.prices["QLD"] = 96.0
        assert us_exec.intraday_guard(datetime(2026, 10, 21, 14, 5, tzinfo=UTC)) == []     # 96 > 95：不触发
        fake.prices["QLD"] = 94.5
        assert us_exec.intraday_guard(datetime(2026, 10, 21, 14, 7, tzinfo=UTC)) == []     # 不到 5 分钟：不检查
        out = us_exec.intraday_guard(datetime(2026, 10, 21, 14, 11, tzinfo=UTC))
        assert out and "止损价" in out[0] and fake.sent[-1]["side"] == "sell"
        us_exec.sync_orders()
        assert us_ledger.position(did, "QLD")["qty"] == 0
        assert us_exec.get_why(pr.get_deployment(did))["code"] == "stopped"
    finally:
        db.ex("UPDATE paper_deployments SET status='stopped' WHERE id=?", (did,))
        db.set_setting("etf_trend_config", cfg)


def test_trailing_stop_path_matches_backtest(env, monkeypatch):
    """移动止损按路径逐日推进：第 j 天的止损只用第 j 天之前的最高价（与回测相同）。"""
    v = _stop_version(None, trailing={"activate_pct": 5.0, "trail_pct": 3.0})
    bars = [("2026-10-20", 99.0, 104.0), ("2026-10-21", 103.0, 110.0), ("2026-10-22", 106.0, 108.0)]
    did, fake, cfg = _stop_dep(monkeypatch, v, bars=bars)
    from aiquant.paper import runner as pr
    try:
        dep = pr.get_deployment(did)
        assert us_exec.stop_plan(dep, pd.Timestamp("2026-10-21").date())["stop"] is None   # 峰值 104 < 激活线 105
        sp = us_exec.stop_plan(dep, pd.Timestamp("2026-10-22").date())
        assert sp["stop"] == pytest.approx(110 * 0.97) and sp["hit"] is None           # 10-22 的止损：峰值到 10-21 为 110
        sp = us_exec.stop_plan(dep, pd.Timestamp("2026-10-23").date())
        assert sp["hit"] and sp["hit"].startswith("移动止损")                              # 10-22 最低 106 ≤ 106.70 → 触发
    finally:
        db.ex("UPDATE paper_deployments SET status='stopped' WHERE id=?", (did,))
        db.set_setting("etf_trend_config", cfg)
