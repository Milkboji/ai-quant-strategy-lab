"""受控任务队列（单进程内后台线程，状态全部持久化在 SQLite）。

- 防重复：同一 dedupe_key 存在 queued/running 任务时直接返回该任务，页面刷新不会重复启动。
- 恢复：服务重启时把遗留的 running 任务标记为失败（"服务重启中断"），queued 任务继续执行。
- 并发：最多 MAX_WORKERS 个任务同时运行，避免五个重任务同时占满内存。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import traceback
import uuid

from . import config, db

log = logging.getLogger("aiquant.jobs")
MAX_WORKERS = 2
STATUS_CN = {"queued": "等待执行", "running": "回测中", "done": "已完成", "failed": "失败", "stopped": "已停止"}

_handlers: dict = {}
_started = False
_start_lock = threading.Lock()


def register(kind: str, fn) -> None:
    _handlers[kind] = fn


def submit(kind: str, *, slot_no: int | None, version_id: int | None, params: dict, dedupe_key: str,
           batch_id: str | None = None, is_test: bool = False) -> tuple[int, bool]:
    """返回 (job_id, created)。"""
    with db.tx() as c:
        ex = c.execute("SELECT id FROM jobs WHERE dedupe_key=? AND status IN ('queued','running') ORDER BY id DESC",
                       (dedupe_key,)).fetchone()
        if ex:
            return int(ex["id"]), False
        cur = c.execute(
            "INSERT INTO jobs(kind,slot_no,version_id,params_json,dedupe_key,status,stage,batch_id,is_test,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (kind, slot_no, version_id, json.dumps(params, ensure_ascii=False, default=str), dedupe_key, "queued",
             "排队中", batch_id, 1 if is_test else 0, db.now_iso()))
        jid = cur.lastrowid
    ensure_worker()
    return jid, True


def new_batch_id() -> str:
    return "B" + uuid.uuid4().hex[:10]


def cancel(job_id: int) -> None:
    with db.tx() as c:
        c.execute("UPDATE jobs SET status='stopped', stage='已停止（未开始）', finished_at=? "
                  "WHERE id=? AND status='queued'", (db.now_iso(), job_id))
        c.execute("UPDATE jobs SET cancel_requested=1 WHERE id=? AND status='running'", (job_id,))


def cancel_all() -> int:
    rows = db.q("SELECT id FROM jobs WHERE status IN ('queued','running')")
    for r in rows:
        cancel(r["id"])
    return len(rows)


def get(job_id: int | None) -> dict | None:
    if not job_id:
        return None
    return db.q1("SELECT * FROM jobs WHERE id=?", (job_id,))


def latest_for_slot(slot_no: int, kinds: tuple[str, ...] = ("backtest",)) -> dict | None:
    ph = ",".join("?" * len(kinds))
    return db.q1(f"SELECT * FROM jobs WHERE slot_no=? AND kind IN ({ph}) ORDER BY id DESC LIMIT 1",
                 (slot_no, *kinds))


def active_count() -> int:
    return db.q1("SELECT COUNT(*) AS n FROM jobs WHERE status IN ('queued','running')")["n"]


def _set(job_id: int, **kw) -> None:
    sets = ", ".join(f"{k}=?" for k in kw)
    db.ex(f"UPDATE jobs SET {sets} WHERE id=?", (*kw.values(), job_id))


def _run_one(job: dict) -> None:
    jid = job["id"]
    fn = _handlers.get(job["kind"])
    if fn is None:
        _set(jid, status="failed", error=f"未知任务类型 {job['kind']}", finished_at=db.now_iso())
        return

    def stage(msg: str) -> None:
        _set(jid, stage=msg[:200])

    def should_stop() -> bool:
        r = db.q1("SELECT cancel_requested FROM jobs WHERE id=?", (jid,))
        return bool(r and r["cancel_requested"])

    try:
        result_id = fn(job, stage, should_stop)
        # 结果一旦保存即视为完成（即使停止请求在保存之后才到达），保证记录可追溯
        _set(jid, status="done", stage="已完成", result_id=result_id, finished_at=db.now_iso())
    except Exception as e:  # noqa: BLE001
        from .engine.backtest import Cancelled
        if isinstance(e, Cancelled):
            _set(jid, status="stopped", stage="已停止", finished_at=db.now_iso())
        else:
            log.error("job %s failed: %s", jid, traceback.format_exc())
            _set(jid, status="failed", stage="失败", error=str(e)[:1000], finished_at=db.now_iso())


def _claim() -> dict | None:
    with db.tx() as c:
        running = c.execute("SELECT COUNT(*) AS n FROM jobs WHERE status='running'").fetchone()["n"]
        if running >= MAX_WORKERS:
            return None
        r = c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if not r:
            return None
        n = c.execute("UPDATE jobs SET status='running', stage='启动', started_at=? WHERE id=? AND status='queued'",
                      (db.now_iso(), r["id"])).rowcount
        if n != 1:
            return None
        return dict(r)


def _loop() -> None:
    while True:
        try:
            job = _claim()
            if job is None:
                time.sleep(0.5)
                continue
            threading.Thread(target=_run_one, args=(job,), daemon=True, name=f"job-{job['id']}").start()
        except Exception:  # noqa: BLE001
            log.error("worker loop error: %s", traceback.format_exc())
            time.sleep(2)


def recover_on_start() -> int:
    n = db.q1("SELECT COUNT(*) AS n FROM jobs WHERE status='running'")["n"]
    db.ex("UPDATE jobs SET status='failed', stage='失败', error='服务重启中断，可重新运行', finished_at=? "
          "WHERE status='running'", (db.now_iso(),))
    return n


def ensure_worker() -> None:
    global _started
    with _start_lock:
        if _started:
            return
        recover_on_start()
        threading.Thread(target=_loop, daemon=True, name="aiquant-worker").start()
        _started = True


def wait_all(timeout: float = 60) -> None:
    """测试辅助：等待所有任务结束。"""
    t0 = time.time()
    while active_count() and time.time() - t0 < timeout:
        time.sleep(0.2)
