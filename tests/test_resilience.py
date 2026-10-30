"""2026-10-30 事故的修复：行情缓存损坏自动恢复、同一时间只有一个交易循环、守护进程接管已在运行的实例。"""
import gzip
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone

import pandas as pd

from aiquant import config, db
from aiquant.data import market_data as md

VENUE, SYM = "corrupt_test", "BTC/USD"


def _bars(n, start="2026-10-01"):
    ts = pd.date_range(start, periods=n, freq="5min", tz="UTC")
    px = 100 + pd.Series(range(n), dtype=float) * 0.01
    return pd.DataFrame({"ts": ts, "open": px, "high": px + 0.05, "low": px - 0.05, "close": px, "volume": 1.0,
                         "trade_count": 1, "vwap": px})


def test_corrupt_master_cache_is_quarantined_salvaged_and_coverage_trimmed(env):
    p = md._master_path(VENUE, SYM)
    df = _bars(20000)
    md._save_master(df, VENUE, SYM)
    s, e = df["ts"].iloc[0].to_pydatetime(), (df["ts"].iloc[-1] + pd.Timedelta(minutes=5)).to_pydatetime()
    md._set_coverage([(s, e)], VENUE, SYM)
    raw = bytearray(p.read_bytes())
    mid = len(raw) // 2
    raw[mid:mid + 4096] = b"\x00" * 4096                 # 和事故里一样：中间一段被写成 0
    p.write_bytes(bytes(raw))
    md._master_cache.clear()
    got = md.load_master(VENUE, SYM)
    assert 0 < len(got) < len(df)                         # 读出了损坏位置之前的连续一段
    assert (got["ts"].diff().dropna() == pd.Timedelta(minutes=5)).all()
    bad = list(p.parent.glob(p.name + ".corrupt-*"))
    assert len(bad) == 1                                  # 原文件隔离保留
    with gzip.open(p, "rt") as f:                         # 新文件可以完整读出
        assert len(pd.read_csv(f)) == len(got)
    cov = md.get_coverage(VENUE, SYM)
    last = got["ts"].max() + pd.Timedelta(minutes=5)
    assert cov == [(s, last.to_pydatetime())]             # 覆盖区间截到读出的部分 → 后面的自动重新下载
    gaps = md.missing_ranges(s, e, cov)
    assert gaps == [(last.to_pydatetime(), e)]
    ev = db.q1("SELECT message FROM paper_events ORDER BY id DESC LIMIT 1")
    assert "行情缓存文件损坏" in ev["message"] and "自动重新下载" in ev["message"]
    for x in bad:
        x.unlink()


def test_save_master_uses_per_process_temp_file(env, monkeypatch):
    names = []
    real = pd.DataFrame.to_csv

    def spy(self, path, *a, **k):
        names.append(str(path))
        return real(self, path, *a, **k)
    monkeypatch.setattr(pd.DataFrame, "to_csv", spy)
    md._save_master(_bars(10), VENUE, SYM)
    assert names and f".{os.getpid()}." in names[-1] and names[-1].endswith(".tmp")
    assert not list(md._master_path(VENUE, SYM).parent.glob("*.tmp"))


def test_only_one_process_runs_the_trading_loop(env, tmp_path):
    """第二个进程拿不到交易循环锁；第一个进程退出后（系统自动释放）就能拿到。"""
    from aiquant import proc_lock
    assert proc_lock.acquire("loop_test")
    code = textwrap.dedent(f"""
        import os, sys
        os.environ["AIQUANT_ROOT"] = {str(config.ROOT)!r}
        sys.path.insert(0, {str(config.ROOT.parent if False else os.getcwd())!r})
        sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})
        from aiquant import proc_lock
        print("GOT" if proc_lock.acquire("loop_test") else "BUSY")
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == "BUSY", r.stderr
    assert proc_lock.holder("loop_test") == os.getpid()
    proc_lock.release("loop_test")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == "GOT", r.stderr


def test_run_server_exits_when_port_already_serving(monkeypatch):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import importlib
    rs = importlib.import_module("run_server")
    started = []
    monkeypatch.setattr(rs, "_already_serving", lambda: True)
    import aiquant.actions as actions
    monkeypatch.setattr(actions, "init", lambda: started.append("init"))
    rs.main()
    assert started == []                                   # 没有启动交易循环，也没有启动网页


def test_watchdog_adopts_healthy_instance_instead_of_relaunching(monkeypatch, tmp_path):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import importlib
    svc = importlib.import_module("service")
    monkeypatch.setattr(svc, "PID", tmp_path / "streamlit.pid")
    monkeypatch.setattr(svc, "WPID", tmp_path / "watchdog.pid")
    monkeypatch.setattr(svc, "WLOG", tmp_path / "watchdog.log")
    (tmp_path / "streamlit.pid").write_text("111")
    monkeypatch.setattr(svc, "CHECK_SEC", 0)
    alive = {222}
    monkeypatch.setattr(svc, "_alive", lambda p: p in alive)
    monkeypatch.setattr(svc, "healthy", lambda: True)
    monkeypatch.setattr(svc, "_port_pid", lambda: 222)
    monkeypatch.setattr(svc, "_app_pid", lambda: None)
    monkeypatch.setattr(svc, "_mem_mb", lambda p: 100)
    monkeypatch.setattr(svc, "ensure_tunnel", lambda: None)
    monkeypatch.setattr(svc, "check_tunnel", lambda: None)
    launched = []
    monkeypatch.setattr(svc, "_launch_app", lambda: launched.append(1))
    calls = {"n": 0}

    def upd():
        calls["n"] += 1
        return calls["n"] > 1          # 第二轮让守护进程退出
    monkeypatch.setattr(svc, "_update_tick", upd)
    monkeypatch.setattr(svc.time, "sleep", lambda s: None)
    svc.watch()
    assert launched == []
    assert (tmp_path / "streamlit.pid").read_text() == "222"
    assert "接管正在运行的实例（PID 222）" in (tmp_path / "watchdog.log").read_text(encoding="utf-8")
