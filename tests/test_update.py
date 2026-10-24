"""版本跟踪与安全重启：记录运行 commit、发现磁盘新版本、暂停新开仓 → 等未完成订单 → 备份 → 重启 → 恢复并记录。"""
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from aiquant import build, db, safe_restart
from aiquant.paper import runner as pr


def test_disk_build_from_file_and_commit_count(tmp_path):
    hist = [f"{i:040x}" for i in range(5, 0, -1)]           # 新的在前
    (tmp_path / "BUILD_INFO.json").write_text(json.dumps({"commit": hist[0], "subject": "新功能", "history": hist}))
    b = build.disk_build(tmp_path)
    assert b["source"] == "file" and b["short"] == hist[0][:7] and b["subject"] == "新功能"
    assert build.commits_between(hist[3], hist[0], root=tmp_path, history=hist) == 3
    assert build.commits_between("deadbeef", hist[0], root=tmp_path, history=hist) is None
    assert build.disk_build(tmp_path / "nope") is None


def test_disk_build_from_git(tmp_path):
    def g(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    g("init", "-q")
    g("config", "user.email", "t@t"); g("config", "user.name", "t")
    shas = []
    for i in range(3):
        (tmp_path / "f.txt").write_text(str(i))
        g("add", "."); g("commit", "-qm", f"提交{i}")
        shas.append(subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip())
    b = build.disk_build(tmp_path)
    assert b["source"] == "git" and b["commit"] == shas[-1] and b["subject"] == "提交2"
    assert build.commits_between(shas[0], shas[-1], root=tmp_path) == 2


def test_pending_update_detected_and_cleared(env, monkeypatch):
    hist = ["c" * 40, "b" * 40, "a" * 40]
    monkeypatch.setattr(build, "disk_build", lambda root=None: {"commit": hist[2], "short": hist[2][:7], "subject": "旧",
                                                               "history": hist[2:]})
    build.record_running()
    assert build.running_build()["commit"] == hist[2] and build.check_pending() is None
    monkeypatch.setattr(build, "disk_build", lambda root=None: {"commit": hist[0], "short": hist[0][:7], "subject": "新",
                                                               "history": hist})
    pu = build.check_pending()
    assert pu["count"] == 2 and pu["disk_short"] == "ccccccc" and pu["running_short"] == "aaaaaaa"
    assert build.pending_update()["count"] == 2
    build.record_running()                                   # 重启后运行版本 = 磁盘版本
    assert build.pending_update() is None and build.check_pending() is None


@pytest.fixture()
def live_dep(env):
    from aiquant.strategy import registry
    v = registry.freeze(None, {"name": "重启测试", "signal_tf": 60,
                               "indicators": [{"id": "f", "type": "EMA", "params": {"period": 9}},
                                              {"id": "s", "type": "EMA", "params": {"period": 21}}],
                               "entry": {"mode": "all", "conditions": [{"left": {"ref": "f"}, "op": ">", "right": {"ref": "s"}}]},
                               "exit": {"mode": "any", "conditions": [{"left": {"ref": "f"}, "op": "<", "right": {"ref": "s"}}]}},
                        source="test", new_lineage=True, set_current=False)
    did = db.ex("INSERT INTO paper_deployments(version_id,config_json,broker,status,created_at,updated_at,is_test) "
                "VALUES(?,?,?,?,?,?,0)", (v["id"], json.dumps({"label": "重启测试", "budget_usd": 1000}), "alpaca_paper",
                                          "running", db.now_iso(), db.now_iso()))
    db.set_setting("safe_restart", None)
    yield did
    db.ex("UPDATE paper_orders SET status='filled' WHERE deployment_id=?", (did,))
    pr.stop(did)
    db.set_setting("safe_restart", None)


def _open_order(did):
    return db.ex("INSERT INTO paper_orders(deployment_id,client_order_id,broker,side,qty,status,filled_qty,signal_ts,"
                 "created_at,updated_at,is_test) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                 (did, f"sr-{did}-{datetime.now().timestamp()}", "alpaca_paper", "buy", 0.01, "submitted", 0, "x",
                  db.now_iso(), db.now_iso()))


def test_safe_restart_waits_for_orders_then_resumes(live_dep, monkeypatch):
    did = live_dep
    with pytest.raises(PermissionError):
        safe_restart.request(actor="t", role="trader")
    oid = _open_order(did)
    st = safe_restart.request(actor="boss", role="admin", max_wait_min=30)
    assert did in st["paused"] and safe_restart.active()
    d = pr.get_deployment(did)
    assert d["status"] == "paused_entries" and d["config"]["restart_pause"] is True
    with pytest.raises(ValueError):
        safe_restart.request(actor="boss", role="admin")
    assert safe_restart.tick() is None                        # 还有未完成订单：继续等
    assert safe_restart.state()["open_orders"] >= 1
    db.ex("UPDATE paper_orders SET status='filled' WHERE id=?", (oid,))
    assert safe_restart.tick() == "restart"                   # 订单结束：备份并重启
    st = safe_restart.state()
    assert st["phase"] == "restarting" and st.get("backup") and "全部结束" in st["note"]
    monkeypatch.setattr(build, "running_build", lambda: {"commit": "f" * 40, "short": "fffffff", "subject": "新版本"})
    msg = safe_restart.on_startup()                          # 新进程启动
    assert "已更新到 commit fffffff" in msg
    d = pr.get_deployment(did)
    assert d["status"] == "running" and "restart_pause" not in d["config"]
    assert safe_restart.state() is None
    assert db.q1("SELECT * FROM paper_events WHERE message LIKE '%已更新到 commit fffffff%'")


def test_safe_restart_timeout_and_cancel(live_dep):
    did = live_dep
    _open_order(did)
    safe_restart.request(actor="boss", role="admin", max_wait_min=5)
    later = datetime.now(timezone.utc) + timedelta(minutes=6)
    assert safe_restart.tick(later) == "restart"
    assert "超过 5 分钟" in safe_restart.state()["note"]
    db.set_setting("safe_restart", None)
    db.ex("UPDATE paper_orders SET status='filled' WHERE deployment_id=?", (did,))
    db.ex("UPDATE paper_deployments SET status='running' WHERE id=?", (did,))
    safe_restart.request(actor="boss", role="admin")
    safe_restart.cancel(actor="boss", role="admin")
    d = pr.get_deployment(did)
    assert d["status"] == "running" and not safe_restart.active()
    with pytest.raises(ValueError):
        safe_restart.request(actor="boss", role="admin", max_wait_min=0)


def test_etf_skips_new_orders_during_safe_restart(env):
    from aiquant.paper import etf_trend
    db.set_setting("safe_restart", {"phase": "waiting", "at": db.now_iso(), "max_wait_min": 30, "paused": []})
    try:
        assert "安全重启" in etf_trend.step().get("skipped", "")
    finally:
        db.set_setting("safe_restart", None)


def test_watchdog_hands_over_to_restart(monkeypatch):
    import importlib
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    svc = importlib.import_module("service")
    spawned = []
    monkeypatch.setattr(svc, "_spawn", lambda cmd, **kw: spawned.append(cmd))
    monkeypatch.setattr(build, "check_pending", lambda: None)
    monkeypatch.setattr(safe_restart, "tick", lambda: None)
    assert svc._update_tick() is False and not spawned
    monkeypatch.setattr(safe_restart, "tick", lambda: "restart")
    assert svc._update_tick() is True and spawned[-1][-1] == "restart"
    assert callable(svc.restart)
