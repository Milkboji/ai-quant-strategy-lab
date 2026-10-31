"""策略自动进化：候选生成、分段评分、一轮进化、空仓才切换。全部使用合成测试数据与 Mock 部署。"""
import json
from datetime import timedelta

import pytest

from aiquant import db, evolve, research
from aiquant.data import market_data as md
from aiquant.paper import runner as pr
from aiquant.strategy import registry
from aiquant.strategy.schema import schema_hash, validate
from aiquant.strategy.templates import TEMPLATES


@pytest.fixture(scope="module")
def evo_env(env):
    sp = env
    # 训练窗口 = 验证集开始前 365 天，再加 60 天指标预热
    md.install_synthetic_test_data(md.to_utc(sp["val_start"]) - timedelta(days=440), md.to_utc(sp["test_end"]))
    evolve.set_config(max_candidates=25, use_deepseek=True, auto_apply=True)
    return sp


def _template_schema(i=0) -> dict:
    t = list(TEMPLATES.values())[i]
    from aiquant.strategy.parser import parse
    sch, errs, _ = parse(t["text"]) if "text" in t else (t["schema"], [], None)
    return sch


def _deploy(schema: dict, tf: int) -> int:
    schema = dict(schema, signal_tf=tf)
    v = registry.freeze(None, schema, source="test", new_lineage=True, set_current=False)
    did = pr.save_deployment(v["id"], {"label": f"测试·{v['name']}", "signal_tf": tf, "aux_tf": schema.get("aux_tf"),
                                       "alloc_pct": 100}, "mock")
    db.ex("UPDATE paper_deployments SET status='running', is_test=1 WHERE id=?", (did,))
    return did


def test_mutations_are_valid_and_distinct():
    base = {"name": "t", "signal_tf": 60, "indicators": [
        {"id": "f", "type": "EMA", "params": {"period": 10}}, {"id": "s", "type": "EMA", "params": {"period": 30}},
        {"id": "r", "type": "RSI", "params": {"period": 14}}],
        "entry": {"mode": "all", "conditions": [{"left": {"ref": "f"}, "op": "cross_above", "right": {"ref": "s"}},
                                                {"left": {"ref": "r"}, "op": "<", "right": {"value": 70}}]},
        "exit": {"mode": "any", "conditions": [{"left": {"ref": "f"}, "op": "cross_below", "right": {"ref": "s"}}]},
        "stop_loss_pct": 3}
    obj, errs = validate(base)
    assert not errs
    muts = evolve.mutations(obj.model_dump())
    assert len(muts) >= 15
    hashes = {schema_hash(m) for m, _ in muts}
    assert schema_hash(obj) not in hashes            # 不包含自身
    for m, why in muts:
        assert not validate(m)[1] and why
    tfs = {m["signal_tf"] for m, _ in muts}
    assert 240 in tfs                                # 相邻K线周期
    assert 30 not in tfs                             # 交易节奏：不往 1 小时以下进化


def test_segment_returns_and_score():
    from datetime import datetime, timezone
    s, e = datetime(2025, 1, 1, tzinfo=timezone.utc), datetime(2025, 1, 5, tzinfo=timezone.utc)
    eq = [["2025-01-01T00:00:00+00:00", 100], ["2025-01-02T00:00:00+00:00", 110],
          ["2025-01-03T00:00:00+00:00", 99], ["2025-01-04T00:00:00+00:00", 99], ["2025-01-04T23:00:00+00:00", 108.9]]
    segs = evolve.segment_returns(eq, s, e)
    assert segs == [10.0, -10.0, 0.0, 10.0]
    assert evolve.score({"max_drawdown_pct": -10}, segs) < evolve.score({"max_drawdown_pct": -2}, segs)


def test_evolve_round_records_and_switches_only_when_flat(evo_env):
    v_sch = {"name": "进化测试", "indicators": [
        {"id": "f", "type": "EMA", "params": {"period": 9}}, {"id": "s", "type": "EMA", "params": {"period": 21}}],
        "entry": {"mode": "all", "conditions": [{"left": {"ref": "f"}, "op": "cross_above", "right": {"ref": "s"}}]},
        "exit": {"mode": "any", "conditions": [{"left": {"ref": "f"}, "op": "cross_below", "right": {"ref": "s"}}]},
        "stop_loss_pct": 1.0, "take_profit_pct": 2.0}
    did = _deploy(v_sch, 15)
    old_vid = pr.get_deployment(did)["version_id"]
    rid = evolve.run({"params_json": json.dumps({"trigger": "测试"}), "is_test": 1}, lambda m: None, lambda: False)
    rows = evolve.results_of(rid)
    mine = [r for r in rows if r["deployment_id"] == did]
    assert mine, rows
    r = mine[0]
    assert r["decision"] in ("替换", "保留", "等待新数据", "待人工确认"), r
    assert r["candidates"] > 0 and r["current"] is not None
    run = db.q1("SELECT * FROM evolve_runs WHERE id=?", (rid,))
    assert run["status"] == "done"
    # 独立测试集从未被进化触及
    assert not db.q("SELECT * FROM holdout_touches WHERE split IN ('test_contaminated','final_test') "
                    "AND version_id IN (SELECT id FROM strategy_versions WHERE source='auto_evolve')")
    d = pr.get_deployment(did)
    if r["decision"] == "替换":
        nv = registry.get_version(r["new_version_id"])
        assert nv["source"] == "auto_evolve" and nv["parent_id"] == old_vid
        assert d["version_id"] == nv["id"]           # 空仓 → 立即切换
        assert research.latest_backtest_for_version(nv["id"], kind_split="训练集")
        assert "pending_switch" not in d["config"]
    else:
        assert d["version_id"] == old_vid
    ev = db.q("SELECT message FROM paper_events WHERE deployment_id=? AND message LIKE '自动进化%'", (did,))
    assert ev


def test_pending_switch_waits_for_flat_position(evo_env):
    sch = {"name": "持仓切换测试", "indicators": [
        {"id": "f", "type": "EMA", "params": {"period": 9}}, {"id": "s", "type": "EMA", "params": {"period": 21}}],
        "entry": {"mode": "all", "conditions": [{"left": {"ref": "f"}, "op": "cross_above", "right": {"ref": "s"}}]},
        "exit": {"mode": "any", "conditions": [{"left": {"ref": "f"}, "op": "cross_below", "right": {"ref": "s"}}]}}
    did = _deploy(sch, 60)
    old = pr.get_deployment(did)["version_id"]
    nv = registry.freeze(None, dict(sch, signal_tf=60, stop_loss_pct=3.0), source="auto_evolve", parent_id=old,
                         set_current=False)
    # 新版本需要有成交的回测才能通过部署门禁
    sp = research.get_split()
    from aiquant.engine import backtest as bt
    s, e = md.to_utc(sp["val_start"]) - timedelta(days=200), md.to_utc(sp["val_start"])
    ds = md.get_dataset(s, e, source="test")
    ev = evolve.evaluate(md.load_dataset(ds), nv["schema"], s, e, dict(research.get_research(), source="test"))
    research.store_backtest(job_id=None, version=nv, slot_no=None, ds=ds, start=s, end=e, tf=60, aux=None,
                            split="训练集", cfg=ev["cfg"], res=ev["res"], is_test=1)
    db.ex("INSERT INTO paper_orders(deployment_id,client_order_id,broker,side,qty,status,filled_qty,filled_price,"
          "signal_ts,created_at,updated_at,is_test) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
          (did, f"t-{did}-b", "mock", "buy", 0.01, "filled", 0.01, 50000, "x", db.now_iso(), db.now_iso()))
    evolve.schedule_switch(did, nv["id"], 60, None, "测试")
    d = pr.get_deployment(did)
    assert d["version_id"] == old and d["config"]["pending_switch"]["version_id"] == nv["id"]   # 有持仓：不切换
    assert not evolve.apply_pending(did)
    db.ex("INSERT INTO paper_orders(deployment_id,client_order_id,broker,side,qty,status,filled_qty,filled_price,"
          "signal_ts,created_at,updated_at,is_test) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
          (did, f"t-{did}-s", "mock", "sell", 0.01, "filled", 0.01, 51000, "y", db.now_iso(), db.now_iso()))
    assert evolve.apply_pending(did)                 # 平仓后：切换
    d = pr.get_deployment(did)
    assert d["version_id"] == nv["id"] and "pending_switch" not in d["config"]
    h = d["config"]["evolve_history"][-1]
    assert h == {"from": old, "to": nv["id"], "at": h["at"], "trigger": "evolve"}


def test_schedule_due_logic(evo_env):
    from datetime import datetime, timezone
    evolve.set_config(enabled=True, interval_days=7)
    db.set_setting("evolve_last_run", {"at": "2026-01-01T00:00:00+00:00"})
    assert evolve.due(datetime(2026, 1, 8, 1, tzinfo=timezone.utc))
    assert not evolve.due(datetime(2026, 1, 5, tzinfo=timezone.utc))
    evolve.set_config(enabled=False)
    assert not evolve.due(datetime(2026, 2, 1, tzinfo=timezone.utc))
    evolve.set_config(enabled=True)


def _fake_result(dep_id, vid, ret, decision="保留", new_vid=None):
    db.ex("INSERT INTO evolve_runs(trigger,status,started_at,is_test) VALUES('测试','done',?,1)", (db.now_iso(),))
    rid = db.q1("SELECT MAX(id) AS m FROM evolve_runs")["m"]
    brief = json.dumps({"return_pct": ret, "score": 0, "max_dd_pct": -5, "trades": 10, "segments": [0, 0, 0, 0],
                        "positive_segments": 0, "tf": 60})
    db.ex("INSERT INTO evolve_results(run_id,deployment_id,label,current_version_id,candidates,current_json,best_json,"
          "decision,reason,new_version_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
          (rid, dep_id, "t", vid, 1, brief if not new_vid else json.dumps({"return_pct": -50}),
           brief if new_vid else None, decision, "测试", new_vid, db.now_iso()))


def test_auto_pause_and_resume_long_term_losers(evo_env):
    evolve.set_config(pause_losers=True, pause_train_loss_pct=-10.0, pause_live_loss_pct=-10.0)
    sch = {"name": "亏损暂停测试", "indicators": [
        {"id": "f", "type": "EMA", "params": {"period": 9}}, {"id": "s", "type": "EMA", "params": {"period": 21}}],
        "entry": {"mode": "all", "conditions": [{"left": {"ref": "f"}, "op": "cross_above", "right": {"ref": "s"}}]},
        "exit": {"mode": "any", "conditions": [{"left": {"ref": "f"}, "op": "cross_below", "right": {"ref": "s"}}]}}
    did = _deploy(sch, 60)
    vid = pr.get_deployment(did)["version_id"]
    _fake_result(did, vid, -35.0)
    evolve.health_check()
    d = pr.get_deployment(did)
    assert d["status"] == "paused_entries" and d["config"]["auto_pause"]["kind"] == "backtest"
    evolve.health_check()                                   # 幂等：仍暂停
    assert pr.get_deployment(did)["status"] == "paused_entries"
    # 进化换上训练集为正的新版本 → 自动恢复
    nv = registry.freeze(None, dict(sch, signal_tf=60, stop_loss_pct=4.0), source="auto_evolve", parent_id=vid,
                         set_current=False)
    db.ex("UPDATE paper_deployments SET version_id=? WHERE id=?", (nv["id"], did))
    _fake_result(did, vid, None, decision="替换", new_vid=nv["id"])
    db.ex("UPDATE evolve_results SET best_json=? WHERE new_version_id=?",
          (json.dumps({"return_pct": 8.0}), nv["id"]))
    evolve.health_check()
    d = pr.get_deployment(did)
    assert d["status"] == "running" and "auto_pause" not in d["config"]


def test_live_loss_pause_and_manual_override(evo_env):
    evolve.set_config(pause_losers=True, pause_train_loss_pct=-10.0, pause_live_loss_pct=-10.0)
    sch = {"name": "实盘亏损测试", "indicators": [
        {"id": "f", "type": "EMA", "params": {"period": 9}}, {"id": "s", "type": "EMA", "params": {"period": 21}}],
        "entry": {"mode": "all", "conditions": [{"left": {"ref": "f"}, "op": "cross_above", "right": {"ref": "s"}}]},
        "exit": {"mode": "any", "conditions": [{"left": {"ref": "f"}, "op": "cross_below", "right": {"ref": "s"}}]}}
    did = _deploy(sch, 60)
    d = pr.get_deployment(did)
    cfg = d["config"]; cfg["budget_usd"] = 10000
    db.ex("UPDATE paper_deployments SET config_json=? WHERE id=?", (json.dumps(cfg), did))
    _fake_result(did, d["version_id"], 5.0)                 # 回测盈利，不触发回测暂停
    db.ex("INSERT INTO paper_orders(deployment_id,client_order_id,broker,side,qty,status,filled_qty,filled_price,"
          "signal_ts,created_at,updated_at,is_test) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
          (did, f"l-{did}-b", "mock", "buy", 0.1, "filled", 0.1, 100000, "x", db.now_iso(), db.now_iso()))
    evolve.health_check(price=95000)                        # 亏 500 = 额度 5%：不暂停
    assert pr.get_deployment(did)["status"] == "running"
    evolve.health_check(price=85000)                        # 亏 1500 = 额度 15%：暂停
    d = pr.get_deployment(did)
    assert d["status"] == "paused_entries" and d["config"]["auto_pause"]["kind"] == "live"
    evolve.health_check(price=85000)                        # 同一版本、实盘原因：不会自动恢复
    assert pr.get_deployment(did)["status"] == "paused_entries"
    pr.set_status(did, "running")                           # 用户手动恢复
    evolve.health_check(price=80000)
    d = pr.get_deployment(did)
    assert d["status"] == "running" and d["config"]["auto_pause_override"] == d["version_id"]
    evolve.health_check(price=70000)                        # 手动恢复的同一版本不再被自动暂停
    assert pr.get_deployment(did)["status"] == "running"


def test_regime_and_trailing_mutations():
    base = {"name": "t", "signal_tf": 1440, "indicators": [
        {"id": "f", "type": "EMA", "params": {"period": 9}}, {"id": "s", "type": "EMA", "params": {"period": 21}}],
        "entry": {"mode": "all", "conditions": [{"left": {"ref": "f"}, "op": "cross_above", "right": {"ref": "s"}}]},
        "exit": {"mode": "any", "conditions": [{"left": {"ref": "f"}, "op": "cross_below", "right": {"ref": "s"}}]},
        "trailing": {"activate_pct": 4, "trail_pct": 4}}
    whys = [w for _, w in evolve.mutations(validate(base)[0].model_dump())]
    assert any("大周期过滤" in w for w in whys)
    assert any("移动止损启动" in w for w in whys)
    assert any("盈利6%/回撤3%" in w for w in whys)
    h = dict(base, signal_tf=240)
    ms = [m for m, w in evolve.mutations(validate(h)[0].model_dump()) if "大周期过滤" in w]
    assert ms and all(m["aux_tf"] == 1440 for m in ms)


def test_plan_budgets_weights_by_strength():
    deps = [{"id": 1, "status": "running", "version_id": -1}, {"id": 2, "status": "running", "version_id": -2},
            {"id": 3, "status": "paused_entries", "version_id": -3}]
    import aiquant.evolve as ev
    orig = ev._train_metrics
    ev._train_metrics = lambda d: {1: {"return_pct": 20, "max_dd_pct": -8}, 2: {"return_pct": -5, "max_dd_pct": -10}}.get(d["id"])
    try:
        p = ev.plan_budgets(100000, deps, dict(ev.DEFAULTS))
    finally:
        ev._train_metrics = orig
    assert p[3]["budget"] == 6000
    assert p[1]["weight"] == 2.0 and p[2]["weight"] == 0.25
    assert p[1]["budget"] > p[2]["budget"]
    assert sum(x["budget"] for x in p.values()) <= 96000


def test_auto_pause_off_resumes_and_never_pauses(evo_env):
    """10-31：关闭自动暂停后，被暂停的策略恢复开仓；亏损策略也不再被暂停（即时测试）。"""
    evolve.set_config(pause_losers=True, pause_train_loss_pct=-10.0, pause_live_loss_pct=-10.0)
    sch = {"name": "关闭暂停测试", "indicators": [
        {"id": "f", "type": "EMA", "params": {"period": 9}}, {"id": "s", "type": "EMA", "params": {"period": 21}}],
        "entry": {"mode": "all", "conditions": [{"left": {"ref": "f"}, "op": "cross_above", "right": {"ref": "s"}}]},
        "exit": {"mode": "any", "conditions": [{"left": {"ref": "f"}, "op": "cross_below", "right": {"ref": "s"}}]}}
    did = _deploy(sch, 60)
    _fake_result(did, pr.get_deployment(did)["version_id"], -35.0)
    evolve.health_check()
    assert pr.get_deployment(did)["status"] == "paused_entries"
    try:
        evolve.set_config(pause_losers=False)
        out = evolve.health_check()
        d = pr.get_deployment(did)
        assert d["status"] == "running" and "auto_pause" not in d["config"] and any("关闭自动暂停" in x for x in out)
        evolve.health_check()
        assert pr.get_deployment(did)["status"] == "running"
    finally:
        evolve.set_config(pause_losers=True)
