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
import logging
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from .. import config, db
from ..data import alpaca, http

ET = ZoneInfo("America/New_York")
log = logging.getLogger("aiquant.etf")
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


def capital_share(equity: float | None) -> float:
    """美股 ETF 实际占用的资金比例（%），BTC 策略按实力分配时先扣除它：
    运行中取「设定比例」和「当前持仓市值 / 权益」的较大者（ETF 涨多了也不会让两边合计超过账户）；
    下线 / 等待下线时只扣除仍在持有的市值。"""
    cfg = get_config()
    st = db.get_setting("etf_trend_state") or {}
    held = float((st.get("position") or {}).get("market_value") or 0)
    held_pct = held / equity * 100 if equity and equity > 0 else 0.0
    target = float(cfg["budget_pct"]) if cfg.get("enabled") and not cfg.get("pending_offline") else 0.0
    return round(max(target, held_pct), 4)


def _log(level: str, msg: str) -> None:
    from . import runner as pr
    pr.event(None, level, f"[ETF趋势] {msg}")


# ---------------------------------------------------------------------------
# 信号（纯函数，便于测试）
# ---------------------------------------------------------------------------

from ..engine.regime_bt import regime_series  # noqa: E402,F401  回测与模拟盘共用同一个信号函数


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


def update_daily_csv(symbols: list[str] | None = None, now: datetime | None = None) -> dict:
    """每个交易日更新一次本地日线库（全部 ETF，只写已收盘的日线；复权口径变化时整只重下）。"""
    from ..data import us_daily
    syms = list(dict.fromkeys(list(symbols or []) + us_daily.UNIVERSE))
    return us_daily.refresh(syms, now)


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

WHY_CODES = ("safe_restart", "disabled", "locked", "no_clock", "closed", "pre_open", "data_error", "pending",
             "bought", "topup", "sold", "hold", "flat", "small", "rejected", "rhythm", "long_pause", "pending_offline")
OPEN_STATUSES = ("new", "accepted", "pending_new", "partially_filled")
FINAL_STATUSES = ("filled", "canceled", "expired", "rejected", "done_for_day")


def _bj(iso: str | None) -> str:
    if not iso:
        return "--"
    try:
        return (datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=8)))
                .strftime("%m-%d %H:%M"))
    except ValueError:
        return str(iso)


def set_why(code: str, text: str, **extra) -> dict:
    """记录「美股 ETF 现在为什么没有动作 / 做了什么」（结构化，供策略总台和面板显示）。内容不变时不重复写库。"""
    cur = db.get_setting("etf_trend_why") or {}
    if cur.get("code") == code and cur.get("text") == text:
        return cur
    w = {"code": code, "text": text, "at": db.now_iso(), **extra}
    db.set_setting("etf_trend_why", w)
    return w


def get_why() -> dict | None:
    return db.get_setting("etf_trend_why")


def entry_filled_usd(sym: str, since_signal_date: str) -> float:
    """本轮持仓（自 since_signal_date 那次买入起）已成交的买入金额。"""
    rows = db.q("SELECT filled_qty, filled_price FROM etf_orders WHERE symbol=? AND side='buy' AND signal_date>=?",
                (sym, since_signal_date))
    return float(sum((r["filled_qty"] or 0) * (r["filled_price"] or 0) for r in rows))


def topup_needed(target: float, filled: float, min_usd: float = 10.0) -> float:
    """部分成交后需要补买的金额；差额小于 max(10 USD, 目标的 2%) 时不补。"""
    rem = float(target) - float(filled)
    return rem if rem >= max(min_usd, 0.02 * float(target)) else 0.0


def _reject(st: dict, today, sym: str, side: str, err: Exception, sig: dict) -> dict:
    """下单被拒：当天不再重试（避免每轮循环都提交一笔会被拒的订单），记录原因并通知。"""
    msg = str(err)[:200]
    st = dict(st, last_action_date=str(today), action=f"下单被拒（{'买入' if side == 'buy' else '卖出'} {sym}）：{msg}",
              signal=sig, checked_at=db.now_iso())
    db.set_setting("etf_trend_state", st)
    set_why("rejected", f"今天的{'买入' if side == 'buy' else '卖出'}订单被券商拒绝：{msg}。今天不再重试，下一个交易日重新判断")
    _log("WARN", f"{'买入' if side == 'buy' else '卖出'} {sym} 被拒绝：{msg}（今天不再重试）")
    try:
        from .. import notify
        notify.event("order_failed", f"美股 ETF 下单被拒：{sym}", f"{'买入' if side == 'buy' else '卖出'} {sym} 被 Alpaca Paper 拒绝：{msg}\n"
                     "今天不再重试，下一个交易日按信号重新判断。", dedupe=f"etf-reject-{today}-{side}")
    except Exception:  # noqa: BLE001
        pass
    return st


def _buy(sym: str, notional: float, sig: dict, why: str) -> str:
    try:
        _submit(sym, "buy", notional=notional, signal_date=sig["date"], reason=why)
        return f"买入 {sym} {notional:,.0f} USD"
    except RuntimeError as e:   # 不支持碎股时改为整股
        if "fraction" not in str(e).lower():
            raise
        px = float(daily_bars(sym, 10)["close"].iloc[-1])
        q = int(notional // px)
        if q < 1:
            return ""
        _submit(sym, "buy", qty=q, signal_date=sig["date"], reason=why)
        return f"买入 {sym} {q} 股"


def _trade_daily(sym: str) -> pd.DataFrame:
    """交易品种已收盘日线（ts = 当天美东收盘时刻），供波动率定仓使用。"""
    from .. import market as mk
    from ..data import us_daily
    d = us_daily.load(sym)
    if d is None or not len(d):
        return pd.DataFrame(columns=["ts", "close"])
    days = us_daily.et_date(d["date"])
    return pd.DataFrame({"ts": pd.to_datetime([mk.session_close(x) for x in days], utc=True), "close": d["close"].values})


def _rhythm_gate(sym: str, now: datetime) -> tuple[str | None, float]:
    """开仓前的统一纪律（与回测 regime_bt 相同的规则）：长周期体检、冷却 / 连亏刹车、波动率定仓。"""
    from .. import market as mk
    from .. import rhythm
    if rhythm.etf_long_paused():
        m = rhythm.etf_long_metrics() or {}
        return (f"长周期体检不达标（年均 {m.get('mean_ret', 0):+.1f}%，盈利 {m.get('pos_years', 0)}/{m.get('n_years', 0)} 年），"
                "暂停新开仓"), 1.0
    rh = rhythm.current(mk.US_EQUITY)
    if rh is None:
        return None, 1.0
    df = _trade_daily(sym) if (rh.vol_target_pct or rh.regime_sma_days) else pd.DataFrame(columns=["ts", "close"])
    block, vm, _ = rhythm.live_gate(rhythm.ETF_KEY, now, df, mk.US_EQUITY)
    return (f"交易节奏：{block}" if block else None), vm


def _rhythm_entry(now: datetime) -> None:
    from .. import market as mk
    from .. import rhythm
    rhythm.live_on_entry(rhythm.ETF_KEY, now, mk.US_EQUITY)


def _rhythm_exit(now: datetime, pnl: float) -> None:
    from .. import market as mk
    from .. import rhythm
    msg = rhythm.live_on_exit(rhythm.ETF_KEY, now, "信号离场", pnl, mk.US_EQUITY)
    if msg:
        _log("WARN", f"交易节奏：{msg}")
        if "连亏" in msg:
            try:
                from .. import notify
                notify.event("loss_streak", "美股 ETF 择时：连亏刹车", msg, dedupe=f"etf-streak-{now:%Y-%m-%d}")
            except Exception:  # noqa: BLE001
                pass


def step(now: datetime | None = None) -> dict:
    try:   # 安全重启进行中：不下新单
        from .. import safe_restart
        if safe_restart.active():
            set_why("safe_restart", "安全重启进行中，暂停下新单")
            return {"skipped": "安全重启进行中，暂停下新单"}
    except Exception:  # noqa: BLE001
        pass
    cfg = get_config()
    if not cfg["enabled"]:
        set_why("disabled", "美股 ETF 择时已下线（未启用）")
        return {}
    if not http.paper_orders_unlocked():
        set_why("locked", "模拟下单锁未打开（双重锁），只计算信号不下单")
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
    if not clk:
        set_why("no_clock", "暂时连不上券商时钟，稍后重试")
        return st
    if not clk.get("is_open"):
        set_why("closed", f"美股休市中，下次开盘 {_bj(clk.get('next_open'))}（北京时间）", next_open=clk.get("next_open"))
        return st
    now_et = now.astimezone(ET)
    today = now_et.date()
    if st.get("last_action_date") == str(today):
        return st
    if now_et.hour * 60 + now_et.minute < 9 * 60 + 35:
        set_why("pre_open", "已开盘，等到美东 9:35（开盘 5 分钟后）再按前一日收盘信号决定")
        return st
    sym = cfg["trade_symbol"]
    try:
        update_daily_csv([cfg["signal_symbol"], sym], now)
    except Exception:  # noqa: BLE001
        log.warning("etf daily csv refresh failed", exc_info=True)
    try:
        sig = compute_signal(daily_bars(cfg["signal_symbol"]), today, cfg)
    except Exception as e:  # noqa: BLE001
        set_why("data_error", f"日线数据暂时取不到，稍后重试：{str(e)[:120]}")
        return st
    pos = _position(sym)
    try:
        from . import etf_versions
        if etf_versions.finish_pending_offline(float(pos["qty"] or 0)):
            set_why("disabled", "已空仓，按计划下线")
            return st
    except Exception:  # noqa: BLE001
        log.warning("pending offline check failed", exc_info=True)
    acct = _account() or {}
    equity = float(acct.get("equity") or 0)
    budget = equity * float(cfg["budget_pct"]) / 100
    cash = float(acct.get("cash") or 0)
    pending = db.q1("SELECT id FROM etf_orders WHERE symbol=? AND status IN ('new','accepted','pending_new',"
                    "'partially_filled')", (sym,))
    in_band = sig["lower"] <= sig["close"] <= sig["upper"]
    band_txt = (f"（收盘 {sig['close']:.2f} 在均线缓冲带 {sig['lower']:.2f}~{sig['upper']:.2f} 内，维持原状态）"
                if in_band else "")
    entry = st.get("entry") if isinstance(st.get("entry"), dict) else None
    action = "保持"
    side = None
    try:
        if pending:
            action = "等待未完成订单"
            set_why("pending", "上一笔订单还没完成，等它成交或过期后再动作")
        elif sig["regime"] == 1 and pos["qty"] <= 0 and cfg.get("pending_offline"):
            set_why("pending_offline", "等待下线：不再开新仓")
        elif sig["regime"] == 1 and pos["qty"] <= 0:
            block, vm = _rhythm_gate(sym, now)
            if block:
                set_why("long_pause" if block.startswith("长周期体检") else "rhythm", f"看多，但{block}，暂不买入")
            else:
                target = budget * vm
                notional = min(target, cash * 0.98)
                if notional >= 10:
                    side = "buy"
                    why = f"{cfg['signal_symbol']} 收盘 {sig['close']:.2f} 高于 {cfg['sma']} 日均线上沿 {sig['upper']:.2f}"
                    if vm < 1:
                        why += f"；波动率定仓 {vm:.0%}"
                    action = _buy(sym, notional, sig, why) or "保持"
                    if action != "保持":
                        entry = {"signal_date": sig["date"], "target": round(target, 2)}
                        set_why("bought", f"看多：{why}，已下单 {action}")
                        _rhythm_entry(now)
                else:
                    set_why("small", f"看多，但可用资金不足 10 USD（现金 {cash:,.2f}），暂不买入")
        elif sig["regime"] == 1 and pos["qty"] > 0:
            rem = 0.0
            if entry and not cfg.get("pending_offline"):
                rem = min(topup_needed(entry["target"], entry_filled_usd(sym, entry["signal_date"])), cash * 0.98)
            if rem >= 10:
                side = "buy"
                action = _buy(sym, rem, sig, f"上次买入只部分成交，补足到目标 {entry['target']:,.0f} USD") or "保持"
                if action != "保持":
                    action = "补足 " + action
                    set_why("topup", f"上次买入只部分成交，今天补买 {rem:,.0f} USD")
            else:
                set_why("hold", f"看多且已持有 {sym}，保持不动{band_txt}")
        elif sig["regime"] == 0 and pos["qty"] > 0:
            side = "sell"
            _submit(sym, "sell", qty=pos["qty"], signal_date=sig["date"],
                    reason=f"{cfg['signal_symbol']} 收盘 {sig['close']:.2f} 低于 {cfg['sma']} 日均线下沿 {sig['lower']:.2f}")
            action = f"卖出全部 {sym}"
            entry = None
            _rhythm_exit(now, float(pos.get("unrealized_pl") or 0))
            set_why("sold", f"看空：{cfg['signal_symbol']} 跌破 {cfg['sma']} 日均线下沿 {sig['lower']:.2f}，已卖出全部 {sym}")
        else:
            set_why("flat", f"看空，保持现金，不持有 {sym}{band_txt}")
    except RuntimeError as e:
        return _reject(st, today, sym, side or "buy", e, sig)
    st = {"last_action_date": str(today), "signal": sig, "action": action, "position": pos,
          "budget": budget, "checked_at": db.now_iso(), "entry": entry}
    db.set_setting("etf_trend_state", st)
    _log("INFO", f"{sig['date']} 收盘 {cfg['signal_symbol']} {sig['close']:.2f}，{cfg['sma']} 日均线 {sig['sma']:.2f}"
                 f"（上沿 {sig['upper']:.2f} / 下沿 {sig['lower']:.2f}）→ {'看多，持有' if sig['regime'] else '看空，空仓'} {sym}；动作：{action}")
    return st


def orders(limit: int = 30) -> list[dict]:
    ensure_tables()
    return db.q("SELECT * FROM etf_orders ORDER BY id DESC LIMIT ?", (limit,))
