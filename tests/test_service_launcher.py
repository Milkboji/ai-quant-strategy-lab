"""后台保活 / 桌面图标启动逻辑（不真正启动服务）。"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("svc_under_test", ROOT / "scripts" / "service.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    monkeypatch.setattr(m, "ROOT", tmp_path)
    (tmp_path / "logs").mkdir()
    for k in ("STOP_FLAG", "START_LOCK", "WLOG", "WPID", "PID", "LOG"):
        monkeypatch.setattr(m, k, tmp_path / "logs" / getattr(m, k).name)
    calls = {"start": 0, "browser": 0, "watchdog": 0}
    state = {"up": False}
    monkeypatch.setattr(m, "healthy", lambda: state["up"])

    def fake_start(open_browser=True):
        calls["start"] += 1
        m.STOP_FLAG.unlink(missing_ok=True)
        state["up"] = True
        return 0
    monkeypatch.setattr(m, "start", fake_start)
    monkeypatch.setattr(m, "_start_watchdog", lambda: calls.__setitem__("watchdog", calls["watchdog"] + 1))
    monkeypatch.setattr(m.webbrowser, "open", lambda u: calls.__setitem__("browser", calls["browser"] + 1))
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    return m, calls, state


def test_ensure_starts_when_down_and_not_when_up(svc):
    m, calls, state = svc
    assert m.ensure() == 0 and calls["start"] == 1 and calls["browser"] == 0
    assert not m.START_LOCK.exists()
    m.ensure()
    assert calls["start"] == 1 and calls["watchdog"] >= 1     # 已在运行：只确保守护进程


def test_manual_stop_pauses_keepalive_until_opened(svc):
    m, calls, state = svc
    m.STOP_FLAG.write_text("x")
    m.ensure()
    assert calls["start"] == 0                                # 用户主动停止：不自动拉起
    assert m.open_ui() == 0
    assert calls["start"] == 1 and calls["browser"] == 1 and not m.STOP_FLAG.exists()


def test_concurrent_start_is_skipped(svc):
    m, calls, state = svc
    m.START_LOCK.write_text("123")                            # 另一个启动正在进行
    m.ensure()
    assert calls["start"] == 0
