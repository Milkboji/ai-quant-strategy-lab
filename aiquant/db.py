"""SQLite 持久化层。所有研究记录只追加，不覆盖已完成记录。"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import config

_lock = threading.RLock()

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS datasets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL, source TEXT NOT NULL, venue TEXT NOT NULL,
    timeframe_min INTEGER NOT NULL, start_utc TEXT, end_utc TEXT, tz TEXT NOT NULL DEFAULT 'UTC',
    fetched_at TEXT NOT NULL, rows INTEGER NOT NULL, sha256 TEXT NOT NULL,
    path TEXT NOT NULL, quality_json TEXT NOT NULL, is_test INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS slots (
    slot_no INTEGER PRIMARY KEY,
    draft_text TEXT NOT NULL DEFAULT '',
    draft_fields_json TEXT NOT NULL DEFAULT '{}',
    timeframe_mode TEXT NOT NULL DEFAULT 'follow',
    aux_timeframe_min INTEGER,
    current_version_id INTEGER,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lineage_id TEXT NOT NULL, version_no INTEGER NOT NULL,
    parent_id INTEGER, slot_no INTEGER,
    name TEXT NOT NULL, source TEXT NOT NULL, change_summary TEXT NOT NULL DEFAULT '',
    nl_text TEXT NOT NULL DEFAULT '',
    schema_json TEXT NOT NULL, schema_hash TEXT NOT NULL,
    llm_call_id INTEGER,
    created_at TEXT NOT NULL, frozen INTEGER NOT NULL DEFAULT 1,
    UNIQUE(lineage_id, version_no)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL, slot_no INTEGER, version_id INTEGER,
    params_json TEXT NOT NULL, dedupe_key TEXT NOT NULL,
    status TEXT NOT NULL, stage TEXT NOT NULL DEFAULT '', error TEXT,
    result_id INTEGER, batch_id TEXT, is_test INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_jobs_dedupe ON jobs(dedupe_key, status);
CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER, version_id INTEGER NOT NULL, slot_no INTEGER,
    dataset_id INTEGER NOT NULL, dataset_hash TEXT NOT NULL,
    window_start TEXT NOT NULL, window_end TEXT NOT NULL,
    signal_tf INTEGER NOT NULL, aux_tf INTEGER,
    split_label TEXT NOT NULL, assumptions_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL, equity_json TEXT NOT NULL,
    created_at TEXT NOT NULL, is_test INTEGER NOT NULL DEFAULT 0,
    cost_basis TEXT
);
CREATE TABLE IF NOT EXISTS benchmarks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_id INTEGER NOT NULL, dataset_hash TEXT NOT NULL,
    window_start TEXT NOT NULL, window_end TEXT NOT NULL, cost_key TEXT NOT NULL,
    metrics_json TEXT NOT NULL, equity_json TEXT NOT NULL, created_at TEXT NOT NULL,
    is_test INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_id INTEGER NOT NULL,
    entry_signal_ts TEXT, entry_fill_ts TEXT, entry_price REAL,
    exit_signal_ts TEXT, exit_fill_ts TEXT, exit_price REAL,
    qty REAL, fee REAL, spread_cost REAL, slippage_cost REAL,
    exit_reason TEXT, pnl REAL, equity_after REAL
);
CREATE INDEX IF NOT EXISTS ix_trades_bt ON trades(backtest_id);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL, slot_no INTEGER, version_id INTEGER, backtest_id INTEGER,
    model TEXT NOT NULL, dedupe_key TEXT NOT NULL,
    request_path TEXT, raw_response_path TEXT,
    status TEXT NOT NULL, parsed_json TEXT, error TEXT,
    created_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS holdout_touches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL, split TEXT NOT NULL,
    window_start TEXT NOT NULL, window_end TEXT NOT NULL,
    backtest_id INTEGER, touched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL, config_json TEXT NOT NULL,
    broker TEXT NOT NULL, status TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, is_test INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS paper_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_id INTEGER NOT NULL, client_order_id TEXT NOT NULL UNIQUE,
    broker TEXT NOT NULL, side TEXT NOT NULL, qty REAL NOT NULL,
    status TEXT NOT NULL, filled_qty REAL NOT NULL DEFAULT 0, filled_price REAL,
    signal_ts TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    is_test INTEGER NOT NULL DEFAULT 0, raw_json TEXT
);
CREATE TABLE IF NOT EXISTS paper_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deployment_id INTEGER, ts TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL,
    is_test INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS endpoint_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, method TEXT NOT NULL, url TEXT NOT NULL, status INTEGER
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_conn_cache: dict[str, sqlite3.Connection] = {}


def _connect(path: Path | None = None) -> sqlite3.Connection:
    # 所有数据库访问都在全局 _lock 内串行执行，因此每个数据库文件只用一个共享连接。
    # （旧版本按线程缓存连接：Streamlit 每次页面刷新都换新线程，连接只增不减，长时间运行会耗尽内存。）
    p = str(path or config.DB_PATH)
    key = p
    c = _conn_cache.get(key)
    if c is None:
        c = sqlite3.connect(p, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        _conn_cache[key] = c
    return c


def init_db() -> None:
    with _lock:
        c = _connect()
        c.executescript(SCHEMA)
        # 成本口径列（2026-10-30）：之前的回测为空 = 固定点差代理（旧口径），不重算、不覆盖
        if "cost_basis" not in {r[1] for r in c.execute("PRAGMA table_info(backtests)")}:
            c.execute("ALTER TABLE backtests ADD COLUMN cost_basis TEXT")
        for i in range(1, 6):
            c.execute(
                "INSERT OR IGNORE INTO slots(slot_no, updated_at) VALUES(?, ?)", (i, now_iso())
            )
        c.commit()


@contextmanager
def tx():
    with _lock:
        c = _connect()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def q(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with _lock:
        rows = _connect().execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]


def q1(sql: str, params: Iterable[Any] = ()) -> dict | None:
    r = q(sql, params)
    return r[0] if r else None


def ex(sql: str, params: Iterable[Any] = ()) -> int:
    with tx() as c:
        cur = c.execute(sql, tuple(params))
        return cur.lastrowid


def get_setting(key: str, default: Any = None) -> Any:
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(r["value"]) if r else default


def set_setting(key: str, value: Any) -> None:
    ex(
        "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, json.dumps(value, ensure_ascii=False), now_iso()),
    )


def audit_endpoint(method: str, url: str, status: int | None) -> None:
    try:
        ex("INSERT INTO endpoint_audit(ts,method,url,status) VALUES(?,?,?,?)",
           (now_iso(), method, url, status))
    except Exception:
        pass
