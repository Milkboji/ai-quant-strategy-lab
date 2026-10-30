"""scripts/service.py：从 netstat 输出找出监听 8501 的进程（各种本地地址写法）。"""
import importlib.util
from pathlib import Path


def _svc():
    p = Path(__file__).resolve().parent.parent / "scripts" / "service.py"
    spec = importlib.util.spec_from_file_location("svc_script", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_listen_pids_matches_any_local_address():
    out = """
  协议  本地地址          外部地址        状态           PID
  TCP    0.0.0.0:8501           0.0.0.0:0              LISTENING       6724
  TCP    127.0.0.1:8501         0.0.0.0:0              LISTENING       9392
  TCP    [::]:8501              [::]:0                 LISTENING       6724
  TCP    127.0.0.1:8501         127.0.0.1:55000        ESTABLISHED     9392
  TCP    127.0.0.1:18501        0.0.0.0:0              LISTENING       111
  TCP    127.0.0.1:55000        127.0.0.1:8501         ESTABLISHED     222
"""
    assert _svc()._listen_pids(out) == [6724, 9392]


def test_netstat_output_in_gbk_is_decoded(monkeypatch):
    svc = _svc()
    raw = "  协议  本地地址\n  TCP    0.0.0.0:8501   0.0.0.0:0   LISTENING   6724\n".encode("gbk")

    class R:
        stdout = raw
    monkeypatch.setattr(svc.subprocess, "run", lambda *a, **k: R())
    assert svc._listen_pids(svc._netstat()) == [6724]
    assert svc._listen_pids(None) == []
