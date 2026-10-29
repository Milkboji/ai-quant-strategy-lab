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
    db.set_setting("promotion_config", {"require_confirm": False, "manual_lock_days": 7})
    yield
    db.set_setting("promotion_config", {"require_confirm": False, "manual_lock_days": 7})


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
        # 已在运行的策略：不按「一键部署」平均分配重置额度（新部署不得摊薄现有策略），额度仍由按实力分配决定
        from aiquant import evolve
        assert d["config"]["budget_usd"] != 96_000
        assert d["config"]["budget_usd"] == evolve.plan_budgets(100_000.0, [d])[d["id"]]["budget"]
        assert any("未自动替换" in m for m in res) and any("保持不变" in m for m in res)
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


# ---------------------------------------------------------------------------
# 锁定版本 / 晋级需人工确认 / 待确认队列
# ---------------------------------------------------------------------------

def _challenger(did: int, champ: dict, **change) -> dict:
    from aiquant import challenger
    raw = db.q1("SELECT * FROM paper_deployments WHERE id=?", (did,))
    cand = dict(champ["schema"], **change)
    cid = challenger.register_btc(raw, champ, cand, "测试改动", {"score": 99.0, "return_pct": 5.0}, True)
    return db.q1("SELECT * FROM evolve_challengers WHERE id=?", (cid,))


def _win(ch: dict, did: int) -> str:
    from aiquant import challenger
    dep = pr.get_deployment(did)
    champ = registry.get_version(dep["version_id"])
    rr = dict(research.get_research(), source="test")
    return challenger._win_btc(ch, dep, champ, json.loads(ch["candidate_json"]), None, rr, "测试：前向胜出")


def _status_of(cid: int) -> dict:
    return db.q1("SELECT * FROM evolve_challengers WHERE id=?", (cid,))


def test_manual_deploy_locks_and_challenger_waits_for_approval(clean):
    v1 = _version(5, "锁定测试A", new_lineage=True)
    res = console.deploy_version("slot:5", v1["id"], actor="tester", role="trader", broker="mock")
    did = res["deployment_id"]
    assert "锁定 7 天" in res["message"]
    lk = console.lock_state(pr.get_deployment(did))
    assert lk and lk["kind"] == "manual_deploy" and lk["until"]
    assert "锁定至" in console.promotion_block(pr.get_deployment(did))
    ch = _challenger(did, v1, stop_loss_pct=5.0)
    assert "等待确认" in _win(ch, did)
    st = _status_of(ch["id"])
    assert st["status"] == "awaiting" and "挑战者胜出，等待确认" in st["reason"]
    assert pr.get_deployment(did)["version_id"] == v1["id"]          # 没有自动晋级
    r = _row("slot:5")
    assert r["lock"] and r["awaiting"]["id"] == ch["id"]
    assert any(a["id"] == ch["id"] for a in console.awaiting())
    out = console.approve(ch["id"], actor="boss", role="admin")      # 批准 → 正常晋级流程（空仓立即切换）
    assert out["result"] == "已晋级"
    st = _status_of(ch["id"])
    assert st["status"] == "promoted" and st["new_version_id"]
    assert pr.get_deployment(did)["version_id"] == st["new_version_id"]
    assert "批准晋级" in [x["action"] for x in console.audit_log(key="slot:5")]


def test_unlocked_strategy_keeps_auto_promotion(clean):
    console.set_promotion_config(actor="boss", role="admin", manual_lock_days=0)
    v1 = _version(5, "锁定测试B", new_lineage=True)
    did = console.deploy_version("slot:5", v1["id"], actor="tester", role="trader", broker="mock")["deployment_id"]
    assert console.lock_state(pr.get_deployment(did)) is None and console.promotion_block(pr.get_deployment(did)) is None
    ch = _challenger(did, v1, stop_loss_pct=6.0)
    _win(ch, did)
    st = _status_of(ch["id"])
    assert st["status"] == "promoted"
    assert pr.get_deployment(did)["version_id"] == st["new_version_id"]


def test_promotion_changes_running_version_only_and_user_adopts(clean):
    """进化 / 挑战者晋级只改运行版本；策略位指针不动，实验室提示「运行版本已晋级为 vX」，用户点击后才切换（写审计）。"""
    console.set_promotion_config(actor="boss", role="admin", manual_lock_days=0)
    v1 = _version(5, "指针测试P", new_lineage=True)
    did = console.deploy_version("slot:5", v1["id"], actor="tester", role="trader", broker="mock")["deployment_id"]
    assert registry.get_slot(5)["current_version_id"] == v1["id"]
    ch = _challenger(did, v1, stop_loss_pct=6.5)
    _win(ch, did)
    new_vid = _status_of(ch["id"])["new_version_id"]
    assert pr.get_deployment(did)["version_id"] == new_vid
    assert registry.get_slot(5)["current_version_id"] == v1["id"]          # 策略位没有被改
    pv = registry.promoted_running(5, new_vid)
    assert pv and pv["id"] == new_vid
    with pytest.raises(PermissionError):
        registry.adopt_running(5, new_vid, actor="g", role="viewer")
    registry.adopt_running(5, new_vid, actor="tester", role="trader")
    assert registry.get_slot(5)["current_version_id"] == new_vid
    assert registry.promoted_running(5, new_vid) is None
    assert any(a["action"] == "实验室切换到运行版本" for a in console.audit_log(20, key="slot:5"))


def test_global_confirm_queues_and_reject_records_reason(clean):
    console.set_promotion_config(actor="boss", role="admin", require_confirm=True, manual_lock_days=0)
    assert console.promotion_config()["require_confirm"] is True
    assert evolve.get_config()["auto_apply"] is False                # 与进化页旧开关保持一致
    v1 = _version(5, "锁定测试C", new_lineage=True)
    did = console.deploy_version("slot:5", v1["id"], actor="tester", role="trader", broker="mock")["deployment_id"]
    assert console.promotion_block(pr.get_deployment(did)) == "已开启「晋级需人工确认」"
    ch = _challenger(did, v1, stop_loss_pct=7.0)
    _win(ch, did)
    assert _status_of(ch["id"])["status"] == "awaiting"
    with pytest.raises(ValueError):
        console.reject(ch["id"], "  ", actor="boss", role="admin")
    console.reject(ch["id"], "回撤太大", actor="boss", role="trader")
    st = _status_of(ch["id"])
    assert st["status"] == "rejected" and "回撤太大" in st["reason"]
    assert pr.get_deployment(did)["version_id"] == v1["id"]
    log = [x for x in console.audit_log(key="slot:5") if x["action"] == "拒绝晋级"]
    assert log and "回撤太大" in log[0]["detail"]
    console.set_promotion_config(actor="boss", role="admin", require_confirm=False)
    assert evolve.get_config()["auto_apply"] is True


def test_lock_toggle_and_permissions(clean):
    console.set_promotion_config(actor="boss", role="admin", manual_lock_days=0)
    v1 = _version(5, "锁定测试D", new_lineage=True)
    did = console.deploy_version("slot:5", v1["id"], actor="tester", role="trader", broker="mock")["deployment_id"]
    console.set_lock("slot:5", True, actor="tester", role="trader", broker="mock")
    lk = console.lock_state(pr.get_deployment(did))
    assert lk["kind"] == "manual" and lk["until"] is None
    assert console.promotion_block(pr.get_deployment(did)) == "版本已手动锁定"
    console.set_lock("slot:5", False, actor="tester", role="trader", broker="mock")
    assert console.lock_state(pr.get_deployment(did)) is None
    ch = _challenger(did, v1, stop_loss_pct=8.0)
    db.ex("UPDATE evolve_challengers SET status='awaiting' WHERE id=?", (ch["id"],))
    for fn in (lambda: console.set_lock("slot:5", True, actor="g", role="viewer", broker="mock"),
               lambda: console.approve(ch["id"], actor="g", role="viewer"),
               lambda: console.reject(ch["id"], "x", actor="g", role="viewer"),
               lambda: console.set_promotion_config(actor="g", role="viewer", require_confirm=True)):
        with pytest.raises(console.ConsolePermissionError):
            fn()
    assert _status_of(ch["id"])["status"] == "awaiting"
    acts = [x["action"] for x in console.audit_log(key="slot:5")]
    assert "锁定版本" in acts and "解除锁定" in acts


def test_expired_lock_and_manual_deploy_voids_awaiting(clean):
    v1 = _version(5, "锁定测试E", new_lineage=True)
    did = console.deploy_version("slot:5", v1["id"], actor="tester", role="trader", broker="mock")["deployment_id"]
    d = pr.get_deployment(did)
    d["config"]["version_lock"]["until"] = "2020-01-01T00:00:00+00:00"   # 锁已过期 → 恢复自动晋级
    db.ex("UPDATE paper_deployments SET config_json=? WHERE id=?", (json.dumps(d["config"], ensure_ascii=False), did))
    assert console.lock_state(pr.get_deployment(did)) is None
    ch = _challenger(did, v1, stop_loss_pct=9.0)
    db.ex("UPDATE evolve_challengers SET status='awaiting' WHERE id=?", (ch["id"],))
    v2 = _version(5, "锁定测试E", take_profit_pct=12.0)
    console.deploy_version("slot:5", v2["id"], actor="tester", role="trader", broker="mock")
    assert _status_of(ch["id"])["status"] == "expired"
    with pytest.raises(ValueError):
        console.approve(ch["id"], actor="boss", role="admin")


def test_queue_btc_keeps_latest_and_etf_approval(clean):
    from aiquant import challenger
    from aiquant.paper import etf_trend
    v1 = _version(5, "锁定测试F", new_lineage=True)
    did = console.deploy_version("slot:5", v1["id"], actor="tester", role="trader", broker="mock")["deployment_id"]
    raw = db.q1("SELECT * FROM paper_deployments WHERE id=?", (did,))
    a = challenger.queue_btc(raw, v1, dict(v1["schema"], stop_loss_pct=4.0), "改动1", {"score": 1}, "通过", "锁定", True)
    b = challenger.queue_btc(raw, v1, dict(v1["schema"], stop_loss_pct=5.0), "改动2", {"score": 2}, "通过", "锁定", True)
    assert _status_of(a)["status"] == "replaced" and _status_of(b)["status"] == "awaiting"
    cfg = etf_trend.get_config()
    cid = db.ex("INSERT INTO evolve_challengers(kind,deployment_id,label,champion_params,candidate_json,change,train_json,"
                "registered_at,status,decided_at,reason,is_test) VALUES('etf',NULL,'ETF',?,?,?,?,?,?,?,?,1)",
                (json.dumps({"sma": int(cfg["sma"]), "band": float(cfg["band"])}),
                 json.dumps({"sma": int(cfg["sma"]) + 10, "band": float(cfg["band"])}), "均线+10", "{}",
                 db.now_iso(), "awaiting", db.now_iso(), "挑战者胜出，等待确认"))
    try:
        console.approve(cid, actor="boss", role="admin")
        assert etf_trend.get_config()["sma"] == int(cfg["sma"]) + 10
        assert _status_of(cid)["status"] == "promoted"
    finally:
        db.set_setting("etf_trend_config", dict(db.get_setting("etf_trend_config") or {}, sma=cfg["sma"], band=cfg["band"]))


# ---------------------------------------------------------------------------
# 进化记录（晋级 / 淘汰 / 拒绝）
# ---------------------------------------------------------------------------

def _log_of(ch_id: int) -> list[dict]:
    from aiquant import evolog
    return [r for r in evolog.rows(500, include_test=True) if r["challenger_id"] == ch_id]


def test_evolution_log_records_approval_rejection_and_auto(clean):
    from aiquant import challenger, evolog
    v1 = _version(5, "记录测试A", new_lineage=True)
    did = console.deploy_version("slot:5", v1["id"], actor="tester", role="trader", broker="mock")["deployment_id"]
    # 人工批准
    ch = _challenger(did, v1, stop_loss_pct=5.5)
    db.ex("UPDATE evolve_challengers SET status='awaiting', last_eval_json=? WHERE id=?",
          (json.dumps({"days": 8.0, "need_days": 14, "challenger": {"return_pct": 3.1, "max_dd_pct": -2.0, "trades": 3},
                       "champion": {"return_pct": 1.2, "max_dd_pct": -2.4, "trades": 2}}), ch["id"]))
    console.approve(ch["id"], actor="boss", role="admin")
    (r,) = _log_of(ch["id"])
    new_vid = _status_of(ch["id"])["new_version_id"]
    assert r["event"] == "晋级" and r["trigger"] == "人工批准" and r["actor"] == "boss"
    assert r["from_version_id"] == v1["id"] and r["to_version_id"] == new_vid and r["strategy_key"] == "slot:5"
    assert r["diff_plain"] and "stop_loss_pct" in r["diff_raw"]
    assert r["forward"]["challenger"]["return_pct"] == 3.1 and r["forward"]["champion"]["max_dd_pct"] == -2.4
    assert r["score"]["challenger"]["score"] == 99.0
    assert "→" in evolog.describe(r)
    # 人工拒绝
    cur = registry.get_version(pr.get_deployment(did)["version_id"])
    ch2 = _challenger(did, cur, take_profit_pct=15.0)
    db.ex("UPDATE evolve_challengers SET status='awaiting' WHERE id=?", (ch2["id"],))
    console.reject(ch2["id"], "前向样本太少", actor="boss", role="admin")
    (r2,) = _log_of(ch2["id"])
    assert r2["event"] == "拒绝" and r2["trigger"] == "人工拒绝" and "前向样本太少" in r2["reason"]
    # 自动淘汰
    ch3 = _challenger(did, cur, take_profit_pct=18.0)
    challenger._close(ch3, "rejected", "前向表现不如现役，淘汰：测试")
    (r3,) = _log_of(ch3["id"])
    assert r3["event"] == "淘汰" and r3["trigger"] == "自动"
    # 作废 / 被替换不算晋级、淘汰、拒绝
    ch4 = _challenger(did, cur, take_profit_pct=21.0)
    challenger._close(ch4, "expired", "现役版本已变化")
    assert _log_of(ch4["id"]) == []


def test_evolution_log_auto_promotion_and_direct(clean):
    from aiquant import evolog
    console.set_promotion_config(actor="boss", role="admin", manual_lock_days=0)
    v1 = _version(5, "记录测试B", new_lineage=True)
    did = console.deploy_version("slot:5", v1["id"], actor="tester", role="trader", broker="mock")["deployment_id"]
    ch = _challenger(did, v1, stop_loss_pct=6.5)
    _win(ch, did)
    (r,) = _log_of(ch["id"])
    assert r["event"] == "晋级" and r["trigger"] == "自动"
    # 进化直接替换（没有挑战者阶段）
    v2 = registry.get_version(pr.get_deployment(did)["version_id"])
    v3 = registry.freeze(5, dict(v2["schema"], take_profit_pct=11.0), source="auto_evolve", parent_id=v2["id"])
    dep = db.q1("SELECT * FROM paper_deployments WHERE id=?", (did,))
    rec = {"label": "记录测试B", "current": {"score": 1.0, "segments": [1, -1, 2, 0]},
           "best": {"score": 2.5, "segments": [2, 1, 2, 1], "change": "增加 11% 止盈"}, "reason": "稳健分 1.00 → 2.50"}
    ho = {"kind": "验证集", "passed": True, "reason": "验证集：候选 +2% / 现役 +1%", "_eval": {}}
    evolog.record_direct(dep=dep, cur=v2, new=v3, rec=rec, ho=ho, run_id=987654)
    rd = next(x for x in evolog.rows(500, include_test=True) if x["source"] == "evolve_run:987654")
    assert rd["event"] == "晋级" and rd["from_version_id"] == v2["id"] and rd["to_version_id"] == v3["id"]
    assert rd["score"]["challenger"]["score"] == 2.5 and rd["holdout"]["passed"] and "_eval" not in rd["holdout"]
    assert any("止盈" in a for a, _, _ in rd["diff_plain"])


def test_evolution_log_etf_promotion(clean):
    from aiquant import evolog
    from aiquant.paper import etf_trend
    cfg = etf_trend.get_config()
    cid = db.ex("INSERT INTO evolve_challengers(kind,deployment_id,label,champion_params,candidate_json,change,train_json,"
                "registered_at,status,decided_at,reason,is_test) VALUES('etf',NULL,'ETF',?,?,?,?,?,?,?,?,1)",
                (json.dumps({"sma": int(cfg["sma"]), "band": float(cfg["band"])}),
                 json.dumps({"sma": int(cfg["sma"]) + 20, "band": 0.03}), "均线+20", "{}",
                 db.now_iso(), "awaiting", db.now_iso(), "挑战者胜出，等待确认"))
    try:
        console.approve(cid, actor="boss", role="admin")
        (r,) = _log_of(cid)
        assert r["kind"] == "etf" and r["event"] == "晋级" and r["strategy_key"] == "etf"
        assert any(a.startswith("均线") for a, _, _ in r["diff_plain"]) and any(a == "缓冲带" for a, _, _ in r["diff_plain"])
        assert "日均线" in evolog.describe(r)
    finally:
        db.set_setting("etf_trend_config", dict(db.get_setting("etf_trend_config") or {}, sma=cfg["sma"], band=cfg["band"]))


def test_evolution_log_backfill_is_idempotent(clean):
    from aiquant import evolog
    n0 = len(evolog.rows(2000, include_test=True))
    evolog.backfill()
    evolog.backfill()
    assert len(evolog.rows(2000, include_test=True)) == n0


# ---------------------------------------------------------------------------
# “为什么没有动作”与验证集剩余次数
# ---------------------------------------------------------------------------

def _running_dep(slot: int, name: str) -> tuple[int, dict]:
    v = _version(slot, name, new_lineage=True)
    did = console.deploy_version(f"slot:{slot}", v["id"], actor="tester", role="trader", broker="mock")["deployment_id"]
    return did, v


def test_explain_cooldown_shows_cause_and_remaining(clean):
    import pandas as pd
    did, _ = _running_dep(5, "解释测试A")
    until = pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=41, minutes=30)
    db.set_setting(f"rhythm_state::{did}", {"block_until": until.isoformat(), "block_why": "连亏 3 笔刹车", "streak": 0})
    w = console.explain(pr.get_deployment(did))
    assert "冷却中（连亏 3 笔）" in w["headline"] and "还剩 1 天 17 小时" in w["headline"]
    item = dict(w["items"])["冷却中"]
    assert "剩余 1 天 17 小时" in item and "北京时间" in item
    db.set_setting(f"rhythm_state::{did}", {"block_until": until.isoformat(), "block_why": "止损后冷却", "streak": 1})
    assert "冷却中（止损）" in console.explain(pr.get_deployment(did))["headline"]
    db.set_setting(f"rhythm_state::{did}", {"streak": 2})
    w = console.explain(pr.get_deployment(did))
    assert "冷却中" not in w["headline"] and "当前连亏 2 笔" in dict(w["items"])["连亏计数"]


def test_explain_long_pause_shows_numbers(clean):
    from aiquant import rhythm
    did, v = _running_dep(5, "解释测试B")
    dep = pr.get_deployment(did)
    tf = int(dep["config"]["signal_tf"])
    db.set_setting(rhythm._long_key(v["id"], tf, None), {"rules": rhythm.rules_hash(), "mean_ret": -3.2, "mean_dd": 12.5,
                                                         "pos_years": 2, "n_years": 6, "ok": False, "years": []})
    cfg = dep["config"]
    cfg["auto_pause"] = {"kind": "long", "reason": "长周期体检不达标", "version_id": v["id"], "at": db.now_iso()}
    db.ex("UPDATE paper_deployments SET config_json=?, status='paused_entries' WHERE id=?",
          (json.dumps(cfg, ensure_ascii=False), did))
    w = console.explain(pr.get_deployment(did))
    txt = dict(w["items"])["长期体检暂停"]
    assert "近 6 年年均收益 -3.2%（为负）" in txt and "盈利年份 2/6（少于 3 年）" in txt and "12.5%" in txt
    assert "长期体检暂停新开仓" in w["headline"]


def test_explain_stale_signal_and_weight_and_sizing(clean):
    import numpy as np
    import pandas as pd
    did, _ = _running_dep(5, "解释测试C")
    now = pd.Timestamp.now(tz="UTC")
    db.set_setting(f"stale_signal::{did}", {"signal_bar": (now - pd.Timedelta(minutes=50)).isoformat(), "age_min": 50,
                                            "limit_min": 15, "at": now.isoformat(), "reason": "入场信号已过期"})
    plan = {"weight": 2.0, "share_pct": 40.0, "detail": {"method": "long", "mean_ret": 30.0, "mean_dd": 5.0, "raw": 12.0,
                                                         "clipped": "max", "wmin": 0.25, "wmax": 2.0, "n_years": 6, "pos_years": 5}}
    idx = pd.date_range(now.floor("D") - pd.Timedelta(days=40), now.floor("D"), freq="5min", tz="UTC", inclusive="left")
    rng = np.random.default_rng(3)
    close = 50000 * np.exp(np.cumsum(rng.normal(0, 0.004, len(idx))))
    vol = console.vol_sizing(pd.DataFrame({"ts": idx, "close": close}))
    assert vol and abs(vol["raw"] - vol["target"] / vol["vol"]) < 1e-3
    assert vol["mult"] == round(min(max(vol["raw"], vol["min"]), 1.0), 4)
    db.ex("UPDATE paper_deployments SET config_json=json_set(config_json,'$.budget_usd',10000) WHERE id=?", (did,))
    w = console.explain(pr.get_deployment(did), plan, 50000.0, vol)
    st_ = dict(w["items"])["入场信号过期未追单"]
    assert "50 分钟" in st_ and "15 分钟" in st_ and "最近一次入场信号过期未追单" in w["headline"]
    calc = dict(w["calc"])
    assert "2 × +30.0% ÷ 5.0% = 12.00" in calc["资金权重"] and "截断为 2" in calc["资金权重"] and "40.0%" in calc["资金权重"]
    assert f"{vol['vol']:.1f}%" in calc["波动率定仓"] and "近 20 日" in calc["波动率定仓"]
    assert "÷ 额度 10,000 USD" in calc["当前仓位比例"]
    assert "抬到 0.25" in console.weight_text({"share_pct": 5.0, "detail": dict(plan["detail"], raw=0.1, clipped="min")})
    assert "未截断" in console.weight_text({"share_pct": 5.0, "detail": dict(plan["detail"], raw=1.2, clipped=None)})
    assert "取最低权重 0.25" in console.weight_text({"detail": dict(plan["detail"], mean_ret=-1.0, clipped="negative")})


def test_plan_budgets_returns_formula_details(monkeypatch):
    deps = [{"id": 1, "status": "running", "config": {}}, {"id": 2, "status": "running", "config": {}},
            {"id": 3, "status": "paused_entries", "config": {"auto_pause": {"kind": "long"}}}]
    lms = {1: {"mean_ret": 30.0, "mean_dd": 5.0, "n_years": 6, "pos_years": 5},
           2: {"mean_ret": 4.0, "mean_dd": 10.0, "n_years": 6, "pos_years": 4}}
    monkeypatch.setattr(evolve, "_long_check", lambda d: lms.get(d["id"]))
    p = evolve.plan_budgets(100_000.0, deps)
    assert p[1]["detail"]["clipped"] == "max" and p[1]["weight"] == 2.0
    assert p[2]["detail"]["clipped"] is None and abs(p[2]["detail"]["raw"] - 0.8) < 1e-9
    assert abs(p[1]["share_pct"] + p[2]["share_pct"] - 100) < 0.05
    assert p[3]["detail"]["method"] == "long_pause" and p[3]["budget"] == 0


def test_validation_quota_counts_used_runs(clean):
    from aiquant import actions
    v = _version(5, "检验次数测试", new_lineage=True)
    q = actions.validation_quota(v["lineage_id"])
    assert q["remaining"] == 3 and q["limit"] == 3
    db.ex("INSERT INTO holdout_touches(version_id,split,window_start,window_end,touched_at) VALUES(?,?,?,?,?)",
          (v["id"], "validation", "a", "b", db.now_iso()))
    assert actions.validation_quota(v["lineage_id"])["remaining"] == 2
