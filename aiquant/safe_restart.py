"""安全重启：让磁盘上的新代码生效，同时不打断模拟盘交易。

流程（管理员在全站顶部点「安全重启」发起）：
1. 暂停所有运行中策略的新开仓（只标记由本次重启暂停的部署，重启后恢复；持仓保留，照常按规则卖出）；美股 ETF 暂停下新单；
2. 守护进程每次自检检查未完成订单，全部结束（或等待超过最长时间）后备份数据库，执行 stop + start；
3. 新进程启动时恢复被暂停的策略，并在事件流记录“已更新到 commit xxx”。
状态存在 settings.safe_restart；最长等待时间 settings.safe_restart_config.max_wait_min（默认 30 分钟）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from . import db

log = logging.getLogger("aiquant.safe_restart")
DEFAULTS = {"max_wait_min": 30}
OPEN_PAPER = ("new", "submitted", "partially_filled", "accepted", "pending_new", "unknown_after_restart")
FINAL_ETF = ("filled", "canceled", "cancelled", "expired", "rejected")


def get_config() -> dict:
    c = dict(DEFAULTS)
    c.update(db.get_setting("safe_restart_config") or {})
    return c


def _require_admin(role: str | None) -> None:
    if role != "admin":
        raise PermissionError("只有管理员可以安全重启")


def state() -> dict | None:
    return db.get_setting("safe_restart")


def active() -> bool:
    s = state()
    return bool(s and s.get("phase") in ("waiting", "restarting"))


def open_orders() -> int:
    marks = ",".join("?" * len(OPEN_PAPER))
    n = db.q1(f"SELECT COUNT(*) AS n FROM paper_orders WHERE is_test=0 AND status IN ({marks})", OPEN_PAPER)["n"]
    try:
        fm = ",".join("?" * len(FINAL_ETF))
        n += db.q1(f"SELECT COUNT(*) AS n FROM etf_orders WHERE status IS NOT NULL AND status NOT IN ({fm}) "
                   "AND status NOT LIKE 'rejected%'", FINAL_ETF)["n"]
    except Exception:  # noqa: BLE001  ETF 表还没建
        pass
    return int(n)


def request(*, actor: str | None, role: str | None, max_wait_min: float | None = None) -> dict:
    """发起安全重启：暂停新开仓 → 等未完成订单结束 → 备份 → stop + start。"""
    from . import build, console
    from .paper import runner as pr
    _require_admin(role)
    if active():
        raise ValueError("安全重启已经在进行中")
    if max_wait_min is not None:
        if not 1 <= float(max_wait_min) <= 240:
            raise ValueError("最长等待时间需在 1~240 分钟之间")
        db.set_setting("safe_restart_config", dict(get_config(), max_wait_min=float(max_wait_min)))
    paused = []
    for d in db.q("SELECT id FROM paper_deployments WHERE broker='alpaca_paper' AND is_test=0 AND status='running'"):
        dep = pr.get_deployment(d["id"])
        cfg = dep["config"]
        cfg["restart_pause"] = True
        db.ex("UPDATE paper_deployments SET config_json=?, status='paused_entries', updated_at=? WHERE id=?",
              (json.dumps(cfg, ensure_ascii=False), db.now_iso(), dep["id"]))
        pr.event(dep["id"], "INFO", "安全重启：暂停新开仓，等待未完成订单结束（持仓保留，照常按规则卖出）")
        paused.append(dep["id"])
    pu = build.pending_update() or {}
    st = {"phase": "waiting", "requested_by": actor or "管理员", "at": db.now_iso(), "paused": paused,
          "max_wait_min": float(get_config()["max_wait_min"]), "target": pu.get("disk_short"),
          "from": (build.running_build() or {}).get("short"), "open_orders": open_orders()}
    db.set_setting("safe_restart", st)
    pr.event(None, "INFO", f"{actor or '管理员'} 发起安全重启：已暂停 {len(paused)} 个策略的新开仓，"
                           f"等待未完成订单结束（最长 {st['max_wait_min']:g} 分钟），然后备份数据库并重启")
    console.audit(actor, role, "安全重启", None, None, None, None, None, "已发起",
                  f"暂停 {len(paused)} 个策略；目标 {st['target'] or '--'}；最长等待 {st['max_wait_min']:g} 分钟")
    return st


def cancel(*, actor: str | None, role: str | None) -> None:
    from . import console
    from .paper import runner as pr
    _require_admin(role)
    st = state()
    if not st or st.get("phase") != "waiting":
        raise ValueError("没有可以取消的安全重启")
    _resume(st)
    db.set_setting("safe_restart", None)
    pr.event(None, "INFO", f"{actor or '管理员'} 取消了安全重启，已恢复新开仓")
    console.audit(actor, role, "取消安全重启", None, None, None, None, None, "已取消")


def _resume(st: dict) -> int:
    from .paper import runner as pr
    n = 0
    for did in st.get("paused") or []:
        dep = pr.get_deployment(did)
        if not dep:
            continue
        cfg = dep["config"]
        if not cfg.pop("restart_pause", None):
            continue
        status = "running" if dep["status"] == "paused_entries" and not cfg.get("auto_pause") \
            and not cfg.get("pending_offline") else dep["status"]
        db.ex("UPDATE paper_deployments SET config_json=?, status=?, updated_at=? WHERE id=?",
              (json.dumps(cfg, ensure_ascii=False), status, db.now_iso(), did))
        if status == "running":
            pr.event(did, "INFO", "安全重启完成：恢复新开仓")
        n += 1
    return n


def tick(now: datetime | None = None) -> str | None:
    """守护进程每次自检调用。可以重启时：备份数据库、把状态改为 restarting，返回 "restart"。"""
    st = state()
    if not st or st.get("phase") != "waiting":
        return None
    now = now or datetime.now(timezone.utc)
    n = open_orders()
    waited = (now - datetime.fromisoformat(st["at"])).total_seconds() / 60
    if n > 0 and waited < float(st["max_wait_min"]):
        st.update(open_orders=n, checked_at=db.now_iso(), waited_min=round(waited, 1))
        db.set_setting("safe_restart", st)
        return None
    note = "未完成订单已全部结束" if n == 0 else f"等待已超过 {st['max_wait_min']:g} 分钟，仍有 {n} 个未完成订单（重启后会自动对账）"
    try:
        from .maintenance import backup
        st["backup"] = str(backup())
    except Exception as e:  # noqa: BLE001
        st["backup_error"] = f"{type(e).__name__}: {e}"
    st.update(phase="restarting", note=note, restart_at=db.now_iso(), open_orders=n)
    db.set_setting("safe_restart", st)
    try:
        from .paper import runner as pr
        pr.event(None, "INFO", f"安全重启：{note}；数据库已备份，开始重启")
    except Exception:  # noqa: BLE001
        log.exception("restart event failed")
    return "restart"


def on_startup() -> str | None:
    """新进程启动时调用：完成安全重启的收尾（恢复策略、记录“已更新到 commit xxx”）。"""
    from . import build
    from .paper import runner as pr
    st = state()
    if not st or st.get("phase") != "restarting":
        return None
    n = _resume(st)
    run = build.running_build() or {}
    if run.get("commit") and run.get("short") != st.get("from"):
        msg = f"已更新到 commit {run['short']}（{run.get('subject') or ''}）"
    elif run.get("commit"):
        msg = f"安全重启完成，版本未变化（commit {run['short']}）"
    else:
        msg = "安全重启完成（无法读取代码版本）"
    pr.event(None, "INFO", f"{msg}；已恢复 {n} 个策略的新开仓" + (f"；备份：{st['backup']}" if st.get("backup") else ""))
    try:
        from . import console
        console.audit("系统", "", "安全重启完成", None, None, None, None, None, msg)
    except Exception:  # noqa: BLE001
        pass
    db.set_setting("safe_restart", None)
    db.set_setting("last_update", {"at": db.now_iso(), "message": msg, "commit": run.get("commit")})
    return msg
