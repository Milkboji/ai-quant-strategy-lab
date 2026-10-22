"""一键启动 / 停止 / 状态检查（Windows 与 Linux 通用）。只监听 127.0.0.1，不部署公网。"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PID = ROOT / "logs" / "streamlit.pid"
LOG = ROOT / "logs" / "streamlit.log"
WPID = ROOT / "logs" / "watchdog.pid"
WLOG = ROOT / "logs" / "watchdog.log"
CHECK_SEC = int(os.environ.get("AIQUANT_WATCH_SEC", "60"))   # 健康检查间隔
FAILS_BEFORE_RESTART = 3  # 连续多少次无响应后重启
MEM_LIMIT_MB = int(os.environ.get("AIQUANT_MEM_LIMIT_MB", "2500"))   # 内存超过此值主动重启
URL = "http://127.0.0.1:8501"
sys.path.insert(0, str(ROOT))


def healthy() -> bool:
    try:
        with urllib.request.urlopen(URL + "/_stcore/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 进程工具（不依赖第三方库）
# ---------------------------------------------------------------------------

def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259     # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _mem_mb(pid: int | None) -> int | None:
    """进程工作集内存（MB）。"""
    if not pid:
        return None
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            h = ctypes.windll.kernel32.OpenProcess(0x1000 | 0x0010, False, pid)
            if not h:
                return None
            c = PMC(); c.cb = ctypes.sizeof(PMC)
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb)
            ctypes.windll.kernel32.CloseHandle(h)
            return int(max(c.WorkingSetSize, c.PagefileUsage) / 1048576) if ok else None
        return int(open(f"/proc/{pid}/status").read().split("VmRSS:")[1].split()[0]) // 1024
    except Exception:
        return None


def _port_pid() -> int | None:
    """实际监听 8501 端口的进程（Windows 虚拟环境的 python.exe 是启动器，真正占内存的是它的子进程）。"""
    try:
        if os.name == "nt":
            out = subprocess.run("netstat -ano", capture_output=True, text=True, shell=True).stdout
            for line in out.splitlines():
                if "127.0.0.1:8501" in line and "LISTENING" in line:
                    return int(line.split()[-1])
        return None
    except Exception:
        return None


def _app_pid() -> int | None:
    """服务进程自己写下的真实进程号（logs/app.pid）。"""
    try:
        p = int((ROOT / "logs" / "app.pid").read_text())
        return p if _alive(p) else None
    except Exception:
        return None


def _detach_kw() -> dict:
    if os.name == "nt":
        return {"creationflags": 0x00000008 | 0x00000200 | 0x08000000}  # DETACHED | NEW_GROUP | NO_WINDOW
    return {"start_new_session": True}


def _wlog(msg: str) -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    with open(WLOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg + "\n")


def _pid() -> int | None:
    try:
        return int(PID.read_text())
    except Exception:
        return None


def _kill(pid: int | None) -> None:
    if not pid:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        pass


def _launch_app() -> subprocess.Popen:
    cmd = [sys.executable, "-m", "streamlit", "run", str(ROOT / "app.py"), "--server.address", "127.0.0.1",
           "--server.port", "8501", "--server.headless", "true"]
    logf = open(LOG, "a", encoding="utf-8")
    p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT, **_detach_kw())
    PID.write_text(str(p.pid))
    return p


def watch() -> int:
    """守护进程：服务无响应、进程退出或内存过高时自动重启（模拟盘会在重启后自动对账恢复）。"""
    WPID.write_text(str(os.getpid()))
    _wlog(f"守护进程启动（每 {CHECK_SEC} 秒检查；内存上限 {MEM_LIMIT_MB} MB）")
    fails, last_mem_log = 0, 0.0
    while True:
        time.sleep(CHECK_SEC)
        if not WPID.exists() or WPID.read_text().strip() != str(os.getpid()):
            _wlog("收到停止信号，守护进程退出")
            return 0
        pid = _pid()
        mem = _mem_mb(_app_pid() or _port_pid() or pid)
        if time.time() - last_mem_log >= 600:
            _wlog(f"状态：{'正常' if healthy() else '无响应'}，PID {pid}，内存 {mem} MB")
            last_mem_log = time.time()
        reason = None
        if not _alive(pid):
            reason = "服务进程已退出"
        elif mem and mem > MEM_LIMIT_MB:
            reason = f"内存 {mem} MB 超过上限 {MEM_LIMIT_MB} MB"
        elif not healthy():
            fails += 1
            if fails >= FAILS_BEFORE_RESTART:
                reason = f"连续 {fails} 次健康检查无响应"
        else:
            fails = 0
        if reason:
            _wlog(f"自动重启：{reason}")
            _kill(pid)
            time.sleep(3)
            p = _launch_app()
            for _ in range(120):
                if healthy():
                    break
                time.sleep(1)
            _wlog(f"重启{'成功' if healthy() else '后仍无响应，下轮继续检查'}（新 PID {p.pid}）")
            fails = 0


def _start_watchdog() -> None:
    try:
        wp = int(WPID.read_text()) if WPID.exists() else None
    except Exception:
        wp = None
    if _alive(wp):
        return
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "watch"], cwd=str(ROOT),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_detach_kw())


def start(open_browser: bool = True) -> int:
    (ROOT / "logs").mkdir(exist_ok=True)
    if healthy():
        _start_watchdog()
        print(f"[OK] 服务已在运行：{URL}")
        if open_browser:
            webbrowser.open(URL)
        return 0
    try:
        from aiquant.maintenance import backup
        print(f"[..] 启动前备份数据库 → {backup()}")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 备份失败：{e}")
    p = _launch_app()
    for _ in range(90):
        if healthy():
            _start_watchdog()
            print(f"[OK] 已启动：{URL}  (PID {p.pid})  日志：{LOG}")
            print("[OK] 守护进程已开启：服务无响应或内存过高时会自动重启（记录在 logs/watchdog.log）")
            if open_browser:
                webbrowser.open(URL)
            return 0
        if p.poll() is not None:
            break
        time.sleep(1)
    print(f"[ERR] 启动失败，请查看日志：{LOG}")
    return 1


def stop() -> int:
    WPID.unlink(missing_ok=True)   # 先让守护进程退出，避免它把服务重新拉起
    pid = int(PID.read_text()) if PID.exists() else None
    if pid:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            else:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            pass
        PID.unlink(missing_ok=True)
    if os.name == "nt" and healthy():
        out = subprocess.run("netstat -ano", capture_output=True, text=True, shell=True).stdout
        for line in out.splitlines():
            if "127.0.0.1:8501" in line and "LISTENING" in line:
                subprocess.run(["taskkill", "/PID", line.split()[-1], "/T", "/F"], capture_output=True)
    time.sleep(1)
    print("[OK] 已停止" if not healthy() else "[WARN] 端口 8501 仍有服务在运行")
    return 0


def status() -> int:
    from aiquant import config
    from aiquant.data import http
    ok = healthy()
    print(f"Dashboard     : {'运行中' if ok else '未运行'}  {URL}")
    print(f"PID 文件      : {PID.read_text() if PID.exists() else '-'}  内存 {_mem_mb(_app_pid() or _pid())} MB")
    wp = int(WPID.read_text()) if WPID.exists() else None
    print(f"守护进程      : {'运行中' if _alive(wp) else '未运行'}  日志 {WLOG}")
    print(f"PAPER_ORDERS  : {'已解锁' if http.paper_orders_unlocked() else '0 (锁定)'}")
    print(f"LIVE_ORDERS   : 0 (永久禁用)")
    print(f"Alpaca 行情Key: {config.mask(config.secret('ALPACA_API_KEY_ID'))}")
    print(f"Alpaca Paper  : {config.mask(config.secret('ALPACA_PAPER_KEY_ID'))}")
    print(f"DeepSeek Key  : {config.mask(config.secret('DEEPSEEK_API_KEY'))}")
    print(f"日志          : {LOG}")
    return 0 if ok else 1


if __name__ == "__main__":
    a = sys.argv[1] if len(sys.argv) > 1 else "status"
    sys.exit({"start": start, "stop": stop, "status": status, "watch": watch}[a]())
