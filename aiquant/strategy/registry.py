"""Strategy Registry：策略位草稿、版本冻结、版本谱系与差异对比。

已冻结的版本不可修改；任何修改（手动 / AI 优化）都会创建新版本。
"""
from __future__ import annotations

import json
import uuid

from .. import db
from .schema import StrategySchema, canonical, describe, schema_hash, validate


class FreezeError(ValueError):
    pass


def get_slot(slot_no: int) -> dict:
    s = db.q1("SELECT * FROM slots WHERE slot_no=?", (slot_no,))
    if s is None:
        raise KeyError(slot_no)
    s["draft_fields"] = json.loads(s["draft_fields_json"] or "{}")
    return s


def all_slots() -> list[dict]:
    return [get_slot(i) for i in range(1, 6)]


def save_draft(slot_no: int, *, text: str | None = None, draft_schema: dict | None = None,
               timeframe_mode: str | None = None, aux_tf: int | None | str = "keep",
               parse_info: dict | None = None) -> None:
    s = get_slot(slot_no)
    fields = s["draft_fields"]
    if draft_schema is not None:
        fields["schema"] = draft_schema
    if parse_info is not None:
        fields["parse"] = parse_info
    sets = ["draft_fields_json=?", "updated_at=?"]
    vals: list = [json.dumps(fields, ensure_ascii=False), db.now_iso()]
    if text is not None:
        sets.append("draft_text=?"); vals.append(text)
    if timeframe_mode is not None:
        sets.append("timeframe_mode=?"); vals.append(timeframe_mode)
    if aux_tf != "keep":
        sets.append("aux_timeframe_min=?"); vals.append(aux_tf)
    vals.append(slot_no)
    db.ex(f"UPDATE slots SET {', '.join(sets)} WHERE slot_no=?", vals)


def get_version(vid: int | None) -> dict | None:
    if vid is None:
        return None
    v = db.q1("SELECT * FROM strategy_versions WHERE id=?", (vid,))
    if v:
        v["schema"] = json.loads(v["schema_json"])
    return v


def lineage_versions(lineage_id: str) -> list[dict]:
    rows = db.q("SELECT * FROM strategy_versions WHERE lineage_id=? ORDER BY version_no", (lineage_id,))
    for r in rows:
        r["schema"] = json.loads(r["schema_json"])
    return rows


def all_versions() -> list[dict]:
    rows = db.q("SELECT * FROM strategy_versions ORDER BY id DESC")
    for r in rows:
        r["schema"] = json.loads(r["schema_json"])
    return rows


def freeze(slot_no: int | None, schema_dict: dict, *, nl_text: str = "", source: str = "manual",
           change_summary: str = "", parent_id: int | None = None, new_lineage: bool = False,
           llm_call_id: int | None = None, set_current: bool = True) -> dict:
    """校验并冻结一个版本。相同内容（哈希相同）在同一谱系中不会重复创建。"""
    obj, errs = validate(schema_dict)
    if errs:
        raise FreezeError("；".join(errs))
    h = schema_hash(obj)
    parent = get_version(parent_id) if parent_id else None
    if parent is None and slot_no is not None and not new_lineage:
        parent = get_version(get_slot(slot_no)["current_version_id"])
    lineage = parent["lineage_id"] if (parent and not new_lineage) else f"L{slot_no or 0}-{uuid.uuid4().hex[:8]}"
    dup = db.q1("SELECT id FROM strategy_versions WHERE lineage_id=? AND schema_hash=?", (lineage, h))
    if dup:
        vid = dup["id"]
    else:
        mx = db.q1("SELECT COALESCE(MAX(version_no),0) AS m FROM strategy_versions WHERE lineage_id=?", (lineage,))
        vno = int(mx["m"]) + 1
        if not change_summary:
            change_summary = "初始版本" if parent is None else summarize_diff(parent["schema"], obj.model_dump())
        vid = db.ex(
            "INSERT INTO strategy_versions(lineage_id,version_no,parent_id,slot_no,name,source,change_summary,"
            "nl_text,schema_json,schema_hash,llm_call_id,created_at,frozen) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (lineage, vno, parent["id"] if parent else None, slot_no, obj.name, source, change_summary,
             nl_text, canonical(obj), h, llm_call_id, db.now_iso()))
    if slot_no is not None and set_current:
        db.ex("UPDATE slots SET current_version_id=?, updated_at=? WHERE slot_no=?", (vid, db.now_iso(), slot_no))
    return get_version(vid)


def set_current(slot_no: int, vid: int) -> None:
    db.ex("UPDATE slots SET current_version_id=?, updated_at=? WHERE slot_no=?", (vid, db.now_iso(), slot_no))


def copy_to_slot(vid: int, target_slot: int) -> dict:
    """把一个已冻结版本复制到另一个策略位，创建新的谱系（来源 copy），不影响原策略位。"""
    v = get_version(vid)
    if v is None:
        raise KeyError(vid)
    nv = freeze(target_slot, v["schema"], nl_text=v["nl_text"], source="copy",
                change_summary=f"复制自 {label(v)}", parent_id=None, new_lineage=True)
    db.ex("UPDATE strategy_versions SET parent_id=? WHERE id=? AND parent_id IS NULL AND id<>?", (vid, nv["id"], vid))
    save_draft(target_slot, text=v["nl_text"], draft_schema=v["schema"])
    return get_version(nv["id"])


def label(v: dict | None) -> str:
    if not v:
        return "--"
    return f"{v['name']} v{v['version_no']}"


def summarize_diff(a: dict, b: dict) -> str:
    d = diff(a, b)
    if not d:
        return "无变化"
    return "；".join(f"{k}: {x} → {y}" for k, x, y in d)[:500]


def diff(a: dict, b: dict) -> list[tuple[str, str, str]]:
    oa, _ = validate(a)
    ob, _ = validate(b)
    if not (oa and ob):
        return [("schema", canonical(a)[:80], canonical(b)[:80])]
    da, dbb = describe(oa), describe(ob)
    return [(k, da[k], dbb[k]) for k in da if da[k] != dbb.get(k)]
