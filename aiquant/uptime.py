"""服务运行记录：中断检测、恢复后补检查、健康检查文件。

- 后台循环每轮（约 60 秒）调用 `beat(now)`：
  距上一次心跳超过 GAP_MIN 分钟 → 记一次中断（service_outages 表：开始 / 结束 / 时长 / 错过的检查），
  在事件流写明中断时长和错过了哪些检查，并标记「恢复后补检查」（下一轮 tick 会立即对全部策略检查一次）。
  BTC 入场信号仍遵守 15 分钟过期规则，不补单；只在事件里列出中断期间出现过的入场信号。
- 健康检查文件 `static/health.json`：{"status": "ok", "ts": 秒级时间戳}，只有状态和时间，不含任何账户 / 策略数据。
  Streamlit 以 /app/static/health.json 提供；外部心跳监控（Cloudflare Worker quant-guard 定时任务）每 5 分钟读取。
- `full_run_day(d)`：美股交易日 d 是否「服务完整运行」（开盘前 10 分钟到收盘后 30 分钟之间没有中断），
  美股节奏影子模式转正只计这样的交易日。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from . import config, db

log = logging.getLogger("aiquant.uptime")
GAP_MIN = 3.0                     # 两次心跳间隔超过 3 分钟算中断（正常 60 秒一轮）
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS service_outages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, start TEXT NOT NULL, end TEXT NOT NULL, minutes REAL NOT NULL,
  source TEXT NOT NULL, missed_json TEXT, created_at TEXT NOT NULL);
"""


def ensure_tables() -> None:
    with db.tx() as c:
        c.executescript(SCHEMA_SQL)


def health_path() -> Path:
    return config.ROOT / "static" / "health.json"


def write_health(now: datetime | None = None) -> None:
    """只写状态和时间戳（不暴露任何数据）。先写临时文件再替换，避免读到半个文件。"""
    p = health_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"status": "ok", "ts": int((now or datetime.now(timezone.utc)).timestamp())}))
        os.replace(tmp, p)
    except OSError:
        log.warning("health file write failed", exc_info=True)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def beat(now: datetime | None = None) -> dict | None:
    """每轮循环开始时调用。返回本次检测到的中断（没有则 None）。"""
    ensure_tables()
    now = now or datetime.now(timezone.utc)
    last = db.get_setting("paper_loop_heartbeat")
    out = None
    if last:
        prev = datetime.fromisoformat(last)
        gap = (now - prev).total_seconds() / 60
        if gap > GAP_MIN:
            out = record(prev, now, "heartbeat")
    write_health(now)
    return out


def record(start: datetime, end: datetime, source: str) -> dict:
    """记录一次中断，写事件，标记恢复后补检查。"""
    missed = missed_checks(start, end)
    mins = (end - start).total_seconds() / 60
    db.ex("INSERT INTO service_outages(start,end,minutes,source,missed_json,created_at) VALUES(?,?,?,?,?,?)",
          (_iso(start), _iso(end), round(mins, 1), source, json.dumps(missed, ensure_ascii=False), db.now_iso()))
    db.set_setting("catchup_pending", {"start": _iso(start), "end": _iso(end), "minutes": round(mins, 1)})
    from .paper import runner as pr
    pr.event(None, "WARN", f"服务中断 {_dur(mins)}（{_bj(start)} → {_bj(end)} 北京时间）。错过的检查：{missed['text']}。"
                           "已立即对全部策略补做一次检查；BTC 入场信号仍按 15 分钟过期规则，不补单")
    try:
        from . import notify
        notify.event("watchdog_restart", f"服务中断 {_dur(mins)} 后恢复", f"{_bj(start)} → {_bj(end)}（北京时间）\n错过的检查："
                     f"{missed['text']}\n已立即补做一次全策略检查；BTC 入场信号不补单。", dedupe=f"outage-{_iso(start)}")
    except Exception:  # noqa: BLE001
        pass
    return {"start": _iso(start), "end": _iso(end), "minutes": round(mins, 1), "missed": missed}


def _bj(t: datetime) -> str:
    return t.astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")


def _dur(mins: float) -> str:
    h, m = divmod(int(round(mins)), 60)
    return f"{h} 小时 {m} 分钟" if h else f"{m} 分钟"


def missed_checks(start: datetime, end: datetime) -> dict:
    """中断期间本应发生的检查：BTC 各部署的 K 线收盘检查次数、美股开盘检查和收盘后检查的交易日。"""
    from . import market as mk
    from .paper import runner as pr
    btc = []
    for d in db.q("SELECT id FROM paper_deployments WHERE broker='alpaca_paper' AND is_test=0 "
                  "AND status IN ('running','paused_entries') ORDER BY id"):
        dep = pr.get_deployment(d["id"])
        tf = int(dep["config"].get("signal_tf") or 15)
        n = _bar_closes(start, end, tf)
        if n:
            btc.append({"deployment": dep["id"], "label": dep["config"].get("label") or f"#{dep['id']}", "tf": tf, "bars": n})
    opens, closes = [], []
    d0, d1 = start.astimezone(mk.ET).date(), end.astimezone(mk.ET).date()
    for day in mk.trading_days(d0, d1):
        if start < mk.session_open(day) + timedelta(minutes=5) < end:
            opens.append(str(day))
        if start < mk.session_close(day) + timedelta(minutes=15) < end:
            closes.append(str(day))
    us_deps = db.q1("SELECT COUNT(*) AS n FROM paper_deployments WHERE broker='alpaca_us' AND is_test=0 "
                    "AND status IN ('running','paused_entries')")["n"]
    parts = []
    if btc:
        parts.append("BTC " + "、".join(f"#{b['deployment']} {b['bars']} 次（{b['tf']} 分钟 K 线）" for b in btc))
    if us_deps and opens:
        today = end.astimezone(mk.ET).date()
        late = str(today) in opens and end < mk.session_close(today)
        parts.append(f"美股开盘检查 {len(opens)} 个交易日（{', '.join(opens)}）"
                     + (f"，其中 {today} 仍在交易时段内，恢复后立即补做" if late else ""))
    if us_deps and closes:
        parts.append(f"美股收盘后检查 {len(closes)} 个交易日（{', '.join(closes)}）")
    return {"btc": btc, "us_open": opens if us_deps else [], "us_close": closes if us_deps else [],
            "text": "；".join(parts) or "没有错过定时检查"}


def _bar_closes(start: datetime, end: datetime, tf: int) -> int:
    """(start, end] 之间收盘的 tf 分钟 K 线数量（UTC 边界对齐；日线按 UTC 0 点）。"""
    step = tf * 60
    a = int(start.timestamp()) // step
    b = int(end.timestamp()) // step
    return max(0, b - a)


def catchup_signals(df5: pd.DataFrame) -> list[str]:
    """恢复后第一次 tick：列出中断期间各 BTC 部署出现过的入场信号（已过期，不补单）。只执行一次。"""
    cp = db.get_setting("catchup_pending")
    if not cp:
        return []
    db.set_setting("catchup_pending", None)
    from .engine.signals import build_frame, group_signal
    from .paper import runner as pr
    from .strategy import registry
    from .strategy.schema import validate
    s, e = pd.Timestamp(cp["start"]), pd.Timestamp(cp["end"])
    out = []
    for d in db.q("SELECT id FROM paper_deployments WHERE broker='alpaca_paper' AND is_test=0 "
                  "AND status IN ('running','paused_entries') ORDER BY id"):
        dep = pr.get_deployment(d["id"])
        try:
            obj, errs = validate(registry.get_version(dep["version_id"])["schema"])
            if errs:
                continue
            tf = int(dep["config"].get("signal_tf") or obj.signal_tf or 15)
            fr = build_frame(df5, obj, tf, dep["config"].get("aux_tf") or obj.aux_tf, data_end=e)
            ent = group_signal(fr, obj.entry) & ~group_signal(fr, obj.exit)
            ct = pd.to_datetime(fr["close_time"], utc=True)
            hits = [t for t, x in zip(ct, ent) if x and s < t <= e]
        except Exception:  # noqa: BLE001
            log.warning("catchup signals failed for #%s", d["id"], exc_info=True)
            continue
        if hits:
            msg = (f"中断期间出现 {len(hits)} 次入场信号（" + "、".join(_bj(t.to_pydatetime()) for t in hits[:6])
                   + ("…" if len(hits) > 6 else "") + " 北京时间），已超过 15 分钟，不补单")
            pr.event(dep["id"], "WARN", msg)
            out.append(f"#{dep['id']} {msg}")
    pr.event(None, "INFO", "恢复后补检查完成：" + ("；".join(out) if out else "中断期间各 BTC 策略没有入场信号"))
    return out


def outages(since: str | None = None) -> list[dict]:
    ensure_tables()
    if since:
        return db.q("SELECT * FROM service_outages WHERE end>=? ORDER BY start", (since,))
    return db.q("SELECT * FROM service_outages ORDER BY start")


def full_run_day(day, rows: list[dict] | None = None) -> bool:
    """美股交易日 day 服务是否完整运行：开盘前 10 分钟到收盘后 30 分钟之间没有记录到中断。"""
    from . import market as mk
    a = mk.session_open(day) - timedelta(minutes=10)
    b = mk.session_close(day) + timedelta(minutes=30)
    for r in (rows if rows is not None else outages()):
        s, e = datetime.fromisoformat(r["start"]), datetime.fromisoformat(r["end"])
        if s < b and e > a:
            return False
    return True


def backfill_from_snapshots(since: str = "2026-10-20T00:00:00+00:00") -> int:
    """一次性：用每小时账户快照的空档补记上线前的中断（相邻快照间隔超过 2 小时），供影子模式计数使用。"""
    ensure_tables()
    if db.get_setting("outage_backfill_done"):
        return 0
    n = 0
    try:
        rows = db.q("SELECT ts FROM account_snapshots WHERE ts>=? ORDER BY ts", (since,))
    except Exception:  # noqa: BLE001
        rows = []
    prev = None
    for r in rows:
        t = datetime.fromisoformat(r["ts"])
        if prev and (t - prev) > timedelta(hours=2):
            s = prev                     # 保守：从最后一次确认在运行的快照算起
            if not db.q1("SELECT id FROM service_outages WHERE start<=? AND end>=?", (_iso(t), _iso(s))):
                db.ex("INSERT INTO service_outages(start,end,minutes,source,missed_json,created_at) VALUES(?,?,?,?,?,?)",
                      (_iso(s), _iso(t), round((t - s).total_seconds() / 60, 1), "snapshot_gap", None, db.now_iso()))
                n += 1
        prev = t
    db.set_setting("outage_backfill_done", {"at": db.now_iso(), "n": n})
    return n
