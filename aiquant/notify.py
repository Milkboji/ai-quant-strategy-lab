"""通知模块：邮件 + Telegram。

- 账号配置只放在 config/secrets.local（或 .env），不进代码仓库：
    NOTIFY_SMTP_HOST=smtp.qq.com        NOTIFY_SMTP_PORT=465
    NOTIFY_SMTP_USER=xxx@qq.com         NOTIFY_SMTP_PASSWORD=邮箱授权码
    NOTIFY_EMAIL_TO=a@x.com,b@y.com     NOTIFY_EMAIL_FROM=（可选，默认同 USER）
    NOTIFY_TELEGRAM_BOT_TOKEN=123:abc   NOTIFY_TELEGRAM_CHAT_ID=123456
- 所有发送都走 aiquant/data/http.py 这个唯一出口：主机白名单 + 审计（token / 密码不写入审计）。
- 开关（哪些通道、哪些事件、每日摘要时间）存在 settings.notify_config，在「系统设置」页修改（仅管理员）。
- 先写入发件箱 notify_outbox，再尝试发送；网络断开时保留，后台循环每分钟重试（最多 5 次）。
- 测试数据（is_test）产生的事件不发通知。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from . import config, db

log = logging.getLogger("aiquant.notify")
BJ = timezone(timedelta(hours=8))
EVENTS = {
    "promotion": "策略晋级",
    "awaiting": "等待人工确认",
    "auto_pause": "长期体检自动暂停",
    "loss_streak": "连亏刹车",
    "watchdog_restart": "守护进程重启",
    "tunnel_down": "外网通道断开超过 60 秒",
    "daily_summary": "每日运行摘要",
}
CHANNELS = {"email": "邮件", "telegram": "Telegram"}
DEFAULTS = {"email": False, "telegram": False, "events": {k: True for k in EVENTS}, "daily_hour": 21}
MAX_ATTEMPTS = 5
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notify_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, event TEXT NOT NULL, channel TEXT NOT NULL,
  title TEXT, body TEXT, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
  sent_at TEXT, dedupe_key TEXT);
CREATE INDEX IF NOT EXISTS ix_notify_status ON notify_outbox(status);
"""


def ensure_tables() -> None:
    with db.tx() as c:
        c.executescript(SCHEMA_SQL)


def get_config() -> dict:
    c = json.loads(json.dumps(DEFAULTS))
    saved = db.get_setting("notify_config") or {}
    c.update({k: v for k, v in saved.items() if k != "events"})
    c["events"].update(saved.get("events") or {})
    return c


def set_config(*, actor: str | None, role: str | None, email: bool | None = None, telegram: bool | None = None,
               events: dict | None = None, daily_hour: int | None = None) -> dict:
    if role != "admin":
        raise PermissionError("只有管理员可以修改通知设置")
    c = get_config()
    if email is not None:
        c["email"] = bool(email)
    if telegram is not None:
        c["telegram"] = bool(telegram)
    if events:
        c["events"].update({k: bool(v) for k, v in events.items() if k in EVENTS})
    if daily_hour is not None:
        if not 0 <= int(daily_hour) <= 23:
            raise ValueError("每日摘要时间需在 0~23 点之间")
        c["daily_hour"] = int(daily_hour)
    db.set_setting("notify_config", c)
    try:
        from . import auth
        auth.audit(actor or "", "修改通知设置", "notify",
                   f"邮件 {'开' if c['email'] else '关'}，Telegram {'开' if c['telegram'] else '关'}，"
                   f"事件 {sum(c['events'].values())}/{len(EVENTS)}，摘要 {c['daily_hour']}:00")
    except Exception:  # noqa: BLE001
        log.exception("notify settings audit failed")
    return c


# ---------------------------------------------------------------------------
# 通道
# ---------------------------------------------------------------------------

def _email_cfg() -> dict | None:
    host, user, pw, to = (config.secret(k) for k in ("NOTIFY_SMTP_HOST", "NOTIFY_SMTP_USER", "NOTIFY_SMTP_PASSWORD",
                                                     "NOTIFY_EMAIL_TO"))
    if not (host and user and pw and to):
        return None
    return {"host": host, "port": int(config.secret("NOTIFY_SMTP_PORT") or 465), "user": user, "password": pw,
            "sender": config.secret("NOTIFY_EMAIL_FROM") or user,
            "to": [x.strip() for x in to.replace(";", ",").split(",") if x.strip()]}


def _tg_cfg() -> dict | None:
    tok, chat = config.secret("NOTIFY_TELEGRAM_BOT_TOKEN"), config.secret("NOTIFY_TELEGRAM_CHAT_ID")
    return {"token": tok, "chat_id": chat} if tok and chat else None


def channel_status() -> dict:
    """{channel: {"configured": bool, "enabled": bool, "detail": 打码后的配置}}"""
    c = get_config()
    e, t = _email_cfg(), _tg_cfg()
    return {
        "email": {"configured": bool(e), "enabled": bool(c["email"]),
                  "detail": (f"{e['host']}:{e['port']} · {config.mask(e['user'])} → {len(e['to'])} 个收件人" if e
                             else "未配置（需要 NOTIFY_SMTP_HOST / NOTIFY_SMTP_USER / NOTIFY_SMTP_PASSWORD / NOTIFY_EMAIL_TO）")},
        "telegram": {"configured": bool(t), "enabled": bool(c["telegram"]),
                     "detail": (f"机器人 {config.mask(t['token'])} · 会话 {config.mask(t['chat_id'])}" if t
                                else "未配置（需要 NOTIFY_TELEGRAM_BOT_TOKEN / NOTIFY_TELEGRAM_CHAT_ID）")},
    }


def _send(channel: str, title: str, body: str) -> None:
    from .data import http
    if channel == "email":
        e = _email_cfg()
        if not e:
            raise RuntimeError("邮件未配置")
        http.smtp_send(host=e["host"], port=e["port"], user=e["user"], password=e["password"], sender=e["sender"],
                       to=e["to"], subject=f"[AI Quant] {title}", body=body)
        return
    if channel == "telegram":
        t = _tg_cfg()
        if not t:
            raise RuntimeError("Telegram 未配置")
        r = http.request("POST", f"https://api.telegram.org/bot{t['token']}/sendMessage",
                         json_body={"chat_id": t["chat_id"], "text": f"【AI Quant】{title}\n{body}"[:4000],
                                    "disable_web_page_preview": True}, timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"Telegram 返回 {r.status_code}")
        return
    raise ValueError(f"未知通道 {channel}")


# ---------------------------------------------------------------------------
# 事件 → 发件箱 → 发送
# ---------------------------------------------------------------------------

def event(kind: str, title: str, body: str = "", dedupe: str | None = None) -> int:
    """登记一条通知事件。返回写入发件箱的条数（事件关闭或无可用通道时为 0，但仍留一条“跳过”记录便于查看）。"""
    if kind not in EVENTS:
        raise ValueError(f"未知事件 {kind}")
    ensure_tables()
    c = get_config()
    if dedupe and db.q1("SELECT id FROM notify_outbox WHERE dedupe_key=? AND ts>=?",
                        (dedupe, (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat())):
        return 0
    now = db.now_iso()
    if not c["events"].get(kind, True):
        return 0
    st = channel_status()
    chans = [ch for ch, s in st.items() if s["enabled"] and s["configured"]]
    if not chans:
        db.ex("INSERT INTO notify_outbox(ts,event,channel,title,body,status,dedupe_key) VALUES(?,?,?,?,?,?,?)",
              (now, kind, "none", title[:200], body[:4000], "skipped", dedupe))
        return 0
    for ch in chans:
        db.ex("INSERT INTO notify_outbox(ts,event,channel,title,body,status,dedupe_key) VALUES(?,?,?,?,?,?,?)",
              (now, kind, ch, title[:200], body[:4000], "pending", dedupe))
    try:
        flush()
    except Exception:  # noqa: BLE001
        log.exception("notify flush failed")
    return len(chans)


def flush(limit: int = 20) -> int:
    """发送发件箱里待发的通知；失败的保留重试（最多 5 次）。返回成功条数。"""
    ensure_tables()
    ok = 0
    for r in db.q("SELECT * FROM notify_outbox WHERE status='pending' ORDER BY id LIMIT ?", (limit,)):
        try:
            _send(r["channel"], r["title"] or "", r["body"] or "")
            db.ex("UPDATE notify_outbox SET status='sent', attempts=attempts+1, sent_at=?, last_error=NULL WHERE id=?",
                  (db.now_iso(), r["id"]))
            ok += 1
        except Exception as e:  # noqa: BLE001
            n = int(r["attempts"]) + 1
            db.ex("UPDATE notify_outbox SET status=?, attempts=?, last_error=? WHERE id=?",
                  ("failed" if n >= MAX_ATTEMPTS else "pending", n, f"{type(e).__name__}: {e}"[:300], r["id"]))
    return ok


def send_test(channel: str, *, actor: str | None, role: str | None) -> str:
    if role != "admin":
        raise PermissionError("只有管理员可以发送测试通知")
    ensure_tables()
    title, body = "测试通知", f"这是一条来自 AI Quant Strategy Lab 的测试消息（{actor or '管理员'}，{datetime.now(BJ):%m-%d %H:%M} 北京时间）。"
    rid = db.ex("INSERT INTO notify_outbox(ts,event,channel,title,body,status) VALUES(?,?,?,?,?,?)",
                (db.now_iso(), "test", channel, title, body, "pending"))
    try:
        _send(channel, title, body)
        db.ex("UPDATE notify_outbox SET status='sent', attempts=1, sent_at=? WHERE id=?", (db.now_iso(), rid))
        return "已发送"
    except Exception as e:  # noqa: BLE001
        db.ex("UPDATE notify_outbox SET status='failed', attempts=1, last_error=? WHERE id=?",
              (f"{type(e).__name__}: {e}"[:300], rid))
        raise


def recent(limit: int = 50) -> list[dict]:
    ensure_tables()
    return db.q("SELECT id,ts,event,channel,title,status,attempts,last_error,sent_at FROM notify_outbox "
                "ORDER BY id DESC LIMIT ?", (limit,))


# ---------------------------------------------------------------------------
# 每日运行摘要
# ---------------------------------------------------------------------------

def daily_summary_text(now: datetime | None = None) -> tuple[str, str]:
    now = now or datetime.now(timezone.utc)
    bj = now.astimezone(BJ)
    day0 = bj.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
    lines = []
    try:
        snaps = db.q("SELECT ts, equity FROM account_snapshots WHERE ts>=? ORDER BY ts", (day0,))
        last = db.q1("SELECT ts, equity FROM account_snapshots ORDER BY ts DESC LIMIT 1")
        if last:
            lines.append(f"模拟账户总资产：{last['equity']:,.2f} USD"
                         + (f"（今日 {last['equity'] - snaps[0]['equity']:+,.2f}）" if snaps else ""))
    except Exception:  # noqa: BLE001
        pass
    deps = db.q("SELECT status FROM paper_deployments WHERE broker='alpaca_paper' AND is_test=0 "
                "AND status IN ('running','paused_entries')")
    lines.append(f"策略：运行 {sum(1 for d in deps if d['status'] == 'running')} 个，"
                 f"暂停新开仓 {sum(1 for d in deps if d['status'] == 'paused_entries')} 个")
    o = db.q1("SELECT COUNT(*) AS n FROM paper_orders WHERE status='filled' AND is_test=0 AND created_at>=?", (day0,))
    lines.append(f"今日成交：{o['n'] if o else 0} 笔")
    try:
        from . import evolog
        evs = [r for r in evolog.rows(200) if r["ts"] >= day0]
        if evs:
            lines.append("今日进化：" + "，".join(f"{e} {sum(1 for r in evs if r['event'] == e)} 次"
                                              for e in ("晋级", "淘汰", "拒绝") if any(r["event"] == e for r in evs)))
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import console
        n = len(console.awaiting())
        if n:
            lines.append(f"待你确认的晋级：{n} 个（策略总台）")
    except Exception:  # noqa: BLE001
        pass
    hb = db.get_setting("paper_loop_heartbeat")
    if hb:
        lines.append(f"自动交易循环心跳：{(datetime.fromisoformat(hb).astimezone(BJ)):%m-%d %H:%M} 北京时间")
    st = db.get_setting("etf_trend_state") or {}
    if st.get("signal"):
        lines.append(f"美股 ETF：{'看多·持有' if st['signal'].get('regime') else '看空·空仓'}")
    lines.append("真实资金交易：永久关闭")
    return f"每日运行摘要 {bj:%m-%d}", "\n".join(lines)


def maybe_daily(now: datetime | None = None) -> bool:
    """每天北京时间 daily_hour 点之后发一次摘要（服务循环每分钟调用）。"""
    now = now or datetime.now(timezone.utc)
    c = get_config()
    bj = now.astimezone(BJ)
    today = bj.strftime("%Y-%m-%d")
    if bj.hour < int(c["daily_hour"]) or db.get_setting("notify_daily_last") == today:
        return False
    db.set_setting("notify_daily_last", today)
    title, body = daily_summary_text(now)
    event("daily_summary", title, body, dedupe=f"daily:{today}")
    return True
