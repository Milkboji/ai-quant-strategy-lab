"""UI 调用的业务动作（统一入口）。"""
from __future__ import annotations

import io
import json
import zipfile

import pandas as pd

from . import config, db, jobs, research
from .ai import optimizer
from .data import market_data as md
from .strategy import registry
from .strategy.templates import DEFAULT_SLOT_TEMPLATES, TEMPLATES

_initialized = False


def init() -> None:
    global _initialized
    if _initialized:
        return
    db.init_db()
    try:   # 记录真实服务进程号，供守护进程监控内存（Windows 虚拟环境的 python.exe 只是启动器）
        import os
        (config.LOG_DIR / "app.pid").write_text(str(os.getpid()))
    except Exception:  # noqa: BLE001
        pass
    research.get_split()
    jobs.register("backtest", research.execute_backtest)
    jobs.register("final_test", research.execute_backtest)
    jobs.register("ai_analyze", optimizer.run_analyze)
    jobs.register("ai_batch", optimizer.run_batch)
    jobs.register("data_update", _data_update)
    from . import evolve
    evolve.ensure_tables()
    jobs.register("evolve", evolve.run)
    # 首次使用：草稿填入示例模板文字（不产生任何版本或结果）
    for slot in registry.all_slots():
        if not slot["draft_text"] and not slot["current_version_id"]:
            t = TEMPLATES[DEFAULT_SLOT_TEMPLATES[slot["slot_no"]]]
            registry.save_draft(slot["slot_no"], text=t["text"])
    jobs.ensure_worker()
    from .paper import service as paper_service
    paper_service.ensure_loop()
    from .live import sim as live_sim
    from .live.feed import FEED
    live_sim.ensure_tables()
    FEED.start()
    _initialized = True


def _data_update(job, stage, should_stop) -> int:
    p = json.loads(job["params_json"])
    r = md.update_from_alpaca(md.to_utc(p["start"]), md.to_utc(p["end"]), progress=stage)
    stage(f"完成：下载 {r['downloaded_rows']} 根")
    return r["downloaded_rows"]


def submit_data_update(start, end) -> tuple[int, bool]:
    key = f"data:{md.to_utc(start).isoformat()}:{md.to_utc(end).isoformat()}"
    return jobs.submit("data_update", slot_no=None, version_id=None,
                       params={"start": md.to_utc(start).isoformat(), "end": md.to_utc(end).isoformat()},
                       dedupe_key=key)


# ---------------------------------------------------------------------------
# 回测
# ---------------------------------------------------------------------------

def submit_backtest(slot_no: int, *, batch_id: str | None = None, allow_test_touch: bool = False,
                    kind: str = "backtest", version_id: int | None = None) -> tuple[int | None, str]:
    slot = registry.get_slot(slot_no)
    vid = version_id or slot["current_version_id"]
    v = registry.get_version(vid)
    if v is None:
        return None, "请先『解析策略』并『保存版本』（冻结）后再回测"
    r = research.get_research()
    try:
        tf, aux = research.effective_tf(slot, v["schema"], r)
    except ValueError as e:
        return None, str(e)
    if kind == "final_test":
        st = research.test_set_status()
        if not st["ok"]:
            return None, st["label"]
        done = db.q1("SELECT h.id FROM holdout_touches h JOIN backtests b ON b.id=h.backtest_id "
                     "WHERE h.version_id=? AND h.split='final_test' AND b.is_test=0", (vid,))
        if done:
            return None, "该版本已运行过独立最终测试，不可重复"
        sp = research.get_split()
        start, end = md.to_utc(sp["test_start"]), md.to_utc(sp["test_end"])
    elif kind == "validation":
        sp = research.get_split()
        start, end = md.to_utc(sp["val_start"]), md.to_utc(sp["test_start"])
        kind = "backtest"
        used = db.q1("SELECT COUNT(*) AS n FROM holdout_touches h JOIN strategy_versions v ON v.id=h.version_id "
                     "WHERE h.split='validation' AND v.lineage_id=?", (v["lineage_id"],))["n"]
        pending = db.q1("SELECT COUNT(*) AS n FROM jobs j JOIN strategy_versions v ON v.id=j.version_id "
                        "WHERE j.status IN ('queued','running') AND v.lineage_id=? AND j.params_json LIKE ?",
                        (v["lineage_id"], f'%"start": "{start.isoformat()}"%'))["n"]
        if used + pending >= research.VALIDATION_RUN_LIMIT:
            return None, f"该策略谱系验证集已用 {used} 次（另有 {pending} 个进行中），达到上限 {research.VALIDATION_RUN_LIMIT}"
    else:
        start, end = research.resolve_window(r)
    src = r["source"]
    params = {"start": start.isoformat(), "end": end.isoformat(), "tf": tf, "aux": aux, "source": src,
              "research": r, "allow_test_touch": allow_test_touch}
    key = research.params_key(vid, tf, aux, start, end, r, src, kind)
    if kind == "backtest":
        prev = db.q1("SELECT * FROM jobs WHERE dedupe_key=? AND status='done' ORDER BY id DESC LIMIT 1", (key,))
        if prev and prev["result_id"] and research.get_backtest(prev["result_id"]):
            return prev["id"], "相同版本与参数已有完成结果，直接复用（不重复运行）"
    jid, created = jobs.submit(kind, slot_no=slot_no, version_id=vid, params=params, dedupe_key=key,
                               batch_id=batch_id, is_test=(src == "test"))
    return jid, "已提交回测任务" if created else "相同任务正在执行，未重复提交"


def submit_all() -> list[tuple[int, int | None, str]]:
    bid = jobs.new_batch_id()
    return [(i, *submit_backtest(i, batch_id=bid)) for i in range(1, 6)]


def stop_slot(slot_no: int) -> int:
    n = 0
    for j in db.q("SELECT id FROM jobs WHERE slot_no=? AND status IN ('queued','running')", (slot_no,)):
        jobs.cancel(j["id"]); n += 1
    return n


def submit_ai_analyze(slot_no: int, force: bool = False) -> tuple[int | None, str]:
    slot = registry.get_slot(slot_no)
    if not slot["current_version_id"]:
        return None, "该策略位没有冻结版本"
    try:
        _, v, b = optimizer.build_analyze_payload(slot_no)
    except ValueError as e:
        return None, str(e)
    jid, created = jobs.submit("ai_analyze", slot_no=slot_no, version_id=v["id"], params={"force": force},
                               dedupe_key=f"ai:{v['id']}:{b['id']}:{force}")
    return jid, "已提交 DeepSeek 分析（一次模型调用）" if created else "该分析正在进行，未重复提交"


def submit_ai_batch(force: bool = False) -> tuple[int | None, str]:
    try:
        optimizer.build_batch_payload()
    except ValueError as e:
        return None, str(e)
    sig = ",".join(str(registry.get_slot(i)["current_version_id"]) for i in range(1, 6))
    jid, created = jobs.submit("ai_batch", slot_no=None, version_id=None, params={"force": force},
                               dedupe_key=f"aibatch:{sig}:{force}")
    return jid, "已提交五策略横向分析（一次模型调用）" if created else "批量分析正在进行"


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------

def slot_status(slot_no: int) -> dict:
    j = jobs.latest_for_slot(slot_no, ("backtest", "final_test", "ai_analyze"))
    if j is None:
        return {"label": "未开始", "tone": "gray", "job": None}
    kind, st = j["kind"], j["status"]
    if st in ("queued", "running"):
        if kind == "ai_analyze":
            return {"label": "AI优化中", "tone": "purple", "job": j}
        if kind == "final_test":
            return {"label": "验证中", "tone": "blue", "job": j}
        return {"label": "等待执行" if st == "queued" else "回测中", "tone": "blue", "job": j}
    if st == "failed":
        return {"label": "失败", "tone": "red", "job": j}
    if st == "stopped":
        return {"label": "已停止", "tone": "orange", "job": j}
    return {"label": "已完成", "tone": "green", "job": j}


def slot_result(slot_no: int) -> dict | None:
    """策略位当前版本最近一次回测结果（含测试数据标记）。"""
    slot = registry.get_slot(slot_no)
    if not slot["current_version_id"]:
        return None
    return research.latest_backtest_for_version(slot["current_version_id"], include_test=True)


def leaderboard() -> pd.DataFrame:
    rows = []
    for i in range(1, 6):
        slot = registry.get_slot(i)
        v = registry.get_version(slot["current_version_id"])
        b = slot_result(i)
        st = slot_status(i)
        m = b["metrics"] if b else {}
        rows.append({
            "策略位": i, "策略名称": v["name"] if v else "--", "版本": f"v{v['version_no']}" if v else "--",
            "K线周期": research_tf_label(b["signal_tf"]) if b else "--", "状态": st["label"],
            "数据分区": (b["split_label"] + ("·测试数据" if b["is_test"] else "")) if b else "--",
            "净收益率%": m.get("net_return_pct"), "净利润": m.get("net_profit"),
            "最大回撤%": m.get("max_drawdown_pct"), "胜率%": m.get("win_rate_pct"),
            "Profit Factor": (None if m.get("profit_factor") in (None, "inf") else m.get("profit_factor")),
            "交易次数": m.get("trades"), "手续费": m.get("fees"), "滑点": m.get("slippage_cost"),
            "点差": m.get("spread_cost"), "_bt": b["id"] if b else None, "_window": (b["window_start"], b["window_end"]) if b else None,
        })
    df = pd.DataFrame(rows)
    return df


def research_tf_label(m: int | None) -> str:
    from .config import TIMEFRAMES
    for k, v in TIMEFRAMES.items():
        if v == m:
            return k
    return "--"


def export_zip() -> bytes:
    buf = io.BytesIO()
    lb = leaderboard()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("leaderboard.csv", lb.drop(columns=["_window"]).to_csv(index=False).encode("utf-8-sig"))
        for _, r in lb.iterrows():
            if pd.notna(r["_bt"]) and r["_bt"]:
                b = research.get_backtest(int(r["_bt"]))
                z.writestr(f"slot{r['策略位']}_backtest_{b['id']}.json",
                           json.dumps({k: b[k] for k in ("id", "version_id", "dataset_hash", "window_start",
                                                          "window_end", "signal_tf", "aux_tf", "split_label",
                                                          "metrics", "assumptions", "is_test")},
                                      ensure_ascii=False, indent=2))
                z.writestr(f"slot{r['策略位']}_trades_{b['id']}.csv",
                           research.get_trades(b["id"]).to_csv(index=False).encode("utf-8-sig"))
        vers = registry.all_versions()
        z.writestr("versions.json", json.dumps([{k: v[k] for k in ("id", "lineage_id", "version_no", "parent_id",
                                                                    "name", "source", "change_summary",
                                                                    "schema_hash", "created_at", "schema")}
                                                 for v in vers], ensure_ascii=False, indent=2))
    return buf.getvalue()


def validation_quota(lineage_id: str) -> dict:
    """本谱系验证集检验剩余次数（已用 + 进行中 都占名额）。"""
    sp = research.get_split()
    start = md.to_utc(sp["val_start"])
    used = research.validation_runs_used(lineage_id)
    pending = db.q1("SELECT COUNT(*) AS n FROM jobs j JOIN strategy_versions v ON v.id=j.version_id "
                    "WHERE j.status IN ('queued','running') AND v.lineage_id=? AND j.params_json LIKE ?",
                    (lineage_id, f'%"start": "{start.isoformat()}"%'))["n"]
    limit = research.VALIDATION_RUN_LIMIT
    return {"used": used, "pending": pending, "limit": limit, "remaining": max(0, limit - used - pending)}
