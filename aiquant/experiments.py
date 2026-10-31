"""实验位：自动进化每产生一个新的挑战者，就把它作为一个独立的小额模拟盘部署真正跑起来（不替换现役版本）。

- 来源：challenger.register_btc 登记的新挑战者（训练集更好、历史留出数据已用完或给不出证据的候选）。
- 额度：每个实验位固定 pct%（默认 2%）× 账户权益，从现金层出（与试用期部署同一个额度池 allocation.trial_plan）；
  最多 max_slots 个（默认 5）。满了就不再开新的，等已有的实验结束。
- 结束：挑战者的结论由现有的挑战者机制决定（快速检验 / 前向测试，与现役版本比较）——
  胜出晋级 → 现役部署切换到这个版本，实验位完成使命，下线；淘汰 / 过期 / 被替换 → 实验位下线。
  下线走策略总台同一个 take_offline：空仓立即停止，有持仓先暂停新开仓、按规则卖完再停。
- 实验位本身不参与自动进化（避免实验位再生出实验位），不参与按实力分配；只在模拟盘下单锁开启时启动。
- 全部动作写审计（strategy_audit，策略 key = exp:<挑战者 id>）、事件流，并通知。
"""
from __future__ import annotations

import json
import logging

from . import db

log = logging.getLogger("aiquant.experiments")

DEFAULTS = {"enabled": True, "pct": 2.0, "max_slots": 5}
ACTOR = "系统（实验位）"


def get_config() -> dict:
    c = dict(DEFAULTS)
    c.update(db.get_setting("experiment_config", {}) or {})
    return c


def set_config(*, actor: str | None, role: str | None, enabled: bool | None = None, pct: float | None = None,
               max_slots: int | None = None) -> dict:
    from . import console
    console.require_role(role)
    cur = get_config()
    new = dict(cur)
    if enabled is not None:
        new["enabled"] = bool(enabled)
    if pct is not None:
        if not 0.5 <= float(pct) <= 10:
            raise ValueError("每个实验位的额度要在 0.5% ~ 10% 之间")
        new["pct"] = float(pct)
    if max_slots is not None:
        if not 0 <= int(max_slots) <= 10:
            raise ValueError("实验位数量要在 0 ~ 10 之间")
        new["max_slots"] = int(max_slots)
    db.set_setting("experiment_config", new)
    console.audit(actor, role, "修改实验位规则", "experiments", "实验位", None, None, None, "已保存",
                  f"{'开启' if new['enabled'] else '关闭'}；每个 {new['pct']:g}%；最多 {new['max_slots']} 个")
    return new


def is_experiment(dep: dict | None) -> bool:
    return bool(dep and (dep.get("config") or {}).get("experiment"))


def active() -> list[dict]:
    from .paper import runner as pr
    out = []
    for r in db.q("SELECT id FROM paper_deployments WHERE status IN ('running','paused_entries') "
                  "AND json_extract(config_json,'$.experiment') IS NOT NULL ORDER BY id"):
        out.append(pr.get_deployment(r["id"]))
    return out


def history(limit: int = 20) -> list[dict]:
    from .paper import runner as pr
    return [pr.get_deployment(r["id"]) for r in db.q(
        "SELECT id FROM paper_deployments WHERE json_extract(config_json,'$.experiment') IS NOT NULL "
        "ORDER BY id DESC LIMIT ?", (limit,))]


def _for_challenger(cid: int) -> dict | None:
    from .paper import runner as pr
    r = db.q1("SELECT id FROM paper_deployments WHERE json_extract(config_json,'$.experiment.challenger_id')=? "
              "ORDER BY id DESC LIMIT 1", (cid,))
    return pr.get_deployment(r["id"]) if r else None


def _notify(title: str, body: str, dedupe: str) -> None:
    try:
        from . import notify
        notify.event("experiment", title, body, dedupe=dedupe)
    except Exception:  # noqa: BLE001
        log.exception("experiment notify failed")


def launch(cid: int) -> dict:
    """把挑战者 #cid 开到一个实验位上。返回 {"result": 已启动/跳过, "why", "deployment_id"}。"""
    from . import allocation, challenger, console, evolve, research
    from .data import http
    from .data import market_data as md
    from .paper import runner as pr
    from .strategy import registry
    c = get_config()
    ch = db.q1("SELECT * FROM evolve_challengers WHERE id=?", (cid,))
    if not ch or ch["kind"] != "btc":
        return {"result": "跳过", "why": "不是 BTC 挑战者"}
    if ch["is_test"]:
        return {"result": "跳过", "why": "测试数据"}
    if not c["enabled"] or int(c["max_slots"]) <= 0:
        return {"result": "跳过", "why": "实验位已关闭"}
    if not http.paper_orders_unlocked():
        return {"result": "跳过", "why": "模拟盘下单锁未开启"}
    if _for_challenger(cid):
        return {"result": "跳过", "why": "这个挑战者已经在实验位上"}
    parent = pr.get_deployment(ch["deployment_id"])
    if not parent or parent["broker"] != "alpaca_paper" or is_experiment(parent):
        return {"result": "跳过", "why": "现役部署不是 BTC 模拟盘部署"}
    running = [d for d in active() if (db.q1("SELECT status FROM evolve_challengers WHERE id=?",
                                             (d["config"]["experiment"]["challenger_id"],)) or {}).get("status")
               in ("running", "awaiting")]   # 挑战者已有结论的实验位马上会下线，不占名额
    if len(running) >= int(c["max_slots"]):
        why = f"实验位已满（{len(running)}/{int(c['max_slots'])}），等已有的实验结束"
        challenger._event(parent["id"], "INFO", f"实验位：挑战者 #{cid} 没有开实验位 — {why}")
        return {"result": "跳过", "why": why}
    champ = registry.get_version(ch["champion_version_id"])
    cand = json.loads(ch["candidate_json"])
    nv = registry.freeze(champ["slot_no"], dict(cand, name=champ["schema"]["name"]), nl_text=champ["nl_text"],
                         source="experiment", change_summary=f"[实验位·挑战者#{cid}] {ch['change']}"[:500],
                         parent_id=champ["id"], set_current=False)
    rr = research.get_research()
    if research.latest_backtest_for_version(nv["id"], kind_split="训练集") is None:
        tr = evolve._windows()["train"]          # 部署门禁需要训练集回测：与进化同一个训练窗口
        ds = md.get_dataset(*tr, source=rr["source"])
        te = evolve.evaluate(md.load_dataset(ds), nv["schema"], tr[0], tr[1], rr)
        research.store_backtest(job_id=None, version=nv, slot_no=champ["slot_no"], ds=ds, start=tr[0], end=tr[1],
                                tf=te["tf"], aux=te["aux"], split="训练集", cfg=te["cfg"], res=te["res"])
    tf = int(cand.get("signal_tf") or parent["config"].get("signal_tf") or 15)
    plabel = parent["config"].get("label") or pr.dep_label(parent["id"])
    key = f"exp:{cid}"
    cfg = {"label": f"实验位·{champ['name']} #{cid}", "strategy_key": key, "signal_tf": tf,
           "aux_tf": cand.get("aux_tf"), "budget_usd": 0, "portfolio": True,
           "experiment": {"challenger_id": cid, "parent_deployment_id": parent["id"], "parent_label": plabel,
                          "champion_version_id": champ["id"], "change": ch["change"], "since": db.now_iso()}}
    cfg = allocation.start_trial(cfg, "crypto")
    cfg["trial"] = dict(cfg["trial"], pct=float(c["pct"]), experiment=True)
    db.set_setting(f"console_offline::{key}", None)
    did = pr.save_deployment(nv["id"], cfg, "alpaca_paper")
    pr.start(did)
    budget = _set_budget(did)
    msg = (f"挑战者 #{cid}（{ch['change']}）开到实验位，模拟盘真实下单；额度 {budget:,.0f} USD（{c['pct']:g}%，从现金层出）；"
           f"现役 {plabel} 不变。挑战者胜出晋级 / 淘汰后，实验位自动下线")
    pr.event(did, "INFO", "实验位：" + msg, False)
    challenger._event(parent["id"], "INFO", f"实验位：{msg}")
    console.audit(ACTOR, "system", "开实验位", key, cfg["label"], did, None, nv["id"], "已启动", msg)
    _notify(f"新实验位：{cfg['label']}", msg, f"exp-start:{cid}")
    return {"result": "已启动", "why": msg, "deployment_id": did}


def _set_budget(did: int) -> float:
    """按试用额度池立即给实验位定额度（之后每天的再平衡会再算一遍）。"""
    from . import allocation, evolve
    from .paper import runner as pr
    try:
        from .paper.brokers import AlpacaPaperAdapter
        a = AlpacaPaperAdapter().account()
        eq = float(a.equity) if a is not None else None
    except Exception:  # noqa: BLE001
        eq = None
    if not eq:
        return 0.0
    t = allocation.trial_plan(eq).get(did) or {}
    b = int(float(t.get("budget") or 0) // 100 * 100)
    dep = pr.get_deployment(did)
    cfg = dep["config"]
    cfg["budget_usd"] = b
    cfg["budget_reason"] = t.get("why") or "实验位"
    evolve._save_cfg(did, cfg)
    return float(b)


END_REASON = {"promoted": "挑战者胜出并晋级：现役部署已切换到这个版本，实验位完成",
              "rejected": "挑战者被淘汰（表现不如现役）", "expired": "挑战者过期（现役已换版本 / 长时间没有证据）",
              "replaced": "挑战者被训练分更高的新挑战者替换"}


def sync() -> list[str]:
    """服务循环调用：挑战者已有结论的实验位下线；现役部署已停止的实验位也下线。"""
    from . import console
    from .paper import runner as pr
    out = []
    for dep in active():
        cfg = dep["config"]
        if cfg.get("pending_offline"):
            continue
        ex = cfg["experiment"]
        ch = db.q1("SELECT status, reason FROM evolve_challengers WHERE id=?", (ex["challenger_id"],))
        status = (ch or {}).get("status") or "expired"
        parent = pr.get_deployment(ex.get("parent_deployment_id")) if ex.get("parent_deployment_id") else None
        why = None
        if status in END_REASON:
            why = END_REASON[status] + (f"：{ch['reason']}" if ch and ch.get("reason") else "")
        elif not parent or parent["status"] not in ("running", "paused_entries"):
            why = "现役部署已停止"
        if not why:
            continue
        key = cfg.get("strategy_key") or f"exp:{ex['challenger_id']}"
        res = console.take_offline(key, actor=ACTOR, role="admin")
        cfg = pr.get_deployment(dep["id"])["config"]
        cfg["experiment"] = dict(cfg["experiment"], ended={"at": db.now_iso(), "status": status, "why": why[:300]})
        from . import evolve
        evolve._save_cfg(dep["id"], cfg)
        pr.event(dep["id"], "INFO", f"实验位结束：{why[:300]}（{res['result']}）", False)
        _notify(f"实验位结束：{cfg.get('label')}", f"{why[:300]}\n{res['message']}", f"exp-end:{ex['challenger_id']}")
        out.append(f"#{dep['id']} {res['result']}")
    return out


def summary(dep: dict) -> dict:
    """策略总台卡片用：挑战者进度、现役对比、实验位本身的模拟盘盈亏。"""
    ex = (dep.get("config") or {}).get("experiment") or {}
    ch = db.q1("SELECT * FROM evolve_challengers WHERE id=?", (ex.get("challenger_id"),)) or {}
    ev = json.loads(ch["last_eval_json"]) if ch.get("last_eval_json") else {}
    rows = db.q("SELECT side, filled_qty, filled_price FROM paper_orders WHERE deployment_id=? AND status='filled' "
                "ORDER BY id", (dep["id"],))
    cash, qty = 0.0, 0.0
    for r in rows:
        v = float(r["filled_qty"] or 0) * float(r["filled_price"] or 0)
        cash += -v if r["side"] == "buy" else v
        qty += float(r["filled_qty"] or 0) * (1 if r["side"] == "buy" else -1)
    return {"challenger_id": ex.get("challenger_id"), "parent_label": ex.get("parent_label"), "change": ex.get("change"),
            "since": ex.get("since"), "ch_status": ch.get("status"), "days": ev.get("days"), "need_days": ev.get("need_days"),
            "challenger": ev.get("challenger"), "champion": ev.get("champion"), "fills": len(rows),
            "realized_cash": cash, "qty": max(qty, 0.0), "ended": ex.get("ended")}
