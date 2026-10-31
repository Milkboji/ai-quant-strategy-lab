"""自动驾驶：人工确认的事交给系统（待确认自动批准、美股节奏自动转正、点差自动切换、一次性设置）。"""
import pytest

from aiquant import allocation, autopilot, console, db, evolve, rhythm, spread_model


@pytest.fixture()
def ap(env):
    console.ensure_tables()
    yield
    db.set_setting("promotion_config", {"require_confirm": False, "manual_lock_days": 7})
    db.set_setting("capital_layers", None)
    evolve.set_config(interval_days=7)


def test_queue_is_approved_by_system(ap, monkeypatch):
    seen = []
    monkeypatch.setattr(console, "awaiting", lambda: [{"id": 11}, {"id": 12}])
    monkeypatch.setattr(console, "approve", lambda cid, actor, role: seen.append((cid, actor, role)) or {"result": "已批准"})
    out = autopilot.approve_queue()
    assert [s[0] for s in seen] == [11, 12] and all(s[2] == "admin" and "自动" in s[1] for s in seen)
    assert len(out) == 2


def test_us_rhythm_promoted_only_when_ready(ap, monkeypatch):
    calls = []
    monkeypatch.setattr(rhythm, "set_us_mode", lambda mode, actor, role: calls.append(mode) or {"message": "ok"})
    monkeypatch.setattr(rhythm, "us_promotion_status", lambda: {"shadow": True, "ready": False, "text": "x"})
    assert autopilot.us_rhythm() is None and not calls
    monkeypatch.setattr(rhythm, "us_promotion_status", lambda: {"shadow": True, "ready": True, "text": "满 20 天"})
    assert autopilot.us_rhythm() == "ok" and calls == ["active"]


def test_spread_switches_when_all_symbols_have_samples(ap, monkeypatch):
    monkeypatch.setattr(spread_model, "current", lambda: None)
    monkeypatch.setattr(spread_model, "proposal", lambda: {"missing": ["QLD"], "profiles": {"BTC/USD": [1.0]}})
    assert autopilot.spread() is None
    monkeypatch.setattr(spread_model, "proposal", lambda: {"missing": [], "profiles": {"BTC/USD": [1.0]}})
    monkeypatch.setattr(spread_model, "build_report", lambda actor: {"id": 7})
    got = []
    monkeypatch.setattr(spread_model, "enable", lambda rid, actor, role: got.append(rid) or "已切换")
    assert autopilot.spread() == "已切换" and got == [7]


def test_setup_once_turns_off_manual_steps(ap):
    out = autopilot.setup_once()
    c = console.promotion_config()
    assert c["require_confirm"] is False and c["manual_lock_days"] == 0
    assert evolve.get_config()["interval_days"] == 1
    assert float(allocation.get_config()["crypto"]["max"]) <= 51.0
    assert "promotion" in out
    assert db.q("SELECT * FROM strategy_audit WHERE action='修改晋级规则' AND actor LIKE '%自动驾驶%'")


def test_newer_lab_version_becomes_challenger_once(ap):
    import json
    from datetime import timedelta
    from aiquant import challenger
    from aiquant.data import market_data as md
    from aiquant.strategy import registry
    md.install_synthetic_test_data(md.to_utc(ap_env_val()) - timedelta(days=440), md.to_utc(ap_env_val()) + timedelta(days=200))
    base = {"name": "自动驾驶实验室", "signal_tf": 60,
            "indicators": [{"id": "f", "type": "EMA", "params": {"period": 9}}, {"id": "s", "type": "EMA", "params": {"period": 21}}],
            "entry": {"mode": "all", "conditions": [{"left": {"ref": "f"}, "op": "cross_above", "right": {"ref": "s"}}]},
            "exit": {"mode": "any", "conditions": [{"left": {"ref": "f"}, "op": "cross_below", "right": {"ref": "s"}}]}}
    v1 = registry.freeze(3, base, source="manual", new_lineage=True)
    cfg = {"label": "策略位3·自动驾驶实验室", "strategy_key": "slot:3", "signal_tf": 60, "budget_usd": 1000}
    did = db.ex("INSERT INTO paper_deployments(version_id,config_json,broker,status,created_at,updated_at,is_test) "
                "VALUES(?,?,?,?,?,?,0)", (v1["id"], json.dumps(cfg, ensure_ascii=False), "alpaca_paper", "running",
                                          db.now_iso(), db.now_iso()))
    try:
        b2 = json.loads(json.dumps(base))
        b2["indicators"][1]["params"]["period"] = 30
        v2 = registry.freeze(3, b2, source="manual", parent_id=v1["id"])          # 实验室新版本（策略位指针指向 v2）
        out = autopilot.lab_versions()
        ch = db.q1("SELECT * FROM evolve_challengers WHERE deployment_id=? AND status='running'", (did,))
        assert ch and "实验室新版本" in ch["change"] and out
        assert json.loads(ch["candidate_json"])["indicators"][1]["params"]["period"] == 30
        assert autopilot.lab_versions() == []                                     # 只登记一次
        assert db.get_setting(f"lab_challenged::{v2['id']}")
    finally:
        db.ex("UPDATE paper_deployments SET status='stopped' WHERE id=?", (did,))
        db.ex("UPDATE evolve_challengers SET status='expired' WHERE deployment_id=?", (did,))


def ap_env_val():
    from aiquant import research
    return research.get_split()["val_start"]
