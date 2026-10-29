"""留出数据（验证集）计次：所有市场、所有谱系共用一张 holdout_touches 表、一套计数规则。

- 每条谱系（策略位 / 实时预设的 lineage_id；美股 ETF 择时为 "etf:信号->交易"，如 "etf:QQQ->QLD"）最多查看验证集 3 次。
- 同一次检验（相同窗口 + 相同回测 / 相同时间）只计一次。
- 2026-10-29 起 ETF 进化不再有单独的 2025-01-01 边界和单独的 split='etf_validation' 记录：迁移时并入 split='validation'
  并写上 lineage_key，合并前后的次数记在 settings.holdout_merge_report。
- 「已暴露」：某条谱系的验证 / 测试数据此前已被研究或全期预览看过（settings.lineage_exposure）。这条谱系的验证检验只作参考、
  不能作为晋级依据；改用 forward_from（上线日）之后的前向数据做留出检验。
"""
from __future__ import annotations

from . import db

LIMIT = 3
ETF_DEFAULT_EXPOSURE = {
    "etf:QQQ->QLD": {
        "exposed": ["validation", "test"],
        "reason": "上线前的研究（2026-10-21，挑选期 / 验证 / 最终检验写在 etf_trend 说明里）、策略实验室的全期预览、"
                  "2026-10-29 的美股节奏研究都看过 2025 年以后的数据",
        "forward_from": "2026-10-21",
    },
}


def ensure() -> None:
    cols = {r["name"] for r in db.q("PRAGMA table_info(holdout_touches)")}
    if "lineage_key" not in cols:
        db.ex("ALTER TABLE holdout_touches ADD COLUMN lineage_key TEXT")


def etf_lineage(signal_symbol: str, trade_symbol: str) -> str:
    return f"etf:{signal_symbol}->{trade_symbol}"


def _rows(lineage: str) -> list[dict]:
    ensure()
    return db.q("SELECT h.* FROM holdout_touches h LEFT JOIN strategy_versions v ON v.id=h.version_id "
                "WHERE h.split='validation' AND (h.lineage_key=? OR (h.lineage_key IS NULL AND v.lineage_id=?))",
                (lineage, lineage))


def _key(r: dict) -> tuple:
    return (r["window_start"], r["window_end"], r["backtest_id"] if r["backtest_id"] is not None else r["touched_at"][:16])


def used(lineage: str) -> int:
    """这条谱系已用的验证次数（每条记录 = 一次检验；两套旧记录里的同一次检验在合并时已去重）。"""
    return len(_rows(lineage))


def record(lineage: str, version_id: int, start: str, end: str, backtest_id: int | None = None, conn=None) -> None:
    ensure()
    args = (version_id, "validation", start, end, backtest_id, db.now_iso(), lineage)
    sql = ("INSERT INTO holdout_touches(version_id,split,window_start,window_end,backtest_id,touched_at,lineage_key)"
           " VALUES(?,?,?,?,?,?,?)")
    if conn is not None:
        conn.execute(sql, args)
    else:
        db.ex(sql, args)


def exposure(lineage: str) -> dict | None:
    m = db.get_setting("lineage_exposure")
    if m is None:
        m = dict(ETF_DEFAULT_EXPOSURE)
        db.set_setting("lineage_exposure", m)
    return m.get(lineage)


def merge_etf_counts(etf_key: str) -> dict:
    """一次性迁移：split='etf_validation' 并入 'validation'（lineage_key=etf_key），去重；返回合并前后的次数。"""
    ensure()
    done = db.get_setting("holdout_merge_report")
    if done:
        return done
    old = db.q("SELECT * FROM holdout_touches WHERE split='etf_validation'")
    before_new = used(etf_key)
    before_old = len({_key(r) for r in old})
    have = {_key(r) for r in _rows(etf_key)}
    moved = 0
    for r in old:
        if _key(r) in have:
            db.ex("DELETE FROM holdout_touches WHERE id=?", (r["id"],))
        else:
            db.ex("UPDATE holdout_touches SET split='validation', lineage_key=? WHERE id=?", (etf_key, r["id"]))
            have.add(_key(r))
            moved += 1
    rep = {"lineage": etf_key, "etf_validation_before": before_old, "validation_before": before_new,
           "after": used(etf_key), "moved": moved, "at": db.now_iso()}
    db.set_setting("holdout_merge_report", rep)
    return rep


def migrate() -> dict:
    """启动时执行（幂等）：建列、合并 ETF 计次、写入已暴露标注、给旧分区下登记的 ETF 挑战者加注释。"""
    import json
    ensure()
    try:
        from .challenger import ensure_tables, etf_lineage
        ensure_tables()
        lin = etf_lineage()
    except Exception:  # noqa: BLE001
        lin = "etf:QQQ->QLD"
    rep = merge_etf_counts(lin)
    exposure(lin)
    for ch in db.q("SELECT id, train_json FROM evolve_challengers WHERE kind='etf' AND status IN ('running','awaiting')"):
        tj = json.loads(ch["train_json"] or "{}")
        if "split_version" in tj or "split_note" in tj:
            continue
        tj["split_version"] = 0
        tj["split_note"] = ("登记时用的是旧分区：参数在 2016-2024 上挑选，跨市场快速检验覆盖到 2026 年，当时没有做留出检验；"
                            "前向测试照常进行，不中断")
        db.ex("UPDATE evolve_challengers SET train_json=? WHERE id=?", (json.dumps(tj, ensure_ascii=False), ch["id"]))
    return rep
