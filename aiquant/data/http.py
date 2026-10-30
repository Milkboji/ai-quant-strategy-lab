"""受控 HTTP 访问层。

- 所有外部请求都会被审计记录（不含密钥）。
- 拒绝任何 Alpaca Live 交易端点。
- 拒绝向任何 /orders 端点发出写请求，除非 Paper 双重锁全部解除（本版本不会解除）。
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlparse

import requests

from .. import config, db


class ForbiddenEndpoint(RuntimeError):
    pass


_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def paper_orders_unlocked() -> bool:
    """Paper 下单双重锁：代码常量 + 本地授权文件。"""
    if not config.PAPER_ORDER_SUBMISSION_UNLOCKED:
        return False
    auth = config.CONFIG_DIR / "paper_authorization.json"
    return auth.exists()


_ALLOWED_HOSTS = {"data.alpaca.markets", "paper-api.alpaca.markets", "api.deepseek.com",
                  "api.telegram.org",       # Telegram 只允许 POST /bot<token>/sendMessage（通知模块）
                  "qyapi.weixin.qq.com",    # 企业微信群机器人只允许 POST /cgi-bin/webhook/send?key=…
                  "open.feishu.cn"}         # 飞书群机器人只允许 POST /open-apis/bot/v2/hook/<token>
_TG_SEND = re.compile(r"^/bot[^/]+/sendmessage$")
_WECOM_SEND = "/cgi-bin/webhook/send"
_FEISHU_HOOK = re.compile(r"^/open-apis/bot/v2/hook/[a-z0-9-]+$")
_WEBHOOK_ONLY = {"api.telegram.org": "Telegram 只允许发送消息（POST sendMessage）",
                 "qyapi.weixin.qq.com": "企业微信只允许群机器人发消息（POST /cgi-bin/webhook/send）",
                 "open.feishu.cn": "飞书只允许群机器人发消息（POST /open-apis/bot/v2/hook/…）"}


def _webhook_ok(host: str, method: str, path: str) -> bool:
    if method != "POST":
        return False
    if host == "api.telegram.org":
        return bool(_TG_SEND.match(path))
    if host == "qyapi.weixin.qq.com":
        return path == _WECOM_SEND
    if host == "open.feishu.cn":
        return bool(_FEISHU_HOOK.match(path))
    return False


# 通知邮件的 SMTP 主机白名单（常见邮箱服务商）。如用其他服务商，在 config/secrets.local 写 NOTIFY_SMTP_ALLOW_HOST=主机名
SMTP_HOSTS = {"smtp.qq.com", "smtp.exmail.qq.com", "smtp.163.com", "smtp.126.com", "smtp.yeah.net", "smtp.sina.com",
              "smtp.aliyun.com", "smtp.qiye.aliyun.com", "smtp.gmail.com", "smtp.office365.com",
              "smtp-mail.outlook.com", "smtp.mail.yahoo.com", "smtp.zoho.com"}


def check_url(method: str, url: str) -> None:
    """白名单主机；写请求只允许 DeepSeek 对话接口、Telegram 发消息（以及双重锁解除后的 Paper 下单）。"""
    u = urlparse(url)
    if u.scheme != "https":
        raise ForbiddenEndpoint("只允许 HTTPS")
    host = (u.hostname or "").lower().rstrip(".")
    if host == "api.alpaca.markets":
        raise ForbiddenEndpoint("禁止访问 Alpaca Live 端点 api.alpaca.markets")
    if host not in _ALLOWED_HOSTS:
        raise ForbiddenEndpoint(f"未在白名单中的端点被拒绝: {host}")
    path = unquote(u.path).lower().rstrip("/")
    m = method.upper()
    if host in _WEBHOOK_ONLY:
        if not _webhook_ok(host, m, path):
            raise ForbiddenEndpoint(_WEBHOOK_ONLY[host])
        return
    if m in _WRITE_METHODS:
        if host == "api.deepseek.com" and m == "POST" and path == "/chat/completions":
            return
        if host == "paper-api.alpaca.markets" and paper_orders_unlocked() and (
                (m == "POST" and path == "/v2/orders")
                or (m == "DELETE" and path in ("/v2/orders", "/v2/positions/btcusd", "/v2/positions/btc/usd"))):
            return
        raise ForbiddenEndpoint("写请求被拒绝：订单/平仓权限锁定（PAPER_ORDERS=0, LIVE_ORDERS=0）")


def request(method: str, url: str, *, headers: dict | None = None, params: dict | None = None,
            json_body: Any = None, timeout: float = 20.0) -> requests.Response:
    check_url(method, url)
    status = None
    try:
        r = requests.request(method, url, headers=headers or {}, params=params, json=json_body,
                             timeout=timeout)
        status = r.status_code
        return r
    finally:
        db.audit_endpoint(method.upper(), redact(url.split("?")[0]), status)


def redact(url: str) -> str:
    """审计记录里不保存密钥：Telegram 机器人 token、企业微信 key、飞书 hook token 打码。"""
    return redact_text(url)


def redact_text(text: str) -> str:
    """任意文本（地址、错误信息）里的通知密钥打码，用于审计和发件箱的错误记录。"""
    t = re.sub(r"/bot[^/\s]+/", "/bot***/", str(text))
    t = re.sub(r"([?&]key=)[^&\s'\"]+", r"\1***", t)
    return re.sub(r"(/bot/v2/hook/)[^/?\s'\"]+", r"\1***", t)


def smtp_allowed(host: str) -> bool:
    h = (host or "").lower().rstrip(".")
    extra = (config.secret("NOTIFY_SMTP_ALLOW_HOST") or "").lower().strip()
    return h in SMTP_HOSTS or (bool(extra) and h == extra)


def smtp_send(*, host: str, port: int, user: str, password: str, sender: str, to: list[str], subject: str,
              body: str, timeout: float = 20.0) -> None:
    """发送通知邮件（唯一出口：主机白名单 + 审计，不记录账号密码）。465 端口用 SSL，其余用 STARTTLS。"""
    import smtplib
    import ssl
    from email.header import Header
    from email.mime.text import MIMEText
    from email.utils import formataddr
    if not smtp_allowed(host):
        raise ForbiddenEndpoint(f"SMTP 主机不在白名单中: {host}")
    status = None
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((str(Header("AI Quant Strategy Lab", "utf-8")), sender))
        msg["To"] = ", ".join(to)
        ctx = ssl.create_default_context()
        if int(port) == 465:
            s = smtplib.SMTP_SSL(host, int(port), timeout=timeout, context=ctx)
        else:
            s = smtplib.SMTP(host, int(port), timeout=timeout)
            s.starttls(context=ctx)
        try:
            s.login(user, password)
            s.sendmail(sender, to, msg.as_string())
            status = 250
        finally:
            try:
                s.quit()
            except Exception:  # noqa: BLE001
                pass
    finally:
        db.audit_endpoint("SMTP", f"smtp://{host}:{int(port)}/sendmail", status)
