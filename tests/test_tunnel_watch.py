"""外网通道自愈：/ready 连续失败才重启；恢复后清零；没有状态接口的旧进程只重启一次。"""
import importlib.util
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("svc", ROOT / "scripts" / "service.py")
svc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(svc)


def _setup(monkeypatch, ready_seq):
    calls = {"restart": 0}
    seq = iter(ready_seq)
    monkeypatch.setattr(svc, "tunnel_configured", lambda: True)
    monkeypatch.setattr(svc, "_alive", lambda pid: True)
    monkeypatch.setattr(svc, "_cf_pid", lambda: 123)
    monkeypatch.setattr(svc, "tunnel_ready", lambda: next(seq))
    monkeypatch.setattr(svc, "_wlog", lambda m: None)
    monkeypatch.setattr(svc, "stop_tunnel", lambda: None)

    def ens():
        calls["restart"] += 1
        svc._cf_state.update(started=time.time() - 1000, fails=0)
    monkeypatch.setattr(svc, "ensure_tunnel", ens)
    svc._cf_state.update(started=time.time() - 1000, fails=0)
    return calls


def test_restart_only_after_consecutive_failures(monkeypatch):
    calls = _setup(monkeypatch, [False, False, True, False, False, False])
    for _ in range(6):
        svc.check_tunnel()
    assert calls["restart"] == 1


def test_grace_period_after_start(monkeypatch):
    calls = _setup(monkeypatch, [False] * 5)
    svc._cf_state.update(started=time.time(), fails=0)
    for _ in range(5):
        svc.check_tunnel()
    assert calls["restart"] == 0


def test_legacy_process_restarted_once(monkeypatch):
    calls = _setup(monkeypatch, [None, None, None])
    svc._cf_state.update(started=0.0, fails=0)
    for _ in range(3):
        svc.check_tunnel()
    assert calls["restart"] == 1
