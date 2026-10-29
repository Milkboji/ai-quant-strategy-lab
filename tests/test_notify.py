"""通知模块：唯一出口（白名单 + 审计 + 打码）、发件箱与重试、事件开关、每日摘要、守护进程通道断开提醒。"""
import json
from datetime import datetime, timezone

import pytest

from aiquant import db, notify
from aiquant.data import http

TOKEN = "123456:SECRET-TOKEN-xyz"


@pytest.fixture()
def nt(env, monkeypatch):
    for k in ("NOTIFY_SMTP_HOST", "NOTIFY_SMTP_USER", "NOTIFY_SMTP_PASSWORD", "NOTIFY_EMAIL_TO",
              "NOTIFY_TELEGRAM_BOT_TOKEN", "NOTIFY_TELEGRAM_CHAT_ID", "NOTIFY_SMTP_ALLOW_HOST"):
        monkeypatch.delenv(k, raising=False)
    db.set_setting("notify_config", {})
    notify.ensure_tables()
    db.ex("DELETE FROM notify_outbox")
    sent = []

    class Resp:
        status_code = 200

        def json(self):
            return {"ok": True}

    def fake_request(method, url, **kw):
        sent.append((method, url, kw.get("json")))
        return Resp()
    monkeypatch.setattr(http.requests, "request", fake_request)
    yield sent
    db.set_setting("notify_config", {})


def _tg(monkeypatch):
    monkeypatch.setenv("NOTIFY_TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("NOTIFY_TELEGRAM_CHAT_ID", "42")
    notify.set_config(actor="boss", role="admin", telegram=True)


def test_telegram_whitelist_only_allows_send_message():
    http.check_url("POST", f"https://api.telegram.org/bot{TOKEN}/sendMessage")
    for m, u in [("GET", f"https://api.telegram.org/bot{TOKEN}/getUpdates"),
                 ("POST", f"https://api.telegram.org/bot{TOKEN}/deleteWebhook"),
                 ("GET", f"https://api.telegram.org/bot{TOKEN}/sendMessage"),
                 ("POST", f"http://api.telegram.org/bot{TOKEN}/sendMessage")]:
        with pytest.raises(http.ForbiddenEndpoint):
            http.check_url(m, u)


def test_telegram_send_is_audited_without_token(nt, monkeypatch):
    _tg(monkeypatch)
    assert notify.event("promotion", "策略晋级：测试", "v1 → v2") == 1
    row = db.q1("SELECT * FROM notify_outbox ORDER BY id DESC LIMIT 1")
    assert row["status"] == "sent" and row["channel"] == "telegram"
    assert nt and nt[-1][2]["chat_id"] == "42" and "策略晋级" in nt[-1][2]["text"]
    a = db.q1("SELECT * FROM endpoint_audit ORDER BY id DESC LIMIT 1")
    assert a["url"] == "https://api.telegram.org/bot***/sendMessage" and TOKEN not in a["url"]


def test_smtp_whitelist_and_audit(nt, monkeypatch):
    with pytest.raises(http.ForbiddenEndpoint):
        http.smtp_send(host="smtp.evil.example", port=465, user="u", password="p", sender="u", to=["x@y.z"],
                       subject="s", body="b")
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, context=None):
            calls.append((host, port))

        def login(self, u, p):
            calls.append(("login", u))

        def sendmail(self, a, b, msg):
            calls.append(("send", tuple(b)))

        def quit(self):
            pass
    import smtplib
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    http.smtp_send(host="smtp.qq.com", port=465, user="me@qq.com", password="AUTH-CODE", sender="me@qq.com",
                   to=["a@x.com"], subject="测试", body="正文")
    assert ("send", ("a@x.com",)) in calls
    a = db.q1("SELECT * FROM endpoint_audit ORDER BY id DESC LIMIT 1")
    assert a["method"] == "SMTP" and a["url"] == "smtp://smtp.qq.com:465/sendmail" and a["status"] == 250
    assert "AUTH-CODE" not in json.dumps(a, ensure_ascii=False)
    monkeypatch.setenv("NOTIFY_SMTP_ALLOW_HOST", "smtp.corp.example")
    assert http.smtp_allowed("smtp.corp.example") and not http.smtp_allowed("smtp.other.example")


def test_events_switches_skip_and_dedupe(nt, monkeypatch):
    assert notify.event("awaiting", "无通道", "x") == 0                      # 没有配置任何通道 → 记一条“未发送”
    assert db.q1("SELECT status FROM notify_outbox ORDER BY id DESC LIMIT 1")["status"] == "skipped"
    _tg(monkeypatch)
    notify.set_config(actor="boss", role="admin", events={"loss_streak": False})
    n0 = db.q1("SELECT COUNT(*) AS n FROM notify_outbox")["n"]
    assert notify.event("loss_streak", "关闭的事件", "x") == 0
    assert db.q1("SELECT COUNT(*) AS n FROM notify_outbox")["n"] == n0
    assert notify.event("awaiting", "去重", "x", dedupe="k1") == 1
    assert notify.event("awaiting", "去重", "x", dedupe="k1") == 0
    with pytest.raises(PermissionError):
        notify.set_config(actor="t", role="trader", telegram=False)
    with pytest.raises(PermissionError):
        notify.send_test("telegram", actor="t", role="viewer")
    assert set(notify.EVENTS) == {"promotion", "awaiting", "auto_pause", "loss_streak", "watchdog_restart",
                                  "tunnel_down", "daily_summary", "order_failed", "cash_short", "exposure_high",
                                  "long_hint", "rhythm_review"}


def test_failed_sends_are_retried_then_marked_failed(nt, monkeypatch):
    _tg(monkeypatch)

    def boom(*a, **k):
        raise ConnectionError("network down")
    monkeypatch.setattr(http.requests, "request", boom)
    notify.event("tunnel_down", "断网", "x")
    r = db.q1("SELECT * FROM notify_outbox ORDER BY id DESC LIMIT 1")
    assert r["status"] == "pending" and r["attempts"] == 1 and "network down" in r["last_error"]
    for _ in range(notify.MAX_ATTEMPTS):
        notify.flush()
    r = db.q1("SELECT * FROM notify_outbox WHERE id=?", (r["id"],))
    assert r["status"] == "failed" and r["attempts"] == notify.MAX_ATTEMPTS


def test_daily_summary_once_per_day(nt, monkeypatch):
    _tg(monkeypatch)
    notify.set_config(actor="boss", role="admin", daily_hour=0)
    db.set_setting("notify_daily_last", None)
    now = datetime(2026, 10, 24, 14, 0, tzinfo=timezone.utc)
    assert notify.maybe_daily(now) is True
    assert notify.maybe_daily(now) is False
    r = db.q1("SELECT * FROM notify_outbox WHERE event='daily_summary' ORDER BY id DESC LIMIT 1")
    assert r and "每日运行摘要" in r["title"] and "真实资金交易：永久关闭" in r["body"]


def test_pause_and_awaiting_hooks_notify(nt):
    from aiquant import challenger, evolve
    from aiquant.paper import runner as pr
    from aiquant.strategy import registry
    v = registry.freeze(None, {"name": "通知钩子", "signal_tf": 60,
                               "indicators": [{"id": "f", "type": "EMA", "params": {"period": 9}},
                                              {"id": "s", "type": "EMA", "params": {"period": 21}}],
                               "entry": {"mode": "all", "conditions": [{"left": {"ref": "f"}, "op": ">", "right": {"ref": "s"}}]},
                               "exit": {"mode": "any", "conditions": [{"left": {"ref": "f"}, "op": "<", "right": {"ref": "s"}}]}},
                        source="test", new_lineage=True, set_current=False)
    did = db.ex("INSERT INTO paper_deployments(version_id,config_json,broker,status,created_at,updated_at,is_test) "
                "VALUES(?,?,?,?,?,?,0)", (v["id"], json.dumps({"label": "通知钩子"}), "mock", "running",
                                          db.now_iso(), db.now_iso()))
    try:
        evolve._pause(pr.get_deployment(did), "long", "长周期体检不达标（测试）")
        assert db.q1("SELECT * FROM notify_outbox WHERE event='auto_pause' AND title LIKE '%通知钩子%'")
        raw = db.q1("SELECT * FROM paper_deployments WHERE id=?", (did,))
        cid = challenger.queue_btc(raw, v, dict(v["schema"], stop_loss_pct=3.0), "加止损", {"score": 1}, "通过", "版本已手动锁定", False)
        assert db.q1("SELECT * FROM notify_outbox WHERE event='awaiting' AND body LIKE ?", (f"%#{cid}%",))
        db.ex("UPDATE evolve_challengers SET status='expired' WHERE id=?", (cid,))
    finally:
        pr.stop(did)


def test_watchdog_tunnel_down_alert_after_60s(monkeypatch):
    import importlib
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    svc = importlib.import_module("service")
    got = []
    monkeypatch.setattr(svc, "_notify", lambda kind, title, body, dedupe=None: got.append((kind, title)))
    svc._cf_state.update(down_since=None, down_alerted=False)
    assert svc._tunnel_down_tick(False, now=1000.0) is None
    assert svc._tunnel_down_tick(False, now=1030.0) is None           # 不到 60 秒：不提醒（短暂抖动）
    assert svc._tunnel_down_tick(False, now=1061.0) == "down"
    assert svc._tunnel_down_tick(False, now=1200.0) is None           # 同一次断开只提醒一次
    assert svc._tunnel_down_tick(True, now=1300.0) == "recovered"
    assert [k for k, _ in got] == ["tunnel_down", "tunnel_down"]
    assert svc._tunnel_down_tick(False, now=2000.0) is None and svc._tunnel_down_tick(True, now=2010.0) is None
    assert len(got) == 2                                               # 短暂断开恢复：不发恢复通知
