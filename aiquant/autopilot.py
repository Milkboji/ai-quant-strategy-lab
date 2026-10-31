"""自动驾驶（2026-10-31 用户决定）：原来要管理员人工确认的事，全部交给系统按既定规则自动执行，写审计并通知。

服务循环每小时调用一次 run()：
1. 待确认队列：有挑战者 / 进化结果在等确认 → 自动批准（走 console.approve，同一条晋级路径，空仓才切换）。
2. 美股交易节奏：影子模式满 20 个完整交易日且没有异常 → 自动把连亏刹车切为生效（rhythm.set_us_mode，前提检查不变）。
3. 点差模型：每个品种都采样满 10 天 → 自动生成新旧口径对比报告并切换到实测点差（spread_model.build_report / enable）。

一次性设置（upgrades.autopilot_1031）：关闭「晋级需人工确认」、手动部署后不再锁定、清除已有锁定，
自动进化改为每天一轮，加密货币上限 51%（现金层腾出空间，5 个实验位都有额度）。
"""
from __future__ import annotations

import logging

from . import db

log = logging.getLogger("aiquant.autopilot")
ACTOR = "系统（自动驾驶）"


def _note(title: str, body: str, dedupe: str) -> None:
    try:
        from . import notify
        notify.event("promotion", title, body, dedupe=dedupe)
    except Exception:  # noqa: BLE001
        log.exception("autopilot notify failed")


def approve_queue() -> list[str]:
    from . import console
    out = []
    for a in console.awaiting():
        try:
            res = console.approve(a["id"], actor=ACTOR, role="admin")
            out.append(f"#{a['id']} {res.get('result') or res.get('message') or '已批准'}")
        except Exception as e:  # noqa: BLE001
            log.exception("auto approve failed")
            out.append(f"#{a['id']} 未能批准：{e}")
    return out


def us_rhythm() -> str | None:
    from . import rhythm
    ps = rhythm.us_promotion_status()
    if not ps.get("shadow") or not ps.get("ready"):
        return None
    res = rhythm.set_us_mode("active", actor=ACTOR, role="admin")
    _note("美股连亏刹车已自动转为生效", f"{ps['text']}\n{res.get('message', '')}", "autopilot-us-rhythm")
    return res.get("message")


def spread() -> str | None:
    from . import spread_model
    if spread_model.current() is not None:
        return None
    p = spread_model.proposal()
    if p["missing"] or not p["profiles"]:
        return None
    rep = spread_model.build_report(ACTOR)
    rid = rep.get("id") or (spread_model.latest_report() or {}).get("id")
    if not rid:
        return None
    msg = spread_model.enable(rid, actor=ACTOR, role="admin")
    _note("点差口径已自动切换为实测点差", f"{msg}\n对比报告 #{rid} 在「策略成绩单」页。", f"autopilot-spread-{rid}")
    return msg


_running = False


def maybe_run() -> bool:
    """服务循环每轮调用：每小时最多一次，放在单独线程里（点差报告要跑回测，不拖慢交易循环）。"""
    global _running
    from datetime import datetime, timezone
    hr = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
    if _running or db.get_setting("autopilot_hour") == hr:
        return False
    db.set_setting("autopilot_hour", hr)
    _running = True

    def work():
        global _running
        try:
            run()
        finally:
            _running = False
    import threading
    threading.Thread(target=work, daemon=True, name="aiquant-autopilot").start()
    return True


def run() -> list[str]:
    out = []
    for name, fn in (("待确认", approve_queue), ("美股节奏", us_rhythm), ("点差", spread)):
        try:
            r = fn()
            if r:
                out.append(f"{name}：{r}")
        except Exception:  # noqa: BLE001
            log.exception("autopilot %s failed", name)
    if out:
        log.info("autopilot: %s", "；".join(out))
    return out


def setup_once() -> dict:
    """一次性把人工确认相关的设置改为全自动（升级 autopilot_1031 调用），每项写审计。"""
    from . import allocation, console, evolve
    out = {}
    console.set_promotion_config(actor=ACTOR, role="admin", require_confirm=False, manual_lock_days=0)
    out["promotion"] = "晋级需人工确认：关；手动部署后锁定 0 天"
    n = 0
    for d in db.q("SELECT id FROM paper_deployments WHERE status IN ('running','paused_entries')"):
        from .paper import runner as pr
        dep = pr.get_deployment(d["id"])
        if console.lock_state(dep):
            console._set_lock(dep, None)
            console.audit(ACTOR, "admin", "解除锁定", console._dep_key(dep), dep["config"].get("label"), dep["id"],
                          dep["version_id"], None, "已解除", "全自动：不再锁定版本")
            n += 1
    from .paper import etf_versions as ev
    if ev.lock_state():
        ev.set_lock(False, actor=ACTOR, role="admin")
        n += 1
    out["locks_cleared"] = n
    evolve.set_config(interval_days=1)
    out["evolve"] = "自动进化每天一轮"
    if float(allocation.get_config()["crypto"]["max"]) > 51.0:
        allocation.set_config(actor=ACTOR, role="admin", crypto_max=51.0)
        out["crypto_max"] = "→ 51%（现金层腾出空间：我的交易系统 5% + 5 个实验位 × 2%）"
    return out
