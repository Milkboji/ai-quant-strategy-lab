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
            "qty": qty, "value": qty * price if price else None,
            "budget": b if dep else 0.0, "weight_pct": (b / total_budget * 100) if dep and total_budget else None,
            "budget_reason": (dep or {}).get("config", {}).get("budget_reason") if dep else None,
            "signal_tf": (dep or {}).get("config", {}).get("signal_tf") if dep else None,
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
    audit(actor, role, "部署此版本", key, v["name"], did, None, v["id"], res["result"],
          "重新上线" if was_offline else "")
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
