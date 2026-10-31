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
              "NOTIFY_TELEGRAM_BOT_TOKEN", "NOTIFY_TELEGRAM_CHAT_ID", "NOTIFY_SMTP_ALLOW_HOST",
              "NOTIFY_WECOM_WEBHOOK", "NOTIFY_FEISHU_WEBHOOK", "NOTIFY_FEISHU_SECRET"):
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
                                  "long_hint", "rhythm_review", "scorecard", "experiment"}


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


WECOM_KEY = "693a91f6-7xxx-4bc4-97a0-0ec2sifa5aaa"
FEISHU_TOK = "2d5a7c91-aaaa-bbbb-cccc-123456789abc"


def test_wecom_feishu_whitelist_only_allows_bot_send():
    http.check_url("POST", f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM_KEY}")
    http.check_url("POST", f"https://open.feishu.cn/open-apis/bot/v2/hook/{FEISHU_TOK}")
    for m, u in [("GET", f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM_KEY}"),
                 ("POST", "https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token=x"),
                 ("POST", f"https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media?key={WECOM_KEY}"),
                 ("GET", f"https://open.feishu.cn/open-apis/bot/v2/hook/{FEISHU_TOK}"),
                 ("POST", "https://open.feishu.cn/open-apis/im/v1/messages"),
                 ("POST", "https://oapi.dingtalk.com/robot/send?access_token=x")]:
        with pytest.raises(http.ForbiddenEndpoint):
            http.check_url(m, u)


def test_wecom_and_feishu_sent_in_parallel_with_masked_audit_and_switches(nt, monkeypatch):
    sent = []

    class R:
        def __init__(self, url):
            self.status_code = 200
            self.url = url

        def json(self):
            return {"errcode": 0, "errmsg": "ok"} if "qyapi" in self.url else {"code": 0, "msg": "success"}

    def fake(method, url, **kw):
        sent.append((url, kw.get("json")))
        return R(url)
    monkeypatch.setattr(http.requests, "request", fake)
    monkeypatch.setenv("NOTIFY_WECOM_WEBHOOK", f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={WECOM_KEY}")
    monkeypatch.setenv("NOTIFY_FEISHU_WEBHOOK", FEISHU_TOK)          # 只填 token 也可以
    monkeypatch.setenv("NOTIFY_FEISHU_SECRET", "sec")
    monkeypatch.setenv("NOTIFY_TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("NOTIFY_TELEGRAM_CHAT_ID", "42")
    st = notify.channel_status()
    assert st["wecom"]["configured"] and st["feishu"]["configured"]
    assert WECOM_KEY not in st["wecom"]["detail"] and FEISHU_TOK not in st["feishu"]["detail"]
    # 只开企业微信和飞书（Telegram 已配置但关闭）
    notify.set_config(actor="boss", role="admin", wecom=True, feishu=True, telegram=False)
    assert notify.event("loss_streak", "连亏刹车", "BTC #2 暂停 72 小时") == 2
    hosts = sorted(u.split("/")[2] for u, _ in sent)
    assert hosts == ["open.feishu.cn", "qyapi.weixin.qq.com"]
    wb = next(b for u, b in sent if "qyapi" in u)
    fb = next(b for u, b in sent if "feishu" in u)
    assert wb["msgtype"] == "text" and "连亏刹车" in wb["text"]["content"]
    assert fb["msg_type"] == "text" and fb["sign"] and fb["timestamp"]
    rows = db.q("SELECT channel,status FROM notify_outbox ORDER BY id")
    assert {r["channel"]: r["status"] for r in rows} == {"wecom": "sent", "feishu": "sent"}
    aud = [a["url"] for a in db.q("SELECT url FROM endpoint_audit ORDER BY id DESC LIMIT 2")]
    assert all(WECOM_KEY not in a and FEISHU_TOK not in a for a in aud)
    assert "https://open.feishu.cn/open-apis/bot/v2/hook/***" in aud
    from aiquant import auth
    auth.ensure()
    notify.set_config(actor="boss", role="admin", wecom=True)
    a = db.q1("SELECT * FROM user_audit WHERE action='修改通知设置' ORDER BY id DESC LIMIT 1")
    assert "企业微信 开" in a["detail"] and "Telegram 关" in a["detail"]


def test_wecom_error_code_retried_and_error_text_masked(nt, monkeypatch):
    class R:
        status_code = 200

        def json(self):
            return {"errcode": 93000, "errmsg": "invalid webhook url"}

    def boom(method, url, **kw):
        if "feishu" in url:
            raise ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {url}")
        return R()
    monkeypatch.setattr(http.requests, "request", boom)
    monkeypatch.setenv("NOTIFY_WECOM_WEBHOOK", WECOM_KEY)            # 只填 key
    monkeypatch.setenv("NOTIFY_FEISHU_WEBHOOK", f"https://open.feishu.cn/open-apis/bot/v2/hook/{FEISHU_TOK}")
    notify.set_config(actor="boss", role="admin", wecom=True, feishu=True)
    notify.event("order_failed", "下单被拒", "x")
    rows = {r["channel"]: r for r in db.q("SELECT * FROM notify_outbox")}
    assert rows["wecom"]["status"] == "pending" and "93000" in rows["wecom"]["last_error"]
    assert rows["feishu"]["status"] == "pending" and FEISHU_TOK not in rows["feishu"]["last_error"]
    assert "hook/***" in rows["feishu"]["last_error"]
    with pytest.raises(RuntimeError) as ei:
        notify.send_test("feishu", actor="boss", role="admin")
    assert FEISHU_TOK not in str(ei.value)


def test_feishu_sign_matches_reference():
    import base64
    import hashlib
    import hmac
    s = notify.feishu_sign("demo", ts=1599360473)
    exp = base64.b64encode(hmac.new(b"1599360473\ndemo", b"", hashlib.sha256).digest()).decode()
    assert s == {"timestamp": "1599360473", "sign": exp}
