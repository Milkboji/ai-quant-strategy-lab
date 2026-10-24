"""策略总台：手动保存不替换运行版本、单策略单版本部署（空仓才切换）、下线（空仓才停止）、权限、审计、一键部署不改运行版本。"""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from aiquant import console, db, evolve, research
from aiquant.data import market_data as md
from aiquant.paper import portfolio as pf
from aiquant.paper import runner as pr
from aiquant.strategy import registry

BASE = {"indicators": [{"id": "f", "type": "EMA", "params": {"period": 9}},
                       {"id": "s", "type": "EMA", "params": {"period": 21}}],
        "entry": {"mode": "all", "conditions": [{"left": {"ref": "f"}, "op": "cross_above", "right": {"ref": "s"}}]},
        "exit": {"mode": "any", "conditions": [{"left": {"ref": "f"}, "op": "cross_below", "right": {"ref": "s"}}]}}
_n = {"i": 0}


def _version(slot: int, name: str, new_lineage: bool = False, **extra) -> dict:
    sch = dict(BASE, name=name, signal_tf=extra.pop("signal_tf", 60), **extra)
    v = registry.freeze(slot, sch, source="manual", new_lineage=new_lineage)
    sp = research.get_split()
    s, e = md.to_utc(sp["val_start"]) - timedelta(days=200), md.to_utc(sp["val_start"])
    ds = md.get_dataset(s, e, source="test")
    ev = evolve.evaluate(md.load_dataset(ds), v["schema"], s, e, dict(research.get_research(), source="test"))
    research.store_backtest(job_id=None, version=v, slot_no=slot, ds=ds, start=s, end=e, tf=ev["tf"], aux=None,
                            split="训练集", cfg=ev["cfg"], res=ev["res"], is_test=0)
    return v


def _fill(did: int, side: str) -> None:
    _n["i"] += 1
    db.ex("INSERT INTO paper_orders(deployment_id,client_order_id,broker,side,qty,status,filled_qty,filled_price,"
          "signal_ts,created_at,updated_at,is_test) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)",
          (did, f"cs-{did}-{_n['i']}", "mock", side, 0.01, "filled", 0.01, 50000, "x", db.now_iso(), db.now_iso()))


def _row(key: str, broker: str = "mock") -> dict:
    return next(r for r in console.rows(broker=broker) if r["key"] == key)


@pytest.fixture(scope="module")
def cs_env(env):
    md.install_synthetic_test_data(md.to_utc(env["val_start"]) - timedelta(days=440), md.to_utc(env["test_end"]))
    return env


@pytest.fixture()
def clean(cs_env):
    db.ex("UPDATE paper_deployments SET status='stopped' WHERE broker='mock' "
          "AND status IN ('configured','running','paused_entries')")
    for k in [f"slot:{i}" for i in range(1, 6)]:
        db.set_setting(f"console_offline::{k}", None)
    yield


def test_manual_save_does_not_replace_running_version(clean):
    v1 = _version(5, "总台测试A", new_lineage=True)
    res = console.deploy_version("slot:5", v1["id"], actor="tester", role="trader", broker="mock")
    assert res["result"] == "已启动"
    did = res["deployment_id"]
    assert pr.get_deployment(did)["status"] == "running"
    assert pr.get_deployment(did)["config"]["strategy_key"] == "slot:5"
    v2 = _version(5, "总台测试A", stop_loss_pct=3.0)          # 实验室手动保存新版本
    assert registry.get_slot(5)["current_version_id"] == v2["id"]
    assert pr.get_deployment(did)["version_id"] == v1["id"]   # 运行版本不变
    r = _row("slot:5")
    assert r["status"] == "运行中" and r["mismatch"] and r["lab_newer"]
    assert r["run"]["id"] == v1["id"] and r["lab"]["id"] == v2["id"]
    assert r["diff"] and any("止损" in k or "stop" in k for k, _, _ in r["diff"])


def test_deploy_version_switches_only_when_flat(clean):
    v1 = _version(5, "总台测试B", new_lineage=True)
    did = console.deploy_version("slot:5", v1["id"], actor="tester", role="admin", broker="mock")["deployment_id"]
    v2 = _version(5, "总台测试B", stop_loss_pct=4.0)
    _fill(did, "buy")                                          # 有持仓
    res = console.deploy_version("slot:5", v2["id"], actor="tester", role="trader", broker="mock")
    assert res["result"] == "等待空仓后切换"
    d = pr.get_deployment(did)
    assert d["version_id"] == v1["id"] and d["config"]["pending_switch"]["trigger"] == "manual"
    r = _row("slot:5")
    assert r["pending"]["version"]["id"] == v2["id"]
    assert not evolve.apply_pending(did)
    _fill(did, "sell")                                         # 平仓后才切换
    evolve.apply_all_pending()
    d = pr.get_deployment(did)
    assert d["version_id"] == v2["id"] and "pending_switch" not in d["config"]
    assert d["config"]["evolve_history"][-1]["trigger"] == "manual"
    assert not _row("slot:5")["mismatch"]
    log = console.audit_log(key="slot:5")
    acts = [x["action"] for x in log]
    assert "部署此版本" in acts and "切换生效" in acts
    sw = next(x for x in log if x["action"] == "切换生效")
    assert sw["actor"] == "tester" and sw["from_version_id"] == v1["id"] and sw["to_version_id"] == v2["id"]
    # 再次选择当前版本：无变化
    assert console.deploy_version("slot:5", v2["id"], actor="tester", role="trader", broker="mock")["result"] == "无变化"


def test_offline_waits_for_flat_and_is_excluded_from_deploy_all(clean):
    v1 = _version(5, "总台测试C", new_lineage=True)
    did = console.deploy_version("slot:5", v1["id"], actor="tester", role="trader", broker="mock")["deployment_id"]
    _fill(did, "buy")
    res = console.take_offline("slot:5", actor="tester", role="trader", broker="mock")
    assert res["result"] == "等待空仓后下线"
    d = pr.get_deployment(did)
    assert d["status"] == "paused_entries" and d["config"]["pending_offline"]["by"] == "tester"
    assert console.apply_pending_offline() == 0
    evolve.health_check(None)                                  # 自动恢复逻辑不能把待下线的策略恢复
    assert pr.get_deployment(did)["status"] == "paused_entries"
    assert _row("slot:5")["pending_offline"]
    _fill(did, "sell")
    assert console.apply_pending_offline() >= 1
    assert pr.get_deployment(did)["status"] == "stopped"
    r = _row("slot:5")
    assert r["status"] == "已下线" and r["offline"] and r["deployment"] is None
    assert "slot:5" not in [t["key"] for t in pf.targets()]
    assert "slot:5" in [t["key"] for t in pf.targets(include_offline=True)]
    acts = [x["action"] for x in console.audit_log(key="slot:5")]
    assert "下线" in acts and "下线生效" in acts
    # 重新部署：清除下线标记，新建部署
    res = console.deploy_version("slot:5", v1["id"], actor="tester", role="trader", broker="mock")
    assert res["result"] == "已启动" and res["deployment_id"] != did
    assert not console.is_offline("slot:5") and _row("slot:5")["status"] == "运行中"


def test_offline_when_flat_stops_immediately(clean):
    v1 = _version(4, "总台测试D", new_lineage=True)
    did = console.deploy_version("slot:4", v1["id"], actor="tester", role="trader", broker="mock")["deployment_id"]
    res = console.take_offline("slot:4", actor="tester", role="trader", broker="mock")
    assert res["result"] == "已下线" and pr.get_deployment(did)["status"] == "stopped"


def test_viewer_cannot_deploy_or_take_offline(clean):
    v1 = _version(5, "总台测试E", new_lineage=True)
    n0 = len(console.audit_log(500))
    with pytest.raises(console.ConsolePermissionError):
        console.deploy_version("slot:5", v1["id"], actor="guest", role="viewer", broker="mock")
    with pytest.raises(console.ConsolePermissionError):
        console.take_offline("slot:5", actor="guest", role="viewer", broker="mock")
    with pytest.raises(console.ConsolePermissionError):
        pf.deploy_all(actor="guest", role="viewer")
    assert len(console.audit_log(500)) == n0
    assert console.active_deployment("slot:5", "mock") is None


def test_version_must_belong_to_strategy(clean):
    v4 = _version(4, "总台测试F", new_lineage=True)
    with pytest.raises(ValueError):
        console.deploy_version("slot:5", v4["id"], actor="tester", role="admin", broker="mock")


def test_deploy_all_keeps_running_version(clean, monkeypatch):
    v1 = _version(3, "总台测试G", new_lineage=True)
    cfg = {"label": f"策略位3·总台测试G v{v1['version_no']}", "signal_tf": 60, "aux_tf": None, "budget_usd": 1000,
           "strategy_key": "slot:3", "portfolio": True}
    did = db.ex("INSERT INTO paper_deployments(version_id,config_json,broker,status,created_at,updated_at,is_test) "
                "VALUES(?,?,?,?,?,?,0)", (v1["id"], json.dumps(cfg, ensure_ascii=False), "alpaca_paper", "running",
                                          db.now_iso(), db.now_iso()))
    keys = [f"slot:{i}" for i in (1, 2, 4, 5)] + [f"preset:{pr_v['name']}" for pr_v in
                                                  (pf.preset_version(k) for k in pf.sim.PRESETS)]
    try:
        for k in keys:   # 只让 slot:3 参与，避免本测试创建其他真实 Paper 部署
            db.set_setting(f"console_offline::{k}", {"at": "test"})
        v2 = _version(3, "总台测试G", stop_loss_pct=3.0, signal_tf=240)   # 实验室新版本，周期也不同
        monkeypatch.setattr(pf, "AlpacaPaperAdapter",
                            lambda: SimpleNamespace(account=lambda: SimpleNamespace(equity=100_000.0)))
        res = pf.deploy_all(actor="tester", role="admin")
        d = pr.get_deployment(did)
        assert d["version_id"] == v1["id"]
        assert d["config"]["label"] == cfg["label"] and d["config"]["signal_tf"] == 60
        assert d["config"]["budget_usd"] == 96_000
        assert any("未自动替换" in m for m in res)
        r = _row("slot:3", broker="alpaca_paper")
        assert r["mismatch"] and r["lab"]["id"] == v2["id"] and r["weight_pct"] is not None
        assert any("周期" in k or "signal_tf" in k for k, _, _ in r["diff"])
    finally:
        pr.stop(did)
        for k in keys:
            db.set_setting(f"console_offline::{k}", None)


def test_console_page_is_read_only_for_viewers(clean):
    from streamlit.testing.v1 import AppTest

    def app():
        import streamlit as st

        from aiquant import auth
        from aiquant.ui import login, page_console
        auth.ensure()
        login._install_readonly_guard()
        st.session_state["auth_user"] = "guest_viewer"      # 不存在的账号 → 观察员
        st.session_state["_aq_readonly"] = True
        page_console.render()

    v1 = _version(5, "总台测试H", new_lineage=True)
    console.deploy_version("slot:5", v1["id"], actor="tester", role="trader", broker="mock")
    at = AppTest.from_function(app, default_timeout=120)
    at.run()
    assert not at.exception
    assert any("策略总台" in m.value for m in at.markdown)
    btns = [b for b in at.button if b.label in ("部署此版本", "下线", "一键部署全部")]
    assert btns and all(b.disabled for b in btns)
