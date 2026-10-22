"""集成测试：版本、任务队列、研究分区、DeepSeek 流程、Paper 安全门禁与 Mock Runner。"""
import json
from datetime import timedelta

import pandas as pd
import pytest

from aiquant import actions, config, db, jobs, research
from aiquant.ai import deepseek, optimizer
from aiquant.data import http
from aiquant.data import market_data as md
from aiquant.paper import runner as pr
from aiquant.paper.brokers import AlpacaPaperAdapter, OrderPermissionLocked
from aiquant.strategy import registry
from aiquant.strategy.templates import TEMPLATES


def _freeze(slot, tpl):
    return registry.freeze(slot, TEMPLATES[tpl]["schema"], nl_text=TEMPLATES[tpl]["text"])


def test_freeze_dedupe_and_versioning(env):
    v1 = _freeze(1, "趋势跟踪")
    again = _freeze(1, "趋势跟踪")
    assert again["id"] == v1["id"]
    s2 = dict(TEMPLATES["趋势跟踪"]["schema"], stop_loss_pct=1.0)
    v2 = registry.freeze(1, s2)
    assert v2["version_no"] == v1["version_no"] + 1 and v2["parent_id"] == v1["id"]
    assert registry.get_version(v1["id"])["schema"]["stop_loss_pct"] == 2.0  # 不覆盖
    with pytest.raises(registry.FreezeError):
        registry.freeze(1, {"name": "bad"})


def test_batch_backtest_dedupe_and_results(env):
    for i, t in enumerate(TEMPLATES, 1):
        _freeze(i, t)
    r1 = actions.submit_all()
    r2 = actions.submit_all()
    assert all(a[1] == b[1] for a, b in zip(r1, r2))  # 刷新/重复点击不重复启动
    jobs.wait_all(180)
    st = db.q("SELECT status, error FROM jobs WHERE kind='backtest'")
    assert all(x["status"] == "done" for x in st), st
    lb = actions.leaderboard()
    assert lb["净收益率%"].notna().sum() == 5
    jid, msg = actions.submit_backtest(1)
    assert "复用" in msg
    b = actions.slot_result(1)
    assert b["is_test"] == 1 and b["split_label"] == "训练集"
    assert research.benchmark_for(b) is not None


def test_cancel_queued_job(env):
    jid, created = jobs.submit("backtest", slot_no=1, version_id=None, params={}, dedupe_key="zz-cancel")
    jobs.cancel(jid)
    jobs.wait_all(10)
    assert jobs.get(jid)["status"] in ("stopped", "failed")


def test_restart_recovery_marks_running_failed(env):
    jid = db.ex("INSERT INTO jobs(kind,params_json,dedupe_key,status,created_at) VALUES('backtest','{}','zz-r','running',?)",
                (db.now_iso(),))
    n = jobs.recover_on_start()
    assert n >= 1 and jobs.get(jid)["status"] == "failed"


def test_test_set_touch_rejected(env):
    r = research.get_research()
    research.set_research({"window_anchor": "latest"})
    try:
        jid, msg = actions.submit_backtest(1)
        jobs.wait_all(60)
        j = jobs.get(jid)
        assert j["status"] == "failed" and "测试集" in j["error"]
    finally:
        research.set_research({"window_anchor": r["window_anchor"]})
    assert research.test_set_status()["ok"]


def test_validation_limit(env):
    msgs = []
    for _ in range(research.VALIDATION_RUN_LIMIT + 1):
        v = registry.get_version(registry.get_slot(2)["current_version_id"])
        # 每次不同参数以避开去重；同时提交（不等待）以验证并发下限制仍生效
        s = dict(v["schema"]); s["stop_loss_pct"] = round((s.get("stop_loss_pct") or 2) + 0.1, 2)
        registry.freeze(2, s)
        msgs.append(actions.submit_backtest(2, kind="validation"))
    jobs.wait_all(90)
    assert msgs[-1][0] is None and "上限" in msgs[-1][1]
    lin = registry.get_version(registry.get_slot(2)["current_version_id"])["lineage_id"]
    n = db.q1("SELECT COUNT(*) AS n FROM holdout_touches h JOIN strategy_versions v ON v.id=h.version_id "
              "WHERE h.split='validation' AND v.lineage_id=?", (lin,))["n"]
    assert n <= research.VALIDATION_RUN_LIMIT


def test_final_test_on_test_data_does_not_consume(env):
    vid = registry.get_slot(4)["current_version_id"]
    jid, msg = actions.submit_backtest(4, kind="final_test")
    assert jid, msg
    jobs.wait_all(60)
    assert jobs.get(jid)["status"] == "done"
    jid2, msg2 = actions.submit_backtest(4, kind="final_test")
    assert "已运行过" not in msg2  # 测试数据不消耗真实独立测试资格


def test_deepseek_mock_analysis_and_candidate(env):
    jid, msg = actions.submit_ai_analyze(3)
    assert jid, msg
    jobs.wait_all(30)
    a = optimizer.latest_analysis(3)
    assert a["status"] == "done"
    raw = json.loads(open(a["raw_response_path"], encoding="utf-8").read())
    assert raw["mock"] is True
    parent = registry.get_version(registry.get_slot(3)["current_version_id"])
    v = optimizer.create_candidate(a["id"])
    assert v["parent_id"] == parent["id"] and v["source"] == "deepseek"
    assert registry.get_slot(3)["current_version_id"] == parent["id"]  # 不自动替换
    assert research.latest_backtest_for_version(v["id"], include_test=True) is None  # 需重新回测


def test_check_proposed_rejects_code():
    s = dict(TEMPLATES["趋势跟踪"]["schema"])
    s["hypothesis"] = "__import__('os').system('x')"
    assert deepseek.check_proposed(s)[1]
    assert deepseek.check_proposed({**TEMPLATES["趋势跟踪"]["schema"], "python": "x"})[1]
    assert deepseek.check_proposed("nope")[1]


def test_batch_ai(env):
    jid, msg = actions.submit_ai_batch()
    jobs.wait_all(30)
    b = optimizer.latest_batch()
    assert b["status"] == "done", b
    with_results = sum(1 for i in range(1, 6) if actions.slot_result(i) is not None)
    assert with_results >= 4
    assert len(b["parsed"]["_created_versions"]) == with_results, b["parsed"].get("_errors")


# ---------------- 交易权限 ----------------

@pytest.mark.parametrize("m,u", [
    ("GET", "https://api.alpaca.markets./v2/account"),
    ("POST", "https://paper-api.alpaca.markets/v2/%6Frders"),
    ("POST", "https://paper-api.alpaca.markets/v2/Orders"),
    ("PATCH", "https://paper-api.alpaca.markets/v2/account/configurations"),
    ("GET", "http://data.alpaca.markets/v1beta3/crypto/us/bars"),
    ("GET", "https://evil.example.com/"),
])
def test_url_guard_bypass_attempts(m, u):
    with pytest.raises(http.ForbiddenEndpoint):
        http.check_url(m, u)


def test_reserved_indicator_ids_rejected():
    from aiquant.strategy.schema import validate
    for rid in ("close", "close_time", "complete", "aux_close_time"):
        s = {**TEMPLATES["趋势跟踪"]["schema"],
             "indicators": [{"id": rid, "type": "EMA", "params": {"period": 20}},
                            {"id": "ema60", "type": "EMA", "params": {"period": 60}}]}
        assert validate(s)[1]


def test_live_and_order_endpoints_forbidden():
    with pytest.raises(http.ForbiddenEndpoint):
        http.check_url("GET", "https://api.alpaca.markets/v2/account")
    with pytest.raises(http.ForbiddenEndpoint):
        http.check_url("POST", "https://paper-api.alpaca.markets/v2/orders")
    with pytest.raises(http.ForbiddenEndpoint):
        http.check_url("DELETE", "https://paper-api.alpaca.markets/v2/positions/BTCUSD")
    http.check_url("GET", "https://paper-api.alpaca.markets/v2/account")
    assert config.LIVE_TRADING_ENABLED is False
    assert not http.paper_orders_unlocked()  # 无授权文件时仍锁定


def test_alpaca_adapter_writes_locked():
    ad = AlpacaPaperAdapter()
    with pytest.raises(OrderPermissionLocked):
        ad.submit_order("x", "buy", 0.001)
    with pytest.raises(OrderPermissionLocked):
        ad.close_position()
    # 授权文件存在但代码常量关闭时仍锁定
    f = config.CONFIG_DIR / "paper_authorization.json"
    f.write_text("{}")
    old = config.PAPER_ORDER_SUBMISSION_UNLOCKED
    config.PAPER_ORDER_SUBMISSION_UNLOCKED = False
    try:
        assert not http.paper_orders_unlocked()
    finally:
        config.PAPER_ORDER_SUBMISSION_UNLOCKED = old
        f.unlink()


def test_deploy_gate_rejects_test_data_and_alpaca_start_locked(env):
    vid = registry.get_slot(1)["current_version_id"]
    ok, checks = pr.deploy_gate(vid)
    assert not ok
    with pytest.raises(ValueError):
        pr.save_deployment(vid, {}, "alpaca_paper")
    did = db.ex("INSERT INTO paper_deployments(version_id,config_json,broker,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)", (vid, "{}", "alpaca_paper", "configured", db.now_iso(), db.now_iso()))
    with pytest.raises(OrderPermissionLocked):
        pr.start(did)
    assert pr.get_deployment(did)["status"] == "configured"


def test_mock_runner_orders_recovery_and_safety(env):
    vid = registry.get_slot(1)["current_version_id"]
    did = pr.save_deployment(vid, {"alloc_pct": 100, "mock_cash": 10000}, "mock")
    ds = db.q1("SELECT * FROM datasets WHERE is_test=1 ORDER BY id DESC LIMIT 1")
    df = md.load_dataset(ds)
    # 未启动不交易
    r = pr.PaperRunner(did).step(df.tail(3000), df["ts"].iloc[-1].to_pydatetime() + timedelta(minutes=5))
    assert r["action"] == "none"
    pr.start(did)
    res = pr.mock_replay(did, df, n_steps=2000, restart_at=1000)
    assert res["restarts"] == 1
    orders = db.q("SELECT * FROM paper_orders WHERE deployment_id=?", (did,))
    assert orders and all(o["is_test"] == 1 and o["broker"] == "mock" for o in orders)
    assert len({o["client_order_id"] for o in orders}) == len(orders)
    # 数据过期 → 阻止
    runner = pr.PaperRunner(did)
    r = runner.step(df.tail(3000), df["ts"].iloc[-1].to_pydatetime() + timedelta(hours=3))
    assert r["action"] == "blocked"
    # 对账不一致 → 暂停新开仓
    from aiquant.paper.brokers import MockBroker
    mb = MockBroker(did)
    st = db.get_setting(mb.key); st["qty"] += 0.5; db.set_setting(mb.key, st)
    r = runner.step(df.tail(3000), df["ts"].iloc[-1].to_pydatetime() + timedelta(minutes=5))
    assert r["action"] == "blocked" and pr.get_deployment(did)["status"] == "paused_entries"
    pr.stop(did)
    assert pr.get_deployment(did)["status"] == "stopped"
    with pytest.raises(ValueError):
        pr.flatten(did, confirmed=False)
    assert db.q1("SELECT COUNT(*) AS n FROM paper_orders WHERE broker='alpaca_paper'")["n"] == 0
    audit = db.q("SELECT * FROM endpoint_audit WHERE url LIKE '%api.alpaca.markets%' AND url NOT LIKE '%paper-api%' "
                 "AND url NOT LIKE '%data.alpaca%'")
    assert audit == []
