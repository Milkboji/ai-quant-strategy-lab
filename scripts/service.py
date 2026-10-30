"""一键启动 / 停止 / 状态检查（Windows 与 Linux 通用）。只监听 127.0.0.1，不部署公网。"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import urllib.error
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
STOP_FLAG = ROOT / "logs" / "stopped.flag"      # 用户主动停止：后台保活不再自动拉起
START_LOCK = ROOT / "logs" / "starting.lock"    # 防止开机自启 / 定时保活 / 桌面图标同时启动
NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}   # 后台运行时不闪黑窗口
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
            out = subprocess.run("netstat -ano", capture_output=True, text=True, shell=True, **NO_WINDOW).stdout
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


def _detach_kw(breakaway: bool = True) -> dict:
    if os.name == "nt":
        f = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
        if breakaway:
            f |= 0x01000000   # BREAKAWAY_FROM_JOB：不随启动它的计划任务 / 父程序一起被关闭
        return {"creationflags": f}
    return {"start_new_session": True}


def _spawn(cmd: list, **kw) -> subprocess.Popen:
    try:
        return subprocess.Popen(cmd, **kw, **_detach_kw(True))
    except OSError:   # 所在作业不允许脱离时退回普通方式
        return subprocess.Popen(cmd, **kw, **_detach_kw(False))


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
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, **NO_WINDOW)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        pass


def _launch_app() -> subprocess.Popen:
    # 先启动后台交易循环再启动网页（scripts/run_server.py），没人打开网页也照常自动交易
    cmd = [sys.executable, str(ROOT / "scripts" / "run_server.py")]
    logf = open(LOG, "a", encoding="utf-8")
    p = _spawn(cmd, cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT)
    PID.write_text(str(p.pid))
    return p


# ---------------------------------------------------------------------------
# Cloudflare Tunnel（外网访问）：配置好 config/cloudflared.yml 后随服务一起运行，掉线自动拉起
# ---------------------------------------------------------------------------
CF_EXE = ROOT / "tools" / "cloudflared.exe"
CF_CFG = ROOT / "config" / "cloudflared.yml"
CF_PID = ROOT / "logs" / "cloudflared.pid"
CF_LOG = ROOT / "logs" / "cloudflared.log"
CF_METRICS = "127.0.0.1:20241"          # cloudflared 本地状态接口：/ready 在至少有 1 条通道连接时返回 200
CF_STALL_CHECKS = 3                     # 连续 3 次（约 3 分钟）没有可用通道 → 重启 cloudflared
CF_ERR = ROOT / "logs" / "cloudflared.stderr.log"
# 默认 HTTP/2：实测这台电脑的网络每约 12 分钟断一次，HTTP/2 约 10 秒重连，QUIC 要 100 秒左右。
# 可用环境变量 AIQUANT_TUNNEL_PROTOCOL=quic 试用 QUIC；QUIC 启动失败或连不上时自动退回 HTTP/2
_cf_state = {"started": 0.0, "fails": 0, "proto": os.environ.get("AIQUANT_TUNNEL_PROTOCOL", "http2"), "quick_exits": 0}


def tunnel_configured() -> bool:
    return CF_CFG.exists() and (CF_EXE.exists() or os.name != "nt")


def _cf_pid() -> int | None:
    try:
        return int(CF_PID.read_text())
    except Exception:
        return None


def _fallback(reason: str) -> None:
    if _cf_state["proto"] != "http2":
        _cf_state["proto"] = "http2"
        tail = ""
        try:
            tail = CF_ERR.read_text(encoding="utf-8", errors="ignore").strip().splitlines()[-1][:200]
        except Exception:
            pass
        _wlog(f"外网通道 QUIC 不可用（{reason}），改用 HTTP/2{('：' + tail) if tail else ''}")


def _spawn_tunnel(exe: str) -> subprocess.Popen:
    err = open(CF_ERR, "ab")   # 配置错误等启动失败信息（--logfile 生效之前就退出的情况）
    try:
        return _spawn([exe, "tunnel", "--no-autoupdate", "--loglevel", "info", "--logfile", str(CF_LOG),
                       "--metrics", CF_METRICS, "--protocol", _cf_state["proto"], "--config", str(CF_CFG), "run"],
                      cwd=str(ROOT), stdout=err, stderr=err)
    finally:
        err.close()


def ensure_tunnel() -> None:
    if not tunnel_configured() or _alive(_cf_pid()):
        return
    if _cf_state["started"] and time.time() - _cf_state["started"] < 60:   # 刚启动就退出
        _cf_state["quick_exits"] += 1
        if _cf_state["proto"] == "quic":
            _fallback("启动后立即退出")
    exe = str(CF_EXE) if CF_EXE.exists() else "cloudflared"
    # 日志只保留最近一份，避免无限增长
    try:
        if CF_LOG.exists() and CF_LOG.stat().st_size > 5_000_000:
            CF_LOG.replace(CF_LOG.with_suffix(".log.1"))
        if CF_ERR.exists() and CF_ERR.stat().st_size > 1_000_000:
            CF_ERR.unlink()
    except Exception:
        pass
    p = _spawn_tunnel(exe)
    if _cf_state["proto"] == "quic":
        for _ in range(16):                    # 8 秒内退出 = QUIC 在这台电脑上不可用 → 立刻改用 HTTP/2
            time.sleep(0.5)
            if p.poll() is not None:
                _fallback(f"启动后立即退出，退出码 {p.returncode}")
                p = _spawn_tunnel(exe)
                break
    CF_PID.write_text(str(p.pid))
    _cf_state.update(started=time.time(), fails=0)
    _wlog(f"外网通道（Cloudflare Tunnel）已启动，PID {p.pid}，协议 {_cf_state['proto']}")


def tunnel_ready() -> bool | None:
    """cloudflared 是否至少有一条到 Cloudflare 的可用连接（None = 状态接口不可用，无法判断）。"""
    try:
        with urllib.request.urlopen(f"http://{CF_METRICS}/ready", timeout=3) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False
    except Exception:
        return None


TUNNEL_ALERT_SEC = 60     # 外网通道连续断开超过这么久才发通知（避免每 12 分钟的短暂抖动刷屏）


def _notify(kind: str, title: str, body: str, dedupe: str | None = None) -> None:
    """守护进程发通知：写入发件箱并尝试发送（发不出去时由主程序每分钟重试）。任何错误都不影响守护。"""
    try:
        from aiquant import notify
        notify.event(kind, title, body, dedupe=dedupe)
    except Exception as e:  # noqa: BLE001
        _wlog(f"通知发送失败：{type(e).__name__}: {e}")


def _tunnel_down_tick(ok: bool, now: float | None = None) -> str | None:
    """记录通道断开时长；断开超过 TUNNEL_ALERT_SEC 发一次通知，恢复时再发一条。返回本次发出的通知类型。"""
    now = now or time.time()
    if ok:
        since, alerted = _cf_state.get("down_since"), _cf_state.get("down_alerted")
        _cf_state["down_since"], _cf_state["down_alerted"] = None, False
        if since and alerted:
            _notify("tunnel_down", "外网通道已恢复", f"断开约 {int(now - since)} 秒后恢复，外网地址可以正常访问。")
            return "recovered"
        return None
    if not _cf_state.get("down_since"):
        _cf_state["down_since"] = now
        return None
    if not _cf_state.get("down_alerted") and now - _cf_state["down_since"] >= TUNNEL_ALERT_SEC:
        _cf_state["down_alerted"] = True
        _notify("tunnel_down", "外网通道断开超过 60 秒",
                f"已断开约 {int(now - _cf_state['down_since'])} 秒。系统和自动交易在电脑上照常运行，守护进程正在自动重连；"
                "本机访问 http://127.0.0.1:8501 不受影响。")
        return "down"
    return None


def check_tunnel() -> None:
    """通道自愈：连接全部掉线且几分钟内没有自己恢复时，重启 cloudflared。"""
    if not tunnel_configured() or not _alive(_cf_pid()):
        return
    if time.time() - _cf_state["started"] < 90:      # 刚启动，给它时间建立连接
        return
    ok = tunnel_ready()
    if ok is None:                                    # 旧进程没有状态接口：重启一次换成新参数
        if _cf_state["started"] == 0.0:
            _wlog("外网通道没有状态接口（旧进程），重启以启用自动检测")
            stop_tunnel(); ensure_tunnel()
        return
    _tunnel_down_tick(bool(ok))
    if ok:
        if _cf_state["fails"]:
            _wlog("外网通道已恢复")
        _cf_state["fails"] = 0
        return
    _cf_state["fails"] += 1
    if _cf_state["proto"] == "quic" and _cf_state["fails"] >= 2:
        _fallback("连续 2 次检查没有可用连接")
        stop_tunnel()
        ensure_tunnel()
        return
    if _cf_state["fails"] >= CF_STALL_CHECKS:
        _wlog(f"外网通道连续 {_cf_state['fails']} 次检查没有可用连接，重启 cloudflared")
        stop_tunnel()
        ensure_tunnel()


def stop_tunnel() -> None:
    _kill(_cf_pid())
    CF_PID.unlink(missing_ok=True)


def _update_tick() -> bool:
    """比较磁盘与运行中的代码版本；安全重启条件满足时启动 stop + start 并让本守护进程退出。返回是否已交给重启。"""
    try:
        from aiquant import build, safe_restart
        pu = build.check_pending()
        if pu and pu.get("disk_commit") != (_cf_state.get("last_pending") or ""):
            _cf_state["last_pending"] = pu["disk_commit"]
            _wlog(f"发现待生效的更新：运行 {pu.get('running_short') or '?'} → 磁盘 {pu['disk_short']}（{pu.get('count')} 个提交）")
        if safe_restart.tick() == "restart":
            _wlog("安全重启：未完成订单已处理、数据库已备份，执行 stop + start")
            _spawn([sys.executable, str(Path(__file__).resolve()), "restart"], cwd=str(ROOT),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except Exception as e:  # noqa: BLE001
        _wlog(f"版本检查 / 安全重启出错：{type(e).__name__}: {e}")
    return False


def restart() -> int:
    """安全重启的最后一步：stop + start（不打开浏览器）。由守护进程在条件满足后调用。"""
    _wlog("执行重启：stop")
    stop()
    time.sleep(2)
    _wlog("执行重启：start")
    rc = start(open_browser=False)
    _wlog(f"重启完成（返回码 {rc}）")
    return rc


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
        try:
            ensure_tunnel()
            check_tunnel()
        except Exception as e:  # noqa: BLE001
            _wlog(f"外网通道启动/检测失败：{e}")
        if _update_tick():
            return 0
        pid = _pid()
        mem = _mem_mb(_app_pid() or _port_pid() or pid)
        if time.time() - last_mem_log >= 600:
            _wlog(f"状态：{'正常' if healthy() else '无响应'}，PID {pid}，内存 {mem} MB")
            last_mem_log = time.time()
        reason = None
        if not _alive(pid) and healthy():
            # PID 文件里的进程已退出，但端口上有一个正常响应的实例（例如开机自启和保活同时启动了两个）：
            # 接管它，不再启动新进程（否则新进程会因端口被占退出，每分钟循环一次）
            live = _port_pid() or _app_pid()
            if live and _alive(live):
                PID.write_text(str(live))
                _wlog(f"接管正在运行的实例（PID {live}），不重复启动")
                pid = live
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
            ok_now = healthy()
            _wlog(f"重启{'成功' if ok_now else '后仍无响应，下轮继续检查'}（新 PID {p.pid}）")
            _notify("watchdog_restart", "守护进程自动重启了系统",
                    f"原因：{reason}\n结果：{'重启成功，模拟盘已自动对账恢复' if ok_now else '重启后仍无响应，下一轮继续检查'}")
            fails = 0


def _start_watchdog() -> None:
    try:
        ensure_tunnel()
    except Exception as e:  # noqa: BLE001
        _wlog(f"外网通道启动失败：{e}")
    try:
        wp = int(WPID.read_text()) if WPID.exists() else None
    except Exception:
        wp = None
    if _alive(wp):
        return
    _spawn([sys.executable, str(Path(__file__).resolve()), "watch"], cwd=str(ROOT),
           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _lock() -> bool:
    """获取启动锁；120 秒内已有其他启动在进行时返回 False。"""
    try:
        if START_LOCK.exists() and time.time() - START_LOCK.stat().st_mtime < 120:
            return False
        START_LOCK.write_text(str(os.getpid()))
        return True
    except Exception:
        return True


def _msgbox(text: str, title: str = "AI 量化交易") -> None:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
        except Exception:
            pass


def ensure() -> int:
    """后台保活（开机自启与每 5 分钟的计划任务调用）：没在运行就启动，不打开浏览器。用户手动停止后不启动。"""
    (ROOT / "logs").mkdir(exist_ok=True)
    if STOP_FLAG.exists():
        return 0
    if healthy():
        _start_watchdog()
        return 0
    if not _lock():
        return 0
    try:
        _wlog("后台保活：服务未运行，自动启动")
        return start(open_browser=False)
    finally:
        START_LOCK.unlink(missing_ok=True)


def open_ui() -> int:
    """桌面图标：确保服务运行（必要时启动），就绪后打开浏览器。"""
    (ROOT / "logs").mkdir(exist_ok=True)
    STOP_FLAG.unlink(missing_ok=True)
    if not healthy():
        if _lock():
            try:
                start(open_browser=False)
            finally:
                START_LOCK.unlink(missing_ok=True)
        for _ in range(120):          # 另一个启动正在进行：等它就绪
            if healthy():
                break
            time.sleep(1)
    if healthy():
        _start_watchdog()
        webbrowser.open(URL)
        return 0
    _msgbox(f"量化系统启动失败。\n请查看日志：{LOG}")
    return 1


def start(open_browser: bool = True) -> int:
    (ROOT / "logs").mkdir(exist_ok=True)
    STOP_FLAG.unlink(missing_ok=True)
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
    (ROOT / "logs").mkdir(exist_ok=True)
    STOP_FLAG.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))   # 手动停止后，后台保活不会再自动启动
    WPID.unlink(missing_ok=True)   # 先让守护进程退出，避免它把服务重新拉起
    stop_tunnel()
    pid = int(PID.read_text()) if PID.exists() else None
    if pid:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, **NO_WINDOW)
            else:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            pass
        PID.unlink(missing_ok=True)
    if os.name == "nt" and healthy():
        out = subprocess.run("netstat -ano", capture_output=True, text=True, shell=True, **NO_WINDOW).stdout
        for line in out.splitlines():
            if "127.0.0.1:8501" in line and "LISTENING" in line:
                subprocess.run(["taskkill", "/PID", line.split()[-1], "/T", "/F"], capture_output=True, **NO_WINDOW)
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
    rd = tunnel_ready() if tunnel_configured() and _alive(_cf_pid()) else None
    print(f"外网通道      : {('运行中' if _alive(_cf_pid()) else '未运行') if tunnel_configured() else '未配置'}"
          f"{'（已连接 Cloudflare）' if rd else ('（暂时没有可用连接）' if rd is False else '')}  日志 {CF_LOG}")
    print(f"PAPER_ORDERS  : {'已解锁' if http.paper_orders_unlocked() else '0 (锁定)'}")
    print(f"LIVE_ORDERS   : 0 (永久禁用)")
    print(f"Alpaca 行情Key: {config.mask(config.secret('ALPACA_API_KEY_ID'))}")
    print(f"Alpaca Paper  : {config.mask(config.secret('ALPACA_PAPER_KEY_ID'))}")
    print(f"DeepSeek Key  : {config.mask(config.secret('DEEPSEEK_API_KEY'))}")
    print(f"日志          : {LOG}")
    return 0 if ok else 1


if __name__ == "__main__":
    a = sys.argv[1] if len(sys.argv) > 1 else "status"
    sys.exit({"start": start, "stop": stop, "status": status, "watch": watch, "ensure": ensure,
              "open": open_ui, "restart": restart}[a]())
