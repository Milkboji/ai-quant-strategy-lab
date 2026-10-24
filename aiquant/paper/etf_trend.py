"""美股 ETF 趋势择时（Alpaca Paper）：QQQ 150 日均线 ±2% 择时 → 持有 QLD（2 倍纳指 ETF），否则持有现金。

研究依据（2026-10-21，10.8 年日线，次日开盘成交，单边 5bp 成本）：
- 挑选期 2016-2022：年化 +28.9%，最大回撤 -34.2%（一直持有 QLD：+21.0% / -64.3%）
- 验证期 2023-2024：年化 +46.3%，回撤 -29.7%
- 最终检验 2025-01~2026-10（只看一次）：累计 +34.6%，回撤 -22.4%（持有 QLD：+63.0% / -44.0%）
  牛市里跑不赢一直持有；价值在熊市（2018、2020、2022）大幅降低回撤。
杠杆来自 2 倍 ETF 本身，没有保证金借款和强平。

执行规则（与回测一致）：用已收盘的日线算信号（带 ±2% 缓冲防止来回切换），
美股开盘 5 分钟后提交当日市价单；每个交易日最多动作一次。只做多，不做空。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from .. import config, db
from ..data import alpaca, http

ET = ZoneInfo("America/New_York")
DEFAULTS = {"enabled": True, "signal_symbol": "QQQ", "trade_symbol": "QLD", "sma": 150, "band": 0.02,
            "budget_pct": 30.0}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS etf_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, side TEXT, notional REAL, qty REAL,
  status TEXT, filled_qty REAL, filled_price REAL, client_order_id TEXT UNIQUE, signal_date TEXT, reason TEXT,
  raw_json TEXT);
"""


def ensure_tables() -> None:
    with db.tx() as c:
        c.executescript(SCHEMA_SQL)


def get_config() -> dict:
    d = dict(DEFAULTS)
    d.update(db.get_setting("etf_trend_config") or {})
    return d


def _log(level: str, msg: str) -> None:
    from . import runner as pr
    pr.event(None, level, f"[ETF趋势] {msg}")


# ---------------------------------------------------------------------------
# 信号（纯函数，便于测试）
# ---------------------------------------------------------------------------

def regime_series(close: pd.Series, n: int, band: float) -> pd.Series:
    """带缓冲的均线状态：收盘 > 均线×(1+band) → 1；< 均线×(1−band) → 0；中间保持上一状态。"""
    ma = close.rolling(n).mean()
    st = pd.Series(float("nan"), index=close.index)
    st[close > ma * (1 + band)] = 1.0
    st[close < ma * (1 - band)] = 0.0
    return st.ffill().fillna(0.0)


def daily_bars(symbol: str, days: int = 420) -> pd.DataFrame:
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    params = {"symbols": symbol, "timeframe": "1Day", "start": (end - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "adjustment": "all", "feed": "sip", "limit": 10000}
    r = http.request("GET", f"{config.ALPACA_DATA_URL}/v2/stocks/bars", headers=alpaca._data_headers(),
                     params=params, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"日线获取失败 {r.status_code}: {r.text[:120]}")
    rows = (r.json().get("bars") or {}).get(symbol, []) or []
    df = pd.DataFrame(rows).rename(columns={"t": "date", "c": "close"})
    df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_convert(ET).dt.date
    return df[["date", "close"]]


def update_daily_csv(symbols: list[str]) -> None:
    """把最近 30 天的日线并入 data/etf_daily/*.csv（供挑战者前向评估）。"""
    out = config.DATA_DIR / "etf_daily"
    out.mkdir(parents=True, exist_ok=True)
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    for sym in symbols:
        params = {"symbols": sym, "timeframe": "1Day", "start": (end - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "adjustment": "all", "feed": "sip", "limit": 1000}
        r = http.request("GET", f"{config.ALPACA_DATA_URL}/v2/stocks/bars", headers=alpaca._data_headers(),
                         params=params, timeout=20)
        if r.status_code != 200:
            continue
        rows = (r.json().get("bars") or {}).get(sym, []) or []
        if not rows:
            continue
        new = pd.DataFrame(rows).rename(columns={"t": "date", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        new = new[["date", "open", "high", "low", "close", "volume"]]
        p = out / f"{sym}.csv"
        old = pd.read_csv(p) if p.exists() else new.iloc[0:0]
        allv = pd.concat([old, new]).drop_duplicates("date", keep="last").sort_values("date")
        tmp = p.with_suffix(".tmp")
        allv.to_csv(tmp, index=False)
        tmp.replace(p)


def compute_signal(df: pd.DataFrame, today, cfg: dict) -> dict:
    """只用 today 之前已收盘的日线。"""
    d = df[df["date"] < today].reset_index(drop=True)
    if len(d) < cfg["sma"] + 5:
        raise RuntimeError("日线数量不足以计算均线")
    st = regime_series(d["close"], int(cfg["sma"]), float(cfg["band"]))
    ma = d["close"].rolling(int(cfg["sma"])).mean()
    return {"date": str(d["date"].iloc[-1]), "close": float(d["close"].iloc[-1]), "sma": float(ma.iloc[-1]),
            "regime": int(st.iloc[-1]), "upper": float(ma.iloc[-1] * (1 + cfg["band"])),
            "lower": float(ma.iloc[-1] * (1 - cfg["band"]))}


# ---------------------------------------------------------------------------
# 券商交互（Alpaca Paper）
# ---------------------------------------------------------------------------

def _clock() -> dict | None:
    code, b = alpaca.paper_get("/v2/clock")
    return b if code == 200 and isinstance(b, dict) else None


def _position(sym: str) -> dict:
    code, b = alpaca.paper_get(f"/v2/positions/{sym}")
    if code == 200 and isinstance(b, dict):
        return {"qty": float(b.get("qty") or 0), "market_value": float(b.get("market_value") or 0),
                "avg_entry_price": float(b.get("avg_entry_price") or 0), "unrealized_pl": float(b.get("unrealized_pl") or 0)}
    return {"qty": 0.0, "market_value": 0.0, "avg_entry_price": 0.0, "unrealized_pl": 0.0}


def _account() -> dict | None:
    code, b = alpaca.paper_get("/v2/account")
    return b if code == 200 and isinstance(b, dict) else None


def _submit(sym: str, side: str, *, notional: float | None = None, qty: float | None = None,
            signal_date: str, reason: str) -> dict:
    coid = f"etf-{sym.lower()}-{side}-{signal_date}-{uuid.uuid4().hex[:6]}"
    body = {"symbol": sym, "side": side, "type": "market", "time_in_force": "day", "client_order_id": coid}
    if notional is not None:
        body["notional"] = f"{notional:.2f}"
    else:
        body["qty"] = f"{qty:.9f}".rstrip("0").rstrip(".")
    db.ex("INSERT INTO etf_orders(ts,symbol,side,notional,qty,status,client_order_id,signal_date,reason) "
          "VALUES(?,?,?,?,?,?,?,?,?)", (db.now_iso(), sym, side, notional, qty, "new", coid, signal_date, reason))
    r = http.request("POST", f"{config.ALPACA_PAPER_URL}/v2/orders", headers=alpaca._paper_headers(), json_body=body)
    info = r.json() if r.content else {}
    status = info.get("status") if r.status_code in (200, 201) else f"rejected_{r.status_code}"
    db.ex("UPDATE etf_orders SET status=?, raw_json=? WHERE client_order_id=?",
          (status, json.dumps(info, ensure_ascii=False)[:2000], coid))
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Alpaca Paper 拒绝订单 {r.status_code}: {r.text[:160]}")
    return info


def sync_orders() -> None:
    for o in db.q("SELECT * FROM etf_orders WHERE status NOT IN ('filled','canceled','expired','rejected') "
                  "AND status NOT LIKE 'rejected_%'"):
        code, b = alpaca.paper_get("/v2/orders:by_client_order_id", {"client_order_id": o["client_order_id"]})
        if code == 200 and isinstance(b, dict):
            db.ex("UPDATE etf_orders SET status=?, filled_qty=?, filled_price=? WHERE id=?",
                  (b.get("status"), float(b.get("filled_qty") or 0), float(b.get("filled_avg_price") or 0) or None, o["id"]))
            if b.get("status") == "filled" and o["status"] != "filled":
                _log("INFO", f"{'买入' if o['side'] == 'buy' else '卖出'} {o['symbol']} 成交 {float(b.get('filled_qty') or 0):.4f} 股 "
                             f"@ {float(b.get('filled_avg_price') or 0):,.2f}")


# ---------------------------------------------------------------------------
# 每轮服务循环调用
# ---------------------------------------------------------------------------

def step(now: datetime | None = None) -> dict:
    try:   # 安全重启进行中：不下新单
        from .. import safe_restart
        if safe_restart.active():
            return {"skipped": "安全重启进行中，暂停下新单"}
    except Exception:  # noqa: BLE001
        pass
    cfg = get_config()
    if not cfg["enabled"] or not http.paper_orders_unlocked():
        return {}
    ensure_tables()
    sync_orders()
    now = now or datetime.now(timezone.utc)
    st = db.get_setting("etf_trend_state") or {}
    if not db.get_setting("etf_trend_rebalanced"):   # 首次启用：把 ETF 资金比例从 BTC 策略额度中划出
        try:
            from .. import evolve
            evolve.rebalance()
            db.set_setting("etf_trend_rebalanced", db.now_iso())
        except Exception:  # noqa: BLE001
            pass
    clk = _clock()
    if not clk or not clk.get("is_open"):
        return st
    now_et = now.astimezone(ET)
    today = now_et.date()
    if now_et.hour * 60 + now_et.minute < 9 * 60 + 35 or st.get("last_action_date") == str(today):
        return st
    sym = cfg["trade_symbol"]
    try:
        update_daily_csv([cfg["signal_symbol"], sym])
    except Exception:  # noqa: BLE001
        pass
    sig = compute_signal(daily_bars(cfg["signal_symbol"]), today, cfg)
    pos = _position(sym)
    acct = _account() or {}
    equity = float(acct.get("equity") or 0)
    budget = equity * float(cfg["budget_pct"]) / 100
    pending = db.q1("SELECT id FROM etf_orders WHERE symbol=? AND status IN ('new','accepted','pending_new',"
                    "'partially_filled')", (sym,))
    action = "保持"
    if pending:
        action = "等待未完成订单"
    elif sig["regime"] == 1 and pos["qty"] <= 0:
        cash = float(acct.get("cash") or 0)
        notional = min(budget, cash * 0.98)
        if notional >= 10:
            why = f"{cfg['signal_symbol']} 收盘 {sig['close']:.2f} 高于 {cfg['sma']} 日均线上沿 {sig['upper']:.2f}"
            try:
                _submit(sym, "buy", notional=notional, signal_date=sig["date"], reason=why)
                action = f"买入 {sym} {notional:,.0f} USD"
            except RuntimeError as e:   # 不支持碎股时改为整股
                if "fraction" not in str(e).lower():
                    raise
                px = float(daily_bars(sym, 10)["close"].iloc[-1])
                q = int(notional // px)
                if q >= 1:
                    _submit(sym, "buy", qty=q, signal_date=sig["date"], reason=why)
                    action = f"买入 {sym} {q} 股"
    elif sig["regime"] == 0 and pos["qty"] > 0:
        _submit(sym, "sell", qty=pos["qty"], signal_date=sig["date"],
                reason=f"{cfg['signal_symbol']} 收盘 {sig['close']:.2f} 低于 {cfg['sma']} 日均线下沿 {sig['lower']:.2f}")
        action = f"卖出全部 {sym}"
    st = {"last_action_date": str(today), "signal": sig, "action": action, "position": pos,
          "budget": budget, "checked_at": db.now_iso()}
    db.set_setting("etf_trend_state", st)
    _log("INFO", f"{sig['date']} 收盘 {cfg['signal_symbol']} {sig['close']:.2f}，{cfg['sma']} 日均线 {sig['sma']:.2f}"
                 f"（上沿 {sig['upper']:.2f} / 下沿 {sig['lower']:.2f}）→ {'看多，持有' if sig['regime'] else '看空，空仓'} {sym}；动作：{action}")
    return st


def orders(limit: int = 30) -> list[dict]:
    ensure_tables()
    return db.q("SELECT * FROM etf_orders ORDER BY id DESC LIMIT ?", (limit,))
