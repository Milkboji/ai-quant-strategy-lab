"""进化记录：每一次晋级、淘汰、拒绝都写一行结构化记录（页面「进化记录」）。

字段：时间、策略、旧版本 → 新版本、参数差异（大白话 + 原始规则 diff）、季度稳健评分、留出检验 / 快速检验结果、
前向测试期间双方收益与回撤、触发方式（自动 / 人工批准 / 人工拒绝）、操作人、原因。

写入点：
- evolve.evolve_one 直接替换（record_direct）；
- challenger._close：挑战者晋级 / 淘汰 / 拒绝（record_challenger），BTC 与美股 ETF 共用；
首次使用时从 evolve_challengers / evolve_results 回填历史（source + source_id 唯一，可重复执行）。
"""
from __future__ import annotations

import difflib
import json
import logging

from . import db

log = logging.getLogger("aiquant.evolog")

EVENTS = ("晋级", "淘汰", "拒绝")
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS evolution_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, event TEXT NOT NULL, trigger TEXT NOT NULL,
  actor TEXT, kind TEXT, strategy_key TEXT, label TEXT, deployment_id INTEGER,
  from_version_id INTEGER, to_version_id INTEGER, from_params TEXT, to_params TEXT,
  diff_plain TEXT, diff_raw TEXT, score_json TEXT, holdout_json TEXT, forward_json TEXT,
  reason TEXT, challenger_id INTEGER, source TEXT, source_id INTEGER, is_test INTEGER DEFAULT 0,
  UNIQUE(source, source_id));
CREATE INDEX IF NOT EXISTS ix_evolog_ts ON evolution_log(ts);
"""
_ready = {"done": False}


def ensure_tables() -> None:
    with db.tx() as c:
        c.executescript(SCHEMA_SQL)
    if not _ready["done"]:
        _ready["done"] = True
        try:
            backfill()
        except Exception:  # noqa: BLE001
            log.exception("evolution log backfill failed")


def _j(x) -> str | None:
    return json.dumps(x, ensure_ascii=False) if x is not None else None


def schema_diff(a: dict | None, b: dict | None) -> tuple[list, str]:
    """(大白话差异 [(项目, 旧, 新)], 原始规则 unified diff)。"""
    from .strategy import registry
    if not a or not b:
        return [], ""
    plain = [list(x) for x in registry.diff(a, b)]
    sa = json.dumps(a, ensure_ascii=False, indent=1, sort_keys=True).splitlines()
    sb = json.dumps(b, ensure_ascii=False, indent=1, sort_keys=True).splitlines()
    raw = "\n".join(difflib.unified_diff(sa, sb, "旧版本", "新版本", lineterm="", n=1))
    return plain, raw


def etf_diff(a: dict, b: dict) -> tuple[list, str]:
    plain = []
    if a.get("sma") != b.get("sma"):
        plain.append(["均线长度（日）", str(a.get("sma")), str(b.get("sma"))])
    if a.get("band") != b.get("band"):
        plain.append(["缓冲带", f"±{float(a.get('band') or 0):.0%}", f"±{float(b.get('band') or 0):.0%}"])
    raw = "\n".join(difflib.unified_diff(json.dumps(a, sort_keys=True, indent=1).splitlines(),
                                         json.dumps(b, sort_keys=True, indent=1).splitlines(),
                                         "旧参数", "新参数", lineterm="", n=1))
    return plain, raw


def _insert(row: dict) -> int | None:
    ensure_tables()
    cols = list(row)
    try:
        return db.ex(f"INSERT OR IGNORE INTO evolution_log({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                     [row[c] for c in cols])
    except Exception:  # noqa: BLE001
        log.exception("evolution log insert failed")
        return None


def _key_of(dep_id: int | None, vid: int | None) -> str | None:
    from . import console
    from .strategy import registry
    if dep_id:
        d = db.q1("SELECT config_json FROM paper_deployments WHERE id=?", (dep_id,))
        if d:
            k = json.loads(d["config_json"] or "{}").get("strategy_key")
            if k:
                return k
    return console.key_of_version(registry.get_version(vid)) if vid else None


def record_direct(*, dep: dict, cur: dict, new: dict, rec: dict, ho: dict, run_id: int) -> None:
    """进化直接通过全部检验并替换（没有经过挑战者阶段）。"""
    plain, raw = schema_diff(cur["schema"], new["schema"])
    lid = _insert({
        "ts": db.now_iso(), "event": "晋级", "trigger": "自动", "actor": "系统", "kind": "btc",
        "strategy_key": _key_of(dep["id"], cur["id"]), "label": rec.get("label"), "deployment_id": dep["id"],
        "from_version_id": cur["id"], "to_version_id": new["id"], "diff_plain": _j(plain), "diff_raw": raw,
        "score_json": _j({"champion": rec.get("current"), "challenger": rec.get("best")}),
        "holdout_json": _j({k: v for k, v in (ho or {}).items() if not str(k).startswith("_")}),
        "forward_json": None, "reason": rec.get("reason"), "challenger_id": None,
        "source": f"evolve_run:{run_id}", "source_id": dep["id"], "is_test": int(dep.get("is_test") or 0)})
    _after(lid)


def record_challenger(ch: dict, status: str, reason: str, new_vid: int | None = None,
                      trigger: str = "自动", actor: str | None = None, notify_: bool = True) -> None:
    """挑战者被关闭时调用：promoted → 晋级；rejected → 淘汰（自动）/ 拒绝（人工）。"""
    from .strategy import registry
    if status not in ("promoted", "rejected"):
        return
    event = "晋级" if status == "promoted" else ("拒绝" if trigger == "人工拒绝" else "淘汰")
    train = json.loads(ch["train_json"]) if ch.get("train_json") else {}
    last = json.loads(ch["last_eval_json"]) if ch.get("last_eval_json") else None
    cand = json.loads(ch["candidate_json"]) if ch.get("candidate_json") else None
    row = {"ts": db.now_iso(), "event": event, "trigger": trigger, "actor": actor or "系统", "kind": ch["kind"],
           "label": ch.get("label"), "deployment_id": ch.get("deployment_id"), "reason": reason,
           "challenger_id": ch["id"], "source": f"challenger:{status}", "source_id": ch["id"],
           "is_test": int(ch.get("is_test") or 0),
           "score_json": _j({"champion": (train or {}).get("champion"),
                             "challenger": {k: v for k, v in (train or {}).items()
                                            if k not in ("fast", "champion", "holdout")} or None}),
           "holdout_json": _j({"holdout": (train or {}).get("holdout"), "fast": (train or {}).get("fast")}),
           "forward_json": _j(last)}
    if ch["kind"] == "etf":
        a = json.loads(ch["champion_params"]) if ch.get("champion_params") else {}
        b = cand or {}
        plain, raw = etf_diff(a, b)
        row.update(strategy_key="etf", label=ch.get("label") or "美股 ETF 择时", from_params=_j(a), to_params=_j(b),
                   diff_plain=_j(plain), diff_raw=raw)
    else:
        champ = registry.get_version(ch.get("champion_version_id")) if ch.get("champion_version_id") else None
        new = registry.get_version(new_vid) if new_vid else None
        plain, raw = schema_diff(champ["schema"] if champ else None, (new or {}).get("schema") or cand)
        row.update(strategy_key=_key_of(ch.get("deployment_id"), ch.get("champion_version_id")),
                   from_version_id=ch.get("champion_version_id"), to_version_id=new_vid,
                   diff_plain=_j(plain), diff_raw=raw)
    lid = _insert(row)
    if notify_:
        _after(lid)


def _after(lid: int | None) -> None:
    """新记录写入后：晋级事件发通知。"""
    if not lid:
        return
    r = db.q1("SELECT * FROM evolution_log WHERE id=?", (lid,))
    if not r or r["event"] != "晋级" or r["is_test"]:
        return
    try:
        from . import notify
        notify.event("promotion", f"策略晋级：{r['label'] or r['strategy_key'] or ''}",
                     f"{describe(r)}\n触发方式：{r['trigger']}\n{r['reason'] or ''}")
    except Exception:  # noqa: BLE001
        log.exception("promotion notify failed")


def describe(r: dict) -> str:
    from .strategy import registry
    if r.get("kind") == "etf":
        a, b = json.loads(r["from_params"] or "{}"), json.loads(r["to_params"] or "{}")
        return f"美股 ETF：{a.get('sma')} 日均线 ±{float(a.get('band') or 0):.0%} → {b.get('sma')} 日均线 ±{float(b.get('band') or 0):.0%}"
    old = registry.label(registry.get_version(r["from_version_id"])) if r.get("from_version_id") else "--"
    new = registry.label(registry.get_version(r["to_version_id"])) if r.get("to_version_id") else "（未生成新版本）"
    return f"{old} → {new}"


def rows(limit: int = 200, events: list[str] | None = None, key: str | None = None,
         include_test: bool = False) -> list[dict]:
    ensure_tables()
    sql, args = "SELECT * FROM evolution_log WHERE 1=1", []
    if not include_test:
        sql += " AND is_test=0"
    if events:
        sql += f" AND event IN ({','.join('?' * len(events))})"
        args += list(events)
    if key:
        sql += " AND strategy_key=?"
        args.append(key)
    out = db.q(sql + " ORDER BY ts DESC, id DESC LIMIT ?", (*args, limit))
    for r in out:
        for k in ("diff_plain", "score_json", "holdout_json", "forward_json"):
            r[k.replace("_json", "")] = json.loads(r[k]) if r.get(k) else None
    return out


def backfill() -> int:
    """从已有的挑战者与进化结果回填历史记录（幂等）。"""
    from .strategy import registry
    n = 0
    try:
        chs = db.q("SELECT * FROM evolve_challengers WHERE status IN ('promoted','rejected') ORDER BY id")
    except Exception:  # noqa: BLE001
        chs = []
    for ch in chs:
        if db.q1("SELECT id FROM evolution_log WHERE source=? AND source_id=?", (f"challenger:{ch['status']}", ch["id"])):
            continue
        manual = "拒绝：" in (ch["reason"] or "") and "淘汰" not in (ch["reason"] or "")[:4]
        trig = "人工拒绝" if (ch["status"] == "rejected" and manual) else (
            "人工批准" if "批准" in (ch["reason"] or "") else "自动")
        record_challenger(ch, ch["status"], ch["reason"] or "", ch.get("new_version_id"), trigger=trig, notify_=False)
        db.ex("UPDATE evolution_log SET ts=? WHERE source=? AND source_id=?",
              (ch["decided_at"] or ch["registered_at"], f"challenger:{ch['status']}", ch["id"]))
        n += 1
    try:
        res = db.q("SELECT r.* FROM evolve_results r JOIN evolve_runs u ON u.id=r.run_id WHERE u.is_test=0 "
                   "AND r.decision='替换' AND r.new_version_id IS NOT NULL ORDER BY r.id")
    except Exception:  # noqa: BLE001
        res = []
    for r in res:
        src = f"evolve_run:{r['run_id']}"
        if db.q1("SELECT id FROM evolution_log WHERE source=? AND source_id=?", (src, r["deployment_id"])):
            continue
        cur, new = registry.get_version(r["current_version_id"]), registry.get_version(r["new_version_id"])
        if not cur or not new:
            continue
        plain, raw = schema_diff(cur["schema"], new["schema"])
        best = json.loads(r["best_json"]) if r["best_json"] else None
        db.ex("INSERT OR IGNORE INTO evolution_log(ts,event,trigger,actor,kind,strategy_key,label,deployment_id,"
              "from_version_id,to_version_id,diff_plain,diff_raw,score_json,holdout_json,reason,source,source_id,is_test)"
              " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
              (r["created_at"], "晋级", "自动", "系统", "btc", _key_of(r["deployment_id"], cur["id"]), r["label"],
               r["deployment_id"], cur["id"], new["id"], _j(plain), raw,
               _j({"champion": json.loads(r["current_json"]) if r["current_json"] else None, "challenger": best}),
               r["holdout_json"], r["reason"], src, r["deployment_id"]))
        n += 1
    return n
