"""策略总台：每个策略（5 个策略位 + 3 个实时预设）一行。

- 展示：来源、状态、实验室最新版本 vs 模拟盘运行版本（不一致时给出规则差异）、挑战者与前向进度、持仓、资金权重。
- 操作：精确到单个策略、单个版本的「部署此版本 / 下线」。版本切换沿用自动进化的规则（evolve.schedule_switch /
  apply_pending）：只在该策略空仓且没有未完成订单时生效，否则等待空仓。下线同理：有持仓时先暂停新开仓、
  持仓照常按规则卖出，空仓后才停止。
- 默认行为：实验室手动保存新版本不会替换运行中的版本，必须在这里确认部署。
- 权限：只有管理员 / 交易员可以部署、下线（后端检查，不只靠界面禁用按钮）。每次操作写入 strategy_audit。

不涉及任何真实资金路径：部署只创建 / 切换 Alpaca Paper（或测试用 Mock）部署，下单仍受 PAPER 双重锁约束，
LIVE_ORDERS = 0 不变。
"""
from __future__ import annotations

import json
import logging

from . import db

log = logging.getLogger("aiquant.console")

EDIT_ROLES = ("admin", "trader")
ACTIVE = ("configured", "running", "paused_entries")
OPEN_ORDER_STATES = ("new", "submitted", "partially_filled", "unknown_after_restart")
STATUSES = ("草稿", "已回测", "已验证", "已部署", "运行中", "暂停中", "已下线")
SOURCE_CN = {"manual": "手写", "deepseek": "DeepSeek 优化", "deepseek_parse": "DeepSeek 解析", "copy": "复制",
             "auto_evolve": "自动进化", "preset": "实时预设", "test": "测试"}
TRIGGER_CN = {"manual": "手动部署", "evolve": "自动进化", "challenger": "挑战者晋级"}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS strategy_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, actor TEXT, role TEXT, action TEXT NOT NULL,
  strategy_key TEXT, strategy_name TEXT, deployment_id INTEGER, from_version_id INTEGER, to_version_id INTEGER,
  result TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS ix_strategy_audit_key ON strategy_audit(strategy_key);
"""


class ConsolePermissionError(PermissionError):
    pass


def ensure_tables() -> None:
    with db.tx() as c:
        c.executescript(SCHEMA_SQL)


def require_role(role: str | None) -> None:
    if role not in EDIT_ROLES:
        raise ConsolePermissionError("只有管理员或交易员可以部署 / 下线策略（观察员只读）")


def audit(actor: str | None, role: str | None, action: str, key: str | None = None, name: str | None = None,
          did: int | None = None, from_vid: int | None = None, to_vid: int | None = None,
          result: str = "", detail: str = "") -> None:
    ensure_tables()
    db.ex("INSERT INTO strategy_audit(ts,actor,role,action,strategy_key,strategy_name,deployment_id,from_version_id,"
          "to_version_id,result,detail) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
          (db.now_iso(), (actor or "系统")[:32], role or "", action[:40], key, (name or "")[:80], did, from_vid,
           to_vid, (result or "")[:200], (detail or "")[:500]))


def audit_log(limit: int = 100, key: str | None = None) -> list[dict]:
    ensure_tables()
    if key:
        return db.q("SELECT * FROM strategy_audit WHERE strategy_key=? ORDER BY id DESC LIMIT ?", (key, limit))
    return db.q("SELECT * FROM strategy_audit ORDER BY id DESC LIMIT ?", (limit,))


# ---------------------------------------------------------------------------
# 策略识别：版本 → 策略（策略位 N / 实时预设）
# ---------------------------------------------------------------------------

def key_of_version(v: dict | None) -> str | None:
    if not v:
        return None
    if v.get("slot_no"):
        return f"slot:{int(v['slot_no'])}"
    if str(v.get("name") or "").startswith("实时-"):
        return f"preset:{v['name']}"
    return None


def versions_of(key: str) -> list[dict]:
    """该策略的全部已冻结版本（新的在前）。"""
    from .strategy import registry
    if key.startswith("slot:"):
        rows = db.q("SELECT id FROM strategy_versions WHERE slot_no=? ORDER BY id DESC", (int(key[5:]),))
    else:
        rows = db.q("SELECT id FROM strategy_versions WHERE slot_no IS NULL AND name=? ORDER BY id DESC",
                    (key[len("preset:"):],))
    return [registry.get_version(r["id"]) for r in rows]


def _deployments(broker: str) -> list[dict]:
    from .paper import runner as pr
    out = []
    for d in db.q("SELECT id FROM paper_deployments WHERE broker=? ORDER BY id", (broker,)):
        out.append(pr.get_deployment(d["id"]))
    return out


def _dep_key(dep: dict) -> str | None:
    from .strategy import registry
    return dep["config"].get("strategy_key") or key_of_version(registry.get_version(dep["version_id"]))


def active_deployment(key: str, broker: str = "alpaca_paper") -> dict | None:
    """该策略当前有效的部署（已配置 / 运行中 / 暂停新开仓），同一策略只取最新一个。"""
    hit = None
    for d in _deployments(broker):
        if d["status"] in ACTIVE and _dep_key(d) == key:
            hit = d
    return hit


def is_offline(key: str) -> bool:
    return bool(db.get_setting(f"console_offline::{key}"))


def _ledger_qty(did: int) -> float:
    q = 0.0
    for r in db.q("SELECT side, filled_qty FROM paper_orders WHERE deployment_id=? AND status='filled'", (did,)):
        q += r["filled_qty"] if r["side"] == "buy" else -r["filled_qty"]
    return max(q, 0.0)


def _has_open_orders(did: int) -> bool:
    marks = ",".join("?" * len(OPEN_ORDER_STATES))
    return db.q1(f"SELECT id FROM paper_orders WHERE deployment_id=? AND status IN ({marks})",
                 (did, *OPEN_ORDER_STATES)) is not None


def is_flat(did: int) -> bool:
    return _ledger_qty(did) <= 1e-9 and not _has_open_orders(did)


def _save_cfg(did: int, cfg: dict) -> None:
    db.ex("UPDATE paper_deployments SET config_json=?, updated_at=? WHERE id=?",
          (json.dumps(cfg, ensure_ascii=False), db.now_iso(), did))


# ---------------------------------------------------------------------------
# 总台数据
# ---------------------------------------------------------------------------

def _base_strategies() -> list[dict]:
    from .live import sim
    from .paper import portfolio
    from .strategy import registry
    out = []
    for slot in registry.all_slots():
        n = slot["slot_no"]
        v = registry.get_version(slot["current_version_id"]) if slot["current_version_id"] else None
        draft = (slot.get("draft_text") or "").strip()
        if not v and not draft:
            continue
        out.append({"key": f"slot:{n}", "kind": "slot", "origin": f"策略位 {n}", "lab": v,
                    "name": v["name"] if v else f"策略位{n} 草稿", "draft": draft})
    for pk in sim.PRESETS:
        try:
            v = portfolio.preset_version(pk)
        except Exception:  # noqa: BLE001
            log.exception("preset version failed")
            continue
        out.append({"key": f"preset:{v['name']}", "kind": "preset", "origin": "实时预设", "lab": v,
                    "name": v["name"], "draft": ""})
    return out


def _challenger_for(did: int | None) -> dict | None:
    if not did:
        return None
    from . import challenger
    challenger.ensure_tables()
    r = db.q1("SELECT * FROM evolve_challengers WHERE kind='btc' AND deployment_id=? AND status='running' "
              "ORDER BY id DESC LIMIT 1", (did,))
    if not r:
        return None
    ev = json.loads(r["last_eval_json"]) if r.get("last_eval_json") else {}
    tr = json.loads(r["train_json"]) if r.get("train_json") else {}
    return {"id": r["id"], "change": r["change"], "registered_at": r["registered_at"],
            "days": ev.get("days"), "need_days": ev.get("need_days"),
            "fast": ((tr or {}).get("fast") or {}).get("verdict"),
            "challenger": ev.get("challenger"), "champion": ev.get("champion")}


def _awaiting_for(did: int | None) -> dict | None:
    if not did:
        return None
    from . import challenger
    challenger.ensure_tables()
    r = db.q1("SELECT id, change, reason, decided_at FROM evolve_challengers WHERE kind='btc' AND deployment_id=? "
              "AND status='awaiting' ORDER BY id DESC LIMIT 1", (did,))
    return dict(r) if r else None


def _status(lab: dict | None, dep: dict | None, offline: bool, had_dep: bool, include_test: bool) -> str:
    from . import research
    if dep:
        return {"running": "运行中", "paused_entries": "暂停中"}.get(dep["status"], "已部署")
    if offline or had_dep:
        return "已下线"
    if not lab:
        return "草稿"
    if db.q1("SELECT id FROM holdout_touches WHERE version_id=? AND split='validation'", (lab["id"],)):
        return "已验证"
    if research.latest_backtest_for_version(lab["id"], include_test=include_test):
        return "已回测"
    return "草稿"


def rows(broker: str = "alpaca_paper", price: float | None = None) -> list[dict]:
    """策略总台每一行的数据。"""
    from .strategy import registry
    include_test = broker == "mock"
    deps = _deployments(broker)
    active = {}
    had = set()
    for d in deps:
        k = _dep_key(d)
        if not k:
            continue
        had.add(k)
        if d["status"] in ACTIVE:
            active[k] = d          # 同一策略取最新
    plans = plans_for(broker)
    vol = vol_sizing()
    budgets = {k: float(d["config"].get("budget_usd") or 0) for k, d in active.items()
               if d["status"] in ("running", "paused_entries")}
    total_budget = sum(budgets.values())
    out = []
    for s in _base_strategies():
        k, lab = s["key"], s["lab"]
        dep = active.get(k)
        run = registry.get_version(dep["version_id"]) if dep else None
        mismatch = bool(dep and lab and run and lab["id"] != run["id"])
        diff = registry.diff(run["schema"], lab["schema"]) if mismatch else []
        if mismatch and dep:
            run_tf = dep["config"].get("signal_tf")
            lab_tf = lab["schema"].get("signal_tf")
            if run_tf and lab_tf and int(run_tf) != int(lab_tf) and not any(x[0] == "signal_tf" for x in diff):
                diff.append(("K线周期（分钟）", str(run_tf), str(lab_tf)))
        pend = (dep or {}).get("config", {}).get("pending_switch") if dep else None
        pend_v = registry.get_version(pend["version_id"]) if pend else None
        qty = _ledger_qty(dep["id"]) if dep else 0.0
        b = budgets.get(k, 0.0)
        offline = is_offline(k)
        out.append({
            "key": k, "kind": s["kind"], "origin": s["origin"], "name": s["name"], "draft": s["draft"],
            "source": (lab or {}).get("source"), "source_cn": SOURCE_CN.get((lab or {}).get("source"), (lab or {}).get("source") or "--"),
            "status": _status(lab, dep, offline, k in had, include_test),
            "lab": lab, "run": run, "deployment": dep, "mismatch": mismatch,
            "lab_newer": bool(mismatch and lab["id"] > run["id"]), "diff": diff,
            "pending": ({"version": pend_v, "reason": pend.get("reason"), "trigger": pend.get("trigger", "evolve"),
                         "at": pend.get("at")} if pend else None),
            "pending_offline": bool(dep and dep["config"].get("pending_offline")),
            "offline": offline,
            "challenger": _challenger_for(dep["id"] if dep else None),
            "awaiting": _awaiting_for(dep["id"] if dep else None),
            "lock": lock_state(dep), "block": promotion_block(dep) if dep else None,
            "qty": qty, "value": qty * price if price else None,
            "budget": b if dep else 0.0, "weight_pct": (b / total_budget * 100) if dep and total_budget else None,
            "budget_reason": (dep or {}).get("config", {}).get("budget_reason") if dep else None,
            "signal_tf": (dep or {}).get("config", {}).get("signal_tf") if dep else None,
            "why": explain(dep, plans.get(dep["id"]) if dep else None, price, vol) if dep else None,
        })
    return out


# ---------------------------------------------------------------------------
# 部署 / 下线
# ---------------------------------------------------------------------------

def _tf_for(v: dict, broker: str) -> tuple[int, int | None]:
    from . import research
    inc = broker == "mock"
    b = (research.latest_backtest_for_version(v["id"], include_test=inc, kind_split="训练集")
         or research.latest_backtest_for_version(v["id"], include_test=inc))
    if b:
        return int(b["signal_tf"]), b["aux_tf"]
    return int(v["schema"].get("signal_tf") or 15), v["schema"].get("aux_tf")


def _label(key: str, v: dict, tf: int) -> str:
    tf_cn = {15: "15分钟", 30: "30分钟", 60: "1小时", 240: "4小时", 1440: "日线"}.get(tf, f"{tf}分钟")
    if key.startswith("slot:"):
        return f"策略位{key[5:]}·{v['name']} v{v['version_no']}"
    return f"{v['name']} v{v['version_no']}（{tf_cn}）"


def _initial_budget(broker: str) -> float | None:
    """新部署的起始额度：先拿一小份可用额度启动，随后由 evolve.rebalance 按实力重新分配。"""
    if broker != "alpaca_paper":
        return None
    from .paper import portfolio
    acct = portfolio.AlpacaPaperAdapter().account()
    if acct is None:
        raise RuntimeError("无法读取 Alpaca Paper 账户，暂不能部署")
    used = sum(float(d["config"].get("budget_usd") or 0) for d in _deployments(broker)
               if d["status"] in ("running", "paused_entries"))
    free = acct.equity * 0.96 - used
    if free < 100:
        raise ValueError("账户里没有未分配的资金额度：请先下线其他策略，或等待下一次资金再分配")
    return float(int(min(free, acct.equity * 0.10) // 100 * 100))


def deploy_version(key: str, version_id: int, *, actor: str | None, role: str | None,
                   broker: str = "alpaca_paper") -> dict:
    """把某个策略部署为指定版本。返回 {"result": 已启动/已切换/等待空仓后切换/无变化, "deployment_id", "message"}。"""
    from . import evolve
    from .paper import runner as pr
    from .strategy import registry
    require_role(role)
    v = registry.get_version(int(version_id))
    if not v or not v["frozen"]:
        raise ValueError("版本不存在或未冻结")
    if key_of_version(v) != key:
        raise ValueError(f"版本 {registry.label(v)} 不属于这个策略")
    from . import research
    if research.version_market(v).key != "crypto":
        raise ValueError("这是美股日线策略版本：模拟盘的美股目前只运行「美股 ETF 择时」（在本页下方的美股卡片部署参数版本），"
                         "策略位里的美股版本暂时只能回测研究，不能部署到 BTC 模拟盘")
    ok, checks = pr.deploy_gate(v["id"])
    if broker == "alpaca_paper" and not ok:
        raise ValueError("部署门禁未通过：" + "；".join(f"{n}（{d}）" for n, p, d in checks if not p))
    was_offline = is_offline(key)
    db.set_setting(f"console_offline::{key}", None)
    tf, aux = _tf_for(v, broker)
    dep = active_deployment(key, broker)
    if dep:
        cfg = dep["config"]
        old_vid = dep["version_id"]
        notes = []
        if cfg.pop("pending_offline", None):
            notes.append("取消待下线")
            _save_cfg(dep["id"], cfg)
            if dep["status"] == "paused_entries" and not cfg.get("auto_pause"):
                pr.set_status(dep["id"], "running")
                pr.event(dep["id"], "INFO", "策略总台：取消下线，恢复运行", bool(dep["is_test"]))
        if old_vid == v["id"]:
            if cfg.pop("pending_switch", None):
                notes.append("取消待切换")
                _save_cfg(dep["id"], cfg)
                pr.event(dep["id"], "INFO", "策略总台：保持当前版本，取消待切换", bool(dep["is_test"]))
            res = {"result": "无变化", "deployment_id": dep["id"],
                   "message": f"{registry.label(v)} 已在运行" + (f"（{'、'.join(notes)}）" if notes else "")}
        else:
            evolve.schedule_switch(dep["id"], v["id"], tf, aux, f"{actor or '用户'} 在策略总台确认部署",
                                   trigger="manual", actor=actor)
            now = pr.get_deployment(dep["id"])
            switched = now["version_id"] == v["id"]
            res = {"result": "已切换" if switched else "等待空仓后切换", "deployment_id": dep["id"],
                   "message": (f"已切换到 {registry.label(v)}" if switched
                               else f"{registry.label(v)} 将在该策略空仓且没有未完成订单时切换")}
        if old_vid != v["id"]:
            n = _expire_awaiting(dep["id"], "用户手动部署了其他版本，待确认的挑战者作废")
            if n:
                notes.append(f"作废 {n} 个待确认挑战者")
        lk = lock_after_manual_deploy(dep["id"], actor)
        if lk:
            notes.append(f"锁定 {lk['days']:g} 天")
            res["message"] += f"；已锁定 {lk['days']:g} 天，期间自动进化不会替换"
        audit(actor, role, "部署此版本", key, v["name"], dep["id"], old_vid, v["id"], res["result"],
              "；".join(notes))
        return res
    cfg = {"label": _label(key, v, tf), "strategy_key": key, "signal_tf": tf, "aux_tf": aux,
           "alloc_pct": 100, "portfolio": True, "stop_mode": "keep", "deployed_by": actor or "用户"}
    budget = _initial_budget(broker)
    if budget is not None:
        cfg["budget_usd"] = budget
    if broker == "mock":
        from . import research
        cfg["fee_bps"] = research.get_research()["fee_bps"]
    did = pr.save_deployment(v["id"], cfg, broker)
    try:
        pr.start(did)
    except Exception as e:  # noqa: BLE001
        audit(actor, role, "部署此版本", key, v["name"], did, None, v["id"], "启动失败", str(e))
        raise
    if broker == "alpaca_paper":
        try:
            evolve.rebalance()
        except Exception:  # noqa: BLE001
            log.exception("rebalance after deploy failed")
    res = {"result": "已启动", "deployment_id": did,
           "message": f"已部署并启动 {registry.label(v)}（部署 #{did}）" + ("，已重新加入组合" if was_offline else "")}
    lk = lock_after_manual_deploy(did, actor)
    if lk:
        res["message"] += f"；已锁定 {lk['days']:g} 天，期间自动进化不会替换"
    audit(actor, role, "部署此版本", key, v["name"], did, None, v["id"], res["result"],
          "；".join(x for x in ("重新上线" if was_offline else "", f"锁定 {lk['days']:g} 天" if lk else "") if x))
    return res


def take_offline(key: str, *, actor: str | None, role: str | None, broker: str = "alpaca_paper") -> dict:
    """下线某个策略：空仓 → 立即停止；有持仓 → 暂停新开仓，持仓照常按规则卖出，空仓后自动停止。
    下线的策略不会被「一键部署全部」重新部署，直到在总台再次部署。"""
    from . import challenger
    from .paper import runner as pr
    from .strategy import registry
    require_role(role)
    dep = active_deployment(key, broker)
    db.set_setting(f"console_offline::{key}", {"at": db.now_iso(), "by": actor or "用户"})
    if not dep:
        audit(actor, role, "下线", key, key.split(":", 1)[1], None, None, None, "已下线", "没有运行中的部署，仅移出组合")
        return {"result": "已下线", "deployment_id": None, "message": "已移出组合（当前没有运行中的部署）"}
    cfg = dep["config"]
    notes = []
    if cfg.pop("pending_switch", None):
        notes.append("取消待切换")
    try:
        challenger.retire_for_deployment(dep["id"], "策略已在策略总台下线")
    except Exception:  # noqa: BLE001
        log.exception("retire challenger failed")
    v = registry.get_version(dep["version_id"])
    if is_flat(dep["id"]):
        _save_cfg(dep["id"], cfg)
        pr.stop(dep["id"])
        pr.event(dep["id"], "INFO", f"策略总台：{actor or '用户'} 下线该策略（空仓，立即停止）", bool(dep["is_test"]))
        res = {"result": "已下线", "deployment_id": dep["id"], "message": f"{registry.label(v)} 已下线"}
    else:
        cfg["pending_offline"] = {"at": db.now_iso(), "by": actor or "用户"}
        _save_cfg(dep["id"], cfg)
        if dep["status"] == "running":
            pr.set_status(dep["id"], "paused_entries")
        pr.event(dep["id"], "INFO", f"策略总台：{actor or '用户'} 下线该策略，有持仓，先暂停新开仓，空仓后停止",
                 bool(dep["is_test"]))
        res = {"result": "等待空仓后下线", "deployment_id": dep["id"],
               "message": f"{registry.label(v)} 已暂停新开仓，持仓按规则卖出后自动下线"}
    if broker == "alpaca_paper":
        try:
            from . import evolve
            evolve.rebalance()
        except Exception:  # noqa: BLE001
            log.exception("rebalance after offline failed")
    audit(actor, role, "下线", key, v["name"] if v else None, dep["id"], dep["version_id"], None, res["result"],
          "；".join(notes))
    return res


def apply_pending_offline() -> int:
    """服务循环每轮调用：待下线的策略空仓后停止。返回本轮停止的数量。"""
    from .paper import runner as pr
    n = 0
    for d in db.q("SELECT id FROM paper_deployments WHERE status IN ('running','paused_entries')"):
        dep = pr.get_deployment(d["id"])
        cfg = dep["config"]
        if not cfg.get("pending_offline") or not is_flat(dep["id"]):
            continue
        by = cfg["pending_offline"].get("by")
        cfg.pop("pending_offline", None)
        _save_cfg(dep["id"], cfg)
        pr.stop(dep["id"])
        pr.event(dep["id"], "INFO", "策略总台：已空仓，下线生效", bool(dep["is_test"]))
        audit("系统", "", "下线生效", _dep_key(dep), cfg.get("label"), dep["id"], dep["version_id"], None,
              "已下线", f"由 {by} 发起")
        n += 1
    return n


# ---------------------------------------------------------------------------
# 晋级规则：锁定版本 / 晋级需人工确认 / 待确认队列
#
# 优先级（从高到低）：
#   1. 用户的手动操作（部署此版本、批准 / 拒绝）随时有效；
#   2. 手动部署后该策略自动锁定 N 天（promotion_config.manual_lock_days，默认 7，0 = 不锁）；
#      用户也可以手动打开 / 关闭「锁定版本」（手动锁定不设到期时间）；
#   3. 锁定中，或开启了全局「晋级需人工确认」：进化照常生成候选、跑挑战者前向测试，
#      但胜出者不自动晋级，只进入待确认队列（evolve_challengers.status = 'awaiting'）；
#   4. 否则维持原有自动晋级。任何切换都只在该策略空仓且没有未完成订单时生效。
# ---------------------------------------------------------------------------

PROMO_DEFAULTS = {"require_confirm": False, "manual_lock_days": 7}


def promotion_config() -> dict:
    c = dict(PROMO_DEFAULTS)
    saved = db.get_setting("promotion_config") or {}
    if "require_confirm" not in saved:   # 兼容旧设置：进化页「通过检验后自动替换」关闭 = 需要人工确认
        from . import evolve
        c["require_confirm"] = not bool(evolve.get_config().get("auto_apply", True))
    c.update(saved)
    return c


def set_promotion_config(*, actor: str | None, role: str | None, require_confirm: bool | None = None,
                         manual_lock_days: float | None = None) -> dict:
    from . import evolve
    require_role(role)
    c = promotion_config()
    before = dict(c)
    if require_confirm is not None:
        c["require_confirm"] = bool(require_confirm)
        evolve.set_config(auto_apply=not bool(require_confirm))   # 保持旧开关一致
    if manual_lock_days is not None:
        if not 0 <= float(manual_lock_days) <= 365:
            raise ValueError("锁定天数需在 0~365 之间")
        c["manual_lock_days"] = float(manual_lock_days)
    db.set_setting("promotion_config", c)
    if c != before:
        audit(actor, role, "修改晋级规则", None, None, None, None, None, "已保存",
              f"晋级需人工确认：{'开' if c['require_confirm'] else '关'}；手动部署后锁定 {c['manual_lock_days']:g} 天")
    return c


def lock_state(dep: dict | None) -> dict | None:
    """有效的版本锁：{"until": iso|None, "kind": manual_deploy|manual, "by", "at"}；过期或没有时返回 None。"""
    if not dep:
        return None
    lk = (dep.get("config") or {}).get("version_lock")
    if not lk:
        return None
    if lk.get("until"):
        import pandas as pd
        if pd.Timestamp(lk["until"]) <= pd.Timestamp.now(tz="UTC"):
            return None
    return lk


def promotion_block(dep: dict | None) -> str | None:
    """这个部署的挑战者 / 进化结果现在能否自动晋级：能 → None；不能 → 原因（进入待确认队列）。"""
    if promotion_config().get("require_confirm"):
        return "已开启「晋级需人工确认」"
    lk = lock_state(dep)
    if lk:
        if lk.get("until"):
            import pandas as pd
            bj = (pd.Timestamp(lk["until"]) + pd.Timedelta(hours=8)).strftime("%m-%d %H:%M")
            return f"版本已锁定至 {bj}（北京时间）"
        return "版本已手动锁定"
    return None


def _set_lock(dep: dict, lock: dict | None) -> None:
    from .paper import runner as pr
    d = pr.get_deployment(dep["id"])
    cfg = d["config"]
    if lock:
        cfg["version_lock"] = lock
    else:
        cfg.pop("version_lock", None)
    _save_cfg(d["id"], cfg)


def set_lock(key: str, on: bool, *, actor: str | None, role: str | None, broker: str = "alpaca_paper") -> dict:
    """手动打开 / 关闭「锁定版本」。打开 = 不设到期时间；关闭 = 清除任何锁（包括手动部署后的自动锁定）。"""
    from .paper import runner as pr
    require_role(role)
    dep = active_deployment(key, broker)
    if not dep:
        raise ValueError("该策略没有运行中的部署，无法锁定")
    if on:
        _set_lock(dep, {"until": None, "kind": "manual", "by": actor or "用户", "at": db.now_iso()})
        pr.event(dep["id"], "INFO", f"策略总台：{actor or '用户'} 锁定版本，自动进化不会自动替换", bool(dep["is_test"]))
        audit(actor, role, "锁定版本", key, dep["config"].get("label"), dep["id"], dep["version_id"], None, "已锁定")
        return {"result": "已锁定", "message": "已锁定：挑战者胜出后会进入待确认队列"}
    _set_lock(dep, None)
    pr.event(dep["id"], "INFO", f"策略总台：{actor or '用户'} 解除版本锁定", bool(dep["is_test"]))
    audit(actor, role, "解除锁定", key, dep["config"].get("label"), dep["id"], dep["version_id"], None, "已解除")
    return {"result": "已解除", "message": "已解除锁定：恢复自动晋级规则"}


def lock_after_manual_deploy(did: int, actor: str | None) -> dict | None:
    days = float(promotion_config().get("manual_lock_days") or 0)
    if days <= 0:
        return None
    from datetime import datetime, timedelta, timezone
    until = (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()
    lock = {"until": until, "kind": "manual_deploy", "by": actor or "用户", "at": db.now_iso(), "days": days}
    _set_lock({"id": did}, lock)
    return lock


def awaiting(limit: int = 50) -> list[dict]:
    """待确认队列（BTC 挑战者 / 进化结果 + 美股 ETF 参数）。"""
    from . import challenger
    challenger.ensure_tables()
    rows = db.q("SELECT * FROM evolve_challengers WHERE status='awaiting' ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        for k in ("candidate_json", "train_json", "last_eval_json", "champion_params"):
            r[k[:-5] if k.endswith("_json") else k] = json.loads(r[k]) if r.get(k) else None
    return rows


def _expire_awaiting(did: int, reason: str) -> int:
    from . import challenger
    challenger.ensure_tables()
    rows = db.q("SELECT id FROM evolve_challengers WHERE kind='btc' AND deployment_id=? AND status='awaiting'", (did,))
    for r in rows:
        db.ex("UPDATE evolve_challengers SET status='expired', decided_at=?, reason=? WHERE id=?",
              (db.now_iso(), reason[:480], r["id"]))
    return len(rows)


def approve(ch_id: int, *, actor: str | None, role: str | None) -> dict:
    """批准待确认的晋级：BTC → 冻结新版本并登记切换（空仓才生效）；ETF → 改用新参数。"""
    from . import challenger, research
    from .paper import etf_trend
    from .paper import runner as pr
    from .strategy import registry
    require_role(role)
    ch = db.q1("SELECT * FROM evolve_challengers WHERE id=?", (int(ch_id),))
    if not ch or ch["status"] != "awaiting":
        raise ValueError("这条记录不在待确认队列中")
    if ch["kind"] == "etf":
        champ, cand = json.loads(ch["champion_params"]), json.loads(ch["candidate_json"])
        cfg = etf_trend.get_config()
        if int(cfg["sma"]) != champ["sma"] or abs(float(cfg["band"]) - champ["band"]) > 1e-9:
            challenger._close(ch, "expired", "现役参数已变化，待确认的挑战者作废")
            audit(actor, role, "批准晋级", "etf", "美股 ETF 择时", None, None, None, "已作废", "现役参数已变化")
            raise ValueError("现役参数已变化，这个挑战者已作废")
        from .paper import etf_versions
        etf_versions.activate(int(cand["sma"]), float(cand["band"]), trigger="approve", actor=actor, role=role,
                              note=f"挑战者 #{ch['id']} 经批准晋级")
        challenger._close(ch, "promoted", f"{actor or '用户'} 批准晋级：{ch['reason'] or ''}", trigger="人工批准", actor=actor)
        return {"result": "已晋级", "message": f"美股 ETF 参数已改为 {cand['sma']} 日均线 ±{cand['band']:.0%}"}
    dep = pr.get_deployment(ch["deployment_id"])
    key = _dep_key(dep) if dep else None
    if not dep or dep["status"] not in ("running", "paused_entries"):
        challenger._close(ch, "expired", "部署已停止，待确认的挑战者作废")
        audit(actor, role, "批准晋级", key, ch["label"], ch["deployment_id"], None, None, "已作废", "部署已停止")
        raise ValueError("该策略已停止，这个挑战者已作废")
    if dep["version_id"] != ch["champion_version_id"]:
        challenger._close(ch, "expired", "现役版本已变化，待确认的挑战者作废")
        audit(actor, role, "批准晋级", key, ch["label"], dep["id"], dep["version_id"], None, "已作废", "现役版本已变化")
        raise ValueError("现役版本已经变化，这个挑战者已作废")
    champ = registry.get_version(dep["version_id"])
    cand = json.loads(ch["candidate_json"])
    rr = dict(research.get_research(), source="test") if ch["is_test"] else research.get_research()
    msg = challenger._promote_btc(ch, dep, champ, cand, None, rr, f"{actor or '用户'} 批准（{ch['reason'] or ''}）"[:400],
                                  trigger="人工批准", actor=actor)
    now = pr.get_deployment(dep["id"])
    new_vid = db.q1("SELECT new_version_id FROM evolve_challengers WHERE id=?", (ch["id"],))["new_version_id"]
    switched = now["version_id"] == new_vid
    res = {"result": "已晋级" if switched else "已批准，等待空仓后切换",
           "message": msg + ("" if switched else "；该策略空仓且没有未完成订单时切换")}
    audit(actor, role, "批准晋级", key, champ["name"], dep["id"], champ["id"], new_vid, res["result"], f"挑战者 #{ch['id']}")
    return res


def reject(ch_id: int, reason: str, *, actor: str | None, role: str | None) -> dict:
    from . import challenger
    from .paper import runner as pr
    require_role(role)
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("请填写拒绝原因")
    ch = db.q1("SELECT * FROM evolve_challengers WHERE id=?", (int(ch_id),))
    if not ch or ch["status"] != "awaiting":
        raise ValueError("这条记录不在待确认队列中")
    challenger._close(ch, "rejected", f"{actor or '用户'} 拒绝：{reason}", trigger="人工拒绝", actor=actor)
    dep = pr.get_deployment(ch["deployment_id"]) if ch["deployment_id"] else None
    pr.event(ch["deployment_id"], "INFO", f"{actor or '用户'} 拒绝挑战者 #{ch['id']}：{reason}"[:480],
             bool(ch["is_test"]))
    audit(actor, role, "拒绝晋级", _dep_key(dep) if dep else ("etf" if ch["kind"] == "etf" else None), ch["label"],
          ch["deployment_id"], ch["champion_version_id"], None, "已淘汰", f"挑战者 #{ch['id']}：{reason}")
    return {"result": "已淘汰", "message": f"挑战者 #{ch['id']} 已淘汰"}


# ---------------------------------------------------------------------------
# “策略为什么没有动作”：冷却 / 长期体检暂停 / 信号过期 / 资金权重公式 / 波动率定仓
# ---------------------------------------------------------------------------

_vol_cache: dict = {}


def _bj(iso) -> str:
    import pandas as pd
    return (pd.Timestamp(iso).tz_convert("UTC") + pd.Timedelta(hours=8)).strftime("%m-%d %H:%M")


def _dur(td) -> str:
    mins = int(td.total_seconds() // 60)
    d, h, m = mins // 1440, (mins % 1440) // 60, mins % 60
    return (f"{d} 天 " if d else "") + (f"{h} 小时 " if h or d else "") + f"{m} 分"


def vol_sizing(df5=None) -> dict | None:
    """波动率定仓：仓位系数 = 目标年化波动率 ÷ 近 N 日年化波动率，限制在 [最低系数, 100%]。与回测 / 模拟盘同一套规则。"""
    import math
    import time
    import numpy as np
    import pandas as pd
    from . import rhythm
    rh = rhythm.current()
    if rh is None or not rh.vol_target_pct:
        return None
    if df5 is None:
        hit = _vol_cache.get("v")
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        try:
            from .data import market_data as md
            m = md.load_master()
            df5 = m[m["ts"] >= m["ts"].max() - pd.Timedelta(days=rh.vol_lookback_days * 3 + 5)]
        except Exception:  # noqa: BLE001
            return None
    if df5 is None or df5.empty:
        return None
    day = df5.assign(ts=pd.to_datetime(df5["ts"], utc=True)).set_index("ts")["close"].resample("1D").last().dropna()
    now = pd.Timestamp.now(tz="UTC").floor("D")
    day = day[day.index < now]                       # 只用已收盘日线（与 daily_context 一致）
    r = np.log(day).diff().dropna().tail(rh.vol_lookback_days)
    if len(r) < max(5, rh.vol_lookback_days // 2):
        return None
    vol = float(r.std() * math.sqrt(365) * 100)
    raw = rh.vol_target_pct / vol if vol > 0 else 1.0
    mult = min(max(raw, rh.vol_min_mult), 1.0)
    out = {"vol": round(vol, 2), "target": rh.vol_target_pct, "lookback": rh.vol_lookback_days, "raw": round(raw, 4),
           "mult": round(mult, 4), "min": rh.vol_min_mult, "days": len(r),
           "clipped": "min" if raw < rh.vol_min_mult else ("max" if raw > 1.0 else None)}
    _vol_cache["v"] = (time.time(), out)
    return out


def weight_text(p: dict | None) -> str | None:
    if not p or not p.get("detail"):
        return None
    d = p["detail"]
    m = d.get("method")
    if m == "long_pause":
        return "长期体检暂停：额度 0（释放给长期有优势的策略）"
    if m == "paused_floor":
        return f"暂停新开仓：只保留最低额度（账户权益的 {d['floor_pct']:g}%）"
    share = f"；占全部运行策略权重的 {p['share_pct']:.1f}%" if p.get("share_pct") is not None else ""
    if m == "long":
        head = (f"权重 = 2 × 长期年均收益 ÷ max(年均回撤, 1) = 2 × {d['mean_ret']:+.1f}% ÷ {max(d['mean_dd'], 1.0):.1f}% "
                f"= {d['raw']:.2f}")
        basis = f"（近 {d['n_years']} 年，盈利 {d['pos_years']} 年）"
    else:
        if d.get("raw") is None:
            return f"训练集收益 {d.get('ret') if d.get('ret') is not None else '--'}%，不为正 → 取最低权重 {d['wmin']:g}" + share
        head = f"权重 = 训练集收益 ÷ max(|回撤|, 1) = {d['ret']:+.1f}% ÷ {max(abs(d['dd'] or 0), 1.0):.1f}% = {d['raw']:.2f}"
        basis = "（还没有长周期结果，暂用一年训练集）"
    c = d.get("clipped")
    if c == "negative":
        tail = f" → 长期年均收益为负，取最低权重 {d['wmin']:g}"
    elif c == "max":
        tail = f" → 超过上限，截断为 {d['wmax']:g}"
    elif c == "min":
        tail = f" → 低于下限，抬到 {d['wmin']:g}"
    else:
        tail = f"（在 {d['wmin']:g}~{d['wmax']:g} 之间，未截断）"
    return head + tail + basis + share


def explain(dep: dict | None, plan: dict | None = None, price: float | None = None, vol: dict | None = None) -> dict:
    """解释这个部署当前为什么没有动作。返回 {"headline": 一句话, "items": [(标题, 说明)], "calc": [(标题, 公式)]}。"""
    import pandas as pd
    from . import rhythm
    if not dep:
        return {"headline": "", "items": [], "calc": []}
    now = pd.Timestamp.now(tz="UTC")
    cfg = dep["config"]
    items, calc, heads = [], [], []
    if cfg.get("pending_offline"):
        heads.append("等待空仓后下线：不再开新仓")
    try:
        from .paper import etf_trend
        if dep.get("broker") == "alpaca_paper" and not dep.get("is_test") and etf_trend.reclaim_active():
            heads.append("美股 ETF 正在收回临时借出的额度：暂停新开仓")
            c = etf_trend.pending_cut(dep["id"])
            done = ((etf_trend.lend_state().get("cuts") or {}).get(str(dep["id"])) or {})
            cut_txt = (f"按借用比例 {c['frac']:.1%} 减仓待执行（额度 {c['before']:,.0f}→{c['after']:,.0f} USD）" if c else
                       (f"已归还：{done.get('note')}" if done.get("done") else "本策略没有用到借来的额度，无需减仓"))
            items.append(("额度收回", "美股 ETF 空仓时，它的一部分额度临时给了 BTC；现在美股发出买入信号（ETF 买入优先），"
                                     "BTC 在下一个美股开盘前卖出借用期间所开仓位的借用比例、暂停新开仓，等美股买入后恢复。"
                                     + cut_txt))
    except Exception:  # noqa: BLE001
        pass
    # 长期体检 / 自动暂停
    ap = cfg.get("auto_pause")
    lm = None
    try:
        lm = rhythm.long_metrics(dep)
    except Exception:  # noqa: BLE001
        lm = None
    rc = rhythm.get_config()
    if dep["status"] == "paused_entries":
        if ap and ap.get("kind") == "long" and lm:
            bad = []
            if lm["mean_ret"] < 0:
                bad.append(f"近 {lm['n_years']} 年年均收益 {lm['mean_ret']:+.1f}%（为负）")
            if lm["pos_years"] < int(rc["long_min_pos_years"]):
                bad.append(f"盈利年份 {lm['pos_years']}/{lm['n_years']}（少于 {rc['long_min_pos_years']} 年）")
            txt = "，".join(bad) or f"近 {lm['n_years']} 年年均收益 {lm['mean_ret']:+.1f}%，盈利 {lm['pos_years']}/{lm['n_years']} 年"
            items.append(("长期体检暂停", f"{txt}；年均回撤 {lm['mean_dd']:.1f}%。规则：年均收益为负且盈利年份少于 "
                                       f"{rc['long_min_pos_years']} 年 → 暂停新开仓，额度释放；达标后自动恢复"))
            heads.append("长期体检暂停新开仓")
        elif ap:
            items.append(("自动暂停", ap.get("reason") or "长期亏损"))
            heads.append("自动暂停新开仓")
        elif not cfg.get("pending_offline"):
            items.append(("暂停新开仓", "手动暂停或持仓对账保护；已有持仓仍按规则卖出"))
            heads.append("暂停新开仓")
    elif lm:
        items.append(("长期体检", f"{'达标' if lm.get('ok') else '不达标'}：近 {lm['n_years']} 年年均收益 {lm['mean_ret']:+.1f}%，"
                                f"盈利 {lm['pos_years']}/{lm['n_years']} 年，年均回撤 {lm['mean_dd']:.1f}%"))
    # 冷却 / 连亏
    st_ = db.get_setting(f"rhythm_state::{dep['id']}") or {}
    bu = pd.Timestamp(st_["block_until"]) if st_.get("block_until") else None
    streak = int(st_.get("streak") or 0)
    rh = rhythm.current()
    if bu is not None and bu > now:
        why = st_.get("block_why") or "冷却"
        cause = "止损" if "止损" in why else ("连亏 " + why.split("连亏")[1].split("刹车")[0].strip() if "连亏" in why else why)
        items.append(("冷却中", f"原因：{cause}（{why}）；剩余 {_dur(bu - now)}，到 {_bj(bu)} 北京时间为止不开新仓"))
        heads.append(f"冷却中（{cause}），还剩 {_dur(bu - now)}")
    elif rh is not None and rh.loss_streak_n:
        items.append(("连亏计数", f"当前连亏 {streak} 笔（满 {rh.loss_streak_n} 笔暂停 {rh.loss_streak_pause_hours:g} 小时）"))
    # 信号过期
    stale = db.get_setting(f"stale_signal::{dep['id']}")
    if stale and stale.get("at") and now - pd.Timestamp(stale["at"]) < pd.Timedelta(days=7):
        items.append(("入场信号过期未追单", f"信号K线收盘 {_bj(stale['signal_bar'])} 北京时间，{_bj(stale['at'])} 才检查到，"
                                        f"已过 {stale['age_min']} 分钟（超过 {stale['limit_min']} 分钟上限），放弃这次入场，"
                                        "不在远离信号的价格追单"))
        if now - pd.Timestamp(stale["at"]) < pd.Timedelta(hours=24):
            heads.append(f"最近一次入场信号过期未追单（{_bj(stale['signal_bar'])}）")
    # 最近一次判断
    last = db.get_setting(f"paper_loop::{dep['id']}") or {}
    if last.get("reason"):
        items.append(("最近一次判断", f"{_bj(last['at']) if last.get('at') else '--'}：{last.get('reason')}"))
    if not heads:
        if last.get("action") == "blocked":
            heads.append(last.get("reason") or "被保护规则阻止")
        elif last.get("reason"):
            heads.append(last["reason"])
        else:
            heads.append("等待下一次 K 线收盘检查")
    # 计算明细：资金权重 + 波动率定仓 + 当前仓位
    wt = weight_text(plan)
    if wt:
        calc.append(("资金权重", wt))
    budget = float(cfg.get("budget_usd") or 0)
    if vol:
        cl = {"min": f"，低于下限，取 {vol['min']:.0%}", "max": "，超过 100%，取 100%"}.get(vol["clipped"], "")
        calc.append(("波动率定仓", f"仓位系数 = 目标年化波动率 {vol['target']:g}% ÷ 近 {vol['lookback']} 日年化波动率 {vol['vol']:.1f}% "
                                f"= {vol['raw']:.2f}{cl} → 下一笔买入使用额度的 {vol['mult']:.0%}"
                                + (f"（约 {budget * 0.99 * vol['mult']:,.0f} USD）" if budget else "")))
    qty = _ledger_qty(dep["id"])
    if budget and price:
        calc.append(("当前仓位比例", f"持仓 {qty:.6f} BTC × {price:,.0f} = {qty * price:,.0f} USD ÷ 额度 {budget:,.0f} USD "
                                 f"= {qty * price / budget:.0%}"))
    elif budget:
        calc.append(("当前仓位比例", f"持仓 {qty:.6f} BTC（额度 {budget:,.0f} USD）"))
    return {"headline": "；".join(heads), "items": items, "calc": calc}


def plans_for(broker: str = "alpaca_paper") -> dict[int, dict]:
    """当前全部运行 / 暂停部署的权重计划（只用于解释公式；实际额度以部署配置为准）。"""
    from . import evolve
    deps = [d for d in _deployments(broker) if d["status"] in ("running", "paused_entries")]
    if broker == "alpaca_paper":
        deps = [d for d in deps if not d["is_test"]]
    if not deps:
        return {}
    snap = None
    try:
        snap = db.q1("SELECT equity FROM account_snapshots ORDER BY ts DESC LIMIT 1")
    except Exception:  # noqa: BLE001
        pass
    try:
        return evolve.plan_budgets(float(snap["equity"]) if snap else 100_000.0, deps)
    except Exception:  # noqa: BLE001
        log.exception("plan budgets failed")
        return {}


# ---------------------------------------------------------------------------
# 美股 ETF 择时：策略总台卡片（复用同一套版本 / 锁定 / 待确认 / 审计机制）
# ---------------------------------------------------------------------------

def etf_explain(equity: float | None = None) -> dict:
    """美股 ETF 为什么没有动作：与 explain() 相同的结构 {"headline", "items", "calc"}。"""
    import pandas as pd
    from . import market as mk
    from . import rhythm
    from .paper import etf_trend
    cfg = etf_trend.get_config()
    now = pd.Timestamp.now(tz="UTC")
    items, calc, heads = [], [], []
    w = etf_trend.get_why() or {}
    if w.get("text"):
        heads.append(w["text"])
        items.append(("最近一次判断", f"{_bj(w.get('at'))}：{w['text']}"))
    stt = db.get_setting("etf_trend_state") or {}
    sig = stt.get("signal") or {}
    if sig:
        pos_txt = "看多（持有）" if sig.get("regime") else "看空（现金）"
        items.append(("信号", f"{sig.get('date')} 收盘 {cfg['signal_symbol']} {sig['close']:.2f}，{cfg['sma']} 日均线 "
                             f"{sig['sma']:.2f}，缓冲带 {sig['lower']:.2f}~{sig['upper']:.2f} → {pos_txt}"))
    lm = rhythm.etf_long_metrics()
    rc = rhythm.get_config()
    if lm:
        hint_only = rhythm.us_long_hint_only()
        items.append(("长期体检", (f"{rhythm.US_LONG_HINT_TEXT}（不拦截开仓）。" if hint_only else "")
                                + f"{'达标' if lm.get('ok') else '不达标'}：{lm['n_years']} 年年均收益 {lm['mean_ret']:+.1f}%，"
                                f"盈利 {lm['pos_years']}/{lm['n_years']} 年，年均回撤 {lm['mean_dd']:.1f}%"
                                + ("" if lm.get("ok") or hint_only else
                                   f"（年均收益为负且盈利年份少于 {rc['long_min_pos_years']} 年 → 暂停新开仓）")))
    else:
        items.append(("长期体检", f"{rhythm.US_LONG_HINT_TEXT}（不拦截开仓）；尚未计算" if rhythm.us_long_hint_only() else "尚未计算"))
    g = db.get_setting(f"rhythm_state::{rhythm.ETF_KEY}") or {}
    bu = pd.Timestamp(g["block_until"]) if g.get("block_until") else None
    rh = rhythm.configured(mk.US_EQUITY)
    if rhythm.is_shadow(mk.US_EQUITY):
        sh = db.get_setting("etf_rhythm_shadow") or {}
        items.append(("美股交易节奏", "影子模式：只计算显示，不影响下单和回测（训练期重做研究的结论确认前）"
                                   + (f"；最近一次：{sh['note']}（{_bj(sh.get('at'))}）" if sh.get("note") else "")))
        if bu is not None and bu > now:
            items.append(("影子冷却", f"如果生效：{g.get('block_why') or '交易节奏'}，到 {_bj(bu)} 北京时间"))
        try:
            items.append(("转正条件", rhythm.us_promotion_status()["text"]))
        except Exception:  # noqa: BLE001
            pass
        bu = None
    if bu is not None and bu > now:
        items.append(("冷却中", f"{g.get('block_why') or '交易节奏'}；剩余 {_dur(bu - now)}，到 {_bj(bu)} 北京时间（按交易日计算，节假日顺延）"))
        if not heads or "节奏" not in heads[0]:
            heads.append(f"交易节奏冷却中，还剩 {_dur(bu - now)}")
    elif rh is not None and rh.loss_streak_n:
        items.append(("连亏计数", f"当前连亏 {int(g.get('streak') or 0)} 笔（满 {rh.loss_streak_n} 笔暂停 "
                                f"{rh.loss_streak_pause_hours:g} 个交易日）"))
    if cfg.get("pending_offline"):
        heads.insert(0, "等待空仓后下线：不再开新仓")
    if not cfg.get("enabled"):
        heads = ["已下线"]
    pct = float(cfg["budget_pct"])
    ls = etf_trend.lend_state()
    if ls.get("state") == "lent":
        items.append(("临时借出额度", f"美股空仓，BTC 最多临时使用美股额度的 {float(ls.get('lend_pct', 50)):g}%（{_bj(ls.get('since'))} 起）"))
    elif ls.get("state") == "reclaim":
        cuts = ls.get("cuts") or {}
        cut_txt = "；".join(f"#{k} 额度 {v['before']:,.0f}→{v['after']:,.0f}，按 {v['frac']:.1%} 减仓"
                           + ("（已完成：" + str(v.get("note") or "") + "）" if v.get("done") else "（待执行）")
                           for k, v in cuts.items()) or "BTC 没有用到借来的额度，无需减仓"
        items.append(("正在收回额度", f"美股发出买入信号（ETF 买入优先）：BTC 在下一个美股开盘前按借用比例减仓、暂停新开仓，"
                                   f"开盘后先还款再买入（{_bj(ls.get('since'))} 起）。{cut_txt}"))
    ex = db.get_setting("account_exposure") or {}
    if ex:
        items.append(("账户杠杆", f"总持仓市值 ÷ 权益 = {float(ex.get('ratio') or 0):.1%}（超过 100% 或现金为负时告警；"
                               "任何情况下都不使用券商保证金）" + ("⚠️" if ex.get("over") or ex.get("negative_cash") else "")))
    if equity:
        share = etf_trend.capital_share(equity)
        calc.append(("资金比例", f"固定 {pct:g}% × 账户权益 {equity:,.0f} = {equity * pct / 100:,.0f} USD（不参与 BTC 的按实力分配）"))
        calc.append(("从 BTC 额度中扣除", f"max(设定比例 {pct:g}%, 当前持仓市值占比) = {share:.1f}%"))
    else:
        calc.append(("资金比例", f"固定 {pct:g}%（不参与 BTC 的按实力分配）"))
    if rh is not None and rh.vol_target_pct and not rhythm.is_shadow(mk.US_EQUITY):
        calc.append(("波动率定仓", f"目标年化波动率 {rh.vol_target_pct:g}%（√252 年化）"))
    else:
        calc.append(("波动率定仓", "美股不采用（研究见交易节奏说明）：每次买入使用全部资金比例"))
    pos = stt.get("position") or {}
    if pos and equity:
        calc.append(("当前仓位", f"{cfg['trade_symbol']} {float(pos.get('qty') or 0):,.4f} 股，市值 {float(pos.get('market_value') or 0):,.0f} USD"
                                f" = 账户权益的 {float(pos.get('market_value') or 0) / equity:.1%}"))
    return {"headline": "；".join(heads) or "等待下一次美股开盘检查", "items": items, "calc": calc}


def etf_row(equity: float | None = None) -> dict:
    """策略总台的美股 ETF 卡片数据。"""
    from . import challenger, evolog
    from .paper import etf_trend, etf_versions
    etf_versions.ensure_tables()
    challenger.ensure_tables()
    cfg = etf_trend.get_config()
    run = etf_versions.running()
    vs = etf_versions.versions()
    lab = max(vs, key=lambda v: v["version_no"]) if vs else None
    mismatch = bool(lab and lab["id"] != run["id"] and lab["version_no"] > run["version_no"])
    diff = []
    if mismatch:
        plain, _ = evolog.etf_diff({"sma": run["sma"], "band": run["band"]}, {"sma": lab["sma"], "band": lab["band"]})
        diff = plain
    ch = None
    for c in challenger.running("etf"):
        ev = json.loads(c["last_eval_json"] or "{}")
        tj = json.loads(c["train_json"] or "{}")
        ch = {"id": c["id"], "change": c["change"], "days": ev.get("days"), "need_days": ev.get("need_days"),
              "fast": (tj.get("fast") or {}).get("verdict"), "challenger": ev.get("challenger"), "champion": ev.get("champion"),
              "split_note": tj.get("split_note")}
    aw = next((a for a in awaiting() if a["kind"] == "etf"), None)
    from . import holdout
    lin = challenger.etf_lineage()
    exp = holdout.exposure(lin)
    stt = db.get_setting("etf_trend_state") or {}
    pos = stt.get("position") or {}
    status = etf_versions.status()
    try:
        from . import rhythm
        if status == "运行中" and rhythm.etf_long_paused():
            status = "暂停中"
    except Exception:  # noqa: BLE001
        pass
    return {"key": "etf", "name": "美股 ETF 择时", "status": status,
            "run": run, "lab": lab, "mismatch": mismatch, "diff": diff, "lock": etf_versions.lock_state(),
            "block": challenger._etf_block(), "challenger": ch, "awaiting": aw, "versions": vs,
            "qty": float(pos.get("qty") or 0), "value": float(pos.get("market_value") or 0) if pos else None,
            "unrealized": float(pos.get("unrealized_pl") or 0) if pos else None,
            "budget_pct": float(cfg["budget_pct"]), "pending_offline": bool(cfg.get("pending_offline")),
            "enabled": bool(cfg.get("enabled")), "why": etf_explain(equity),
            "trade_symbol": cfg["trade_symbol"], "signal_symbol": cfg["signal_symbol"], "lineage": lin,
            "exposed": exp, "validation_used": holdout.used(lin), "lend_pct": float(cfg.get("lend_pct", 50.0)),
            "lend_state": etf_trend.lend_state().get("state", "none")}
