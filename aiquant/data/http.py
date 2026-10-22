"""受控 HTTP 访问层。

- 所有外部请求都会被审计记录（不含密钥）。
- 拒绝任何 Alpaca Live 交易端点。
- 拒绝向任何 /orders 端点发出写请求，除非 Paper 双重锁全部解除（本版本不会解除）。
"""
from __future__ import annotations

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


_ALLOWED_HOSTS = {"data.alpaca.markets", "paper-api.alpaca.markets", "api.deepseek.com"}


def check_url(method: str, url: str) -> None:
    """白名单：只允许三个主机；写请求只允许 DeepSeek 对话接口（以及双重锁解除后的 Paper 下单）。"""
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
        db.audit_endpoint(method.upper(), url.split("?")[0], status)
