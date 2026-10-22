"""Alpaca 只读客户端：加密货币历史行情 + Paper 账户只读检查。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .. import config
from . import http

VENUE = "alpaca_us"  # Alpaca crypto 'us' 数据源，单一交易场所，不混合其他场所


def _data_headers() -> dict:
    h = {"Accept": "application/json"}
    k, s = config.secret("ALPACA_API_KEY_ID"), config.secret("ALPACA_API_SECRET_KEY")
    if k and s:
        h["APCA-API-KEY-ID"] = k
        h["APCA-API-SECRET-KEY"] = s
    return h


def _paper_headers() -> dict | None:
    k = config.secret("ALPACA_PAPER_KEY_ID") or config.secret("ALPACA_API_KEY_ID")
    s = config.secret("ALPACA_PAPER_SECRET_KEY") or config.secret("ALPACA_API_SECRET_KEY")
    if not (k and s):
        return None
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s, "Accept": "application/json"}


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_bars_5m(symbol: str, start: datetime, end: datetime, progress=None) -> pd.DataFrame:
    """分页下载 5 分钟 K 线（UTC）。仅返回 Alpaca 实际提供的 K 线，不补任何虚假 K 线。"""
    url = f"{config.ALPACA_DATA_URL}/v1beta3/crypto/us/bars"
    params: dict[str, Any] = {
        "symbols": symbol, "timeframe": "5Min", "start": _iso(start), "end": _iso(end),
        "limit": 10000, "sort": "asc",
    }
    rows: list[dict] = []
    pages = 0
    while True:
        r = http.request("GET", url, headers=_data_headers(), params=params, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Alpaca 行情接口返回 {r.status_code}: {r.text[:200]}")
        js = r.json()
        rows.extend(js.get("bars", {}).get(symbol, []) or [])
        pages += 1
        if progress:
            progress(f"已下载 {len(rows)} 根 5 分钟 K 线（第 {pages} 页）")
        tok = js.get("next_page_token")
        if not tok:
            break
        params["page_token"] = tok
    if not rows:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume", "trade_count", "vwap"])
    df = pd.DataFrame(rows).rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low", "c": "close",
                                            "v": "volume", "n": "trade_count", "vw": "vwap"})
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df[["ts", "open", "high", "low", "close", "volume", "trade_count", "vwap"]]


def latest_quote(symbol: str = "BTC/USD") -> dict | None:
    url = f"{config.ALPACA_DATA_URL}/v1beta3/crypto/us/latest/quotes"
    r = http.request("GET", url, headers=_data_headers(), params={"symbols": symbol}, timeout=10)
    if r.status_code != 200:
        return None
    q = r.json().get("quotes", {}).get(symbol)
    if not q:
        return None
    return {"ts": q.get("t"), "bid": q.get("bp"), "ask": q.get("ap")}


# ---------------------------------------------------------------------------
# Paper 只读
# ---------------------------------------------------------------------------

def paper_get(path: str, params: dict | None = None) -> tuple[int | None, Any]:
    h = _paper_headers()
    if h is None:
        return None, "未配置 Alpaca Paper 凭证（.env 中 ALPACA_PAPER_KEY_ID / ALPACA_PAPER_SECRET_KEY）"
    try:
        r = http.request("GET", f"{config.ALPACA_PAPER_URL}{path}", headers=h, params=params, timeout=10)
    except http.ForbiddenEndpoint as e:
        return None, str(e)
    except Exception as e:  # 网络错误
        return None, f"网络错误: {type(e).__name__}"
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text[:200]


def paper_connection_status() -> dict:
    """真实接口检查，返回 {ok, label, detail}。不会缓存成静态"已连接"。"""
    code, body = paper_get("/v2/account")
    if code == 200 and isinstance(body, dict):
        return {"ok": True, "label": "Alpaca Paper 已连接",
                "detail": f"账户状态 {body.get('status')}", "checked_at": datetime.now(timezone.utc).isoformat()}
    if code is None:
        return {"ok": False, "label": "Alpaca Paper 未连接", "detail": str(body),
                "checked_at": datetime.now(timezone.utc).isoformat()}
    return {"ok": False, "label": f"Alpaca Paper 异常({code})", "detail": str(body)[:160],
            "checked_at": datetime.now(timezone.utc).isoformat()}
