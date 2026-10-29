"""下单前现金检查与杠杆告警（BTC 策略与美股 ETF 共用，2026-10-29 用户规则）。

- 任何情况下都不得使用券商保证金：买单金额不超过「账户现金 − 其他未成交买单占用」的 98%（留出手续费 / 滑点）。
  不使用 buying_power（Alpaca 模拟账户默认带保证金，buying_power 可能是现金的 2~4 倍）。
- 现金不足时按可用现金缩量，写原因（返回给调用方写进日志 /「为什么没有动作」）并发通知（每个下单方每天一次）。
- 只做多：卖出数量不超过持仓，不会产生空头和借券。
- 账户总持仓市值 ÷ 权益 > 100%，或现金为负（已在使用保证金）→ 告警（日志 + 通知，每天一次）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .. import db

log = logging.getLogger("aiquant.cash")
CASH_BUFFER = 0.98
MIN_ORDER_USD = 10.0
EXPOSURE_LIMIT = 1.0
OPEN_ETF = ("new", "accepted", "pending_new", "partially_filled")
OPEN_BTC = ("new", "submitted", "unknown_after_restart")


def reserved_cash(btc_price: float | None = None, *, exclude: str | None = None) -> float:
    """其他未成交买单预计占用的现金：美股 ETF 未成交买单金额 + BTC 未成交买单（数量 × 最新价）。
    exclude="etf" / "btc"：不算该下单方自己的（它自己的未成交单另有「等待未完成订单」规则）。"""
    total = 0.0
    if exclude != "etf":
        try:
            rows = db.q("SELECT notional, qty, filled_qty, filled_price FROM etf_orders WHERE side='buy' AND status IN "
                        f"({','.join('?' * len(OPEN_ETF))})", OPEN_ETF)
            total += sum(float(r["notional"] or 0) - float(r["filled_qty"] or 0) * float(r["filled_price"] or 0)
                         for r in rows)
        except Exception:  # noqa: BLE001  表还没建
            pass
    if exclude != "btc" and btc_price:
        rows = db.q("SELECT o.qty FROM paper_orders o JOIN paper_deployments d ON d.id=o.deployment_id "
                    f"WHERE d.broker='alpaca_paper' AND o.is_test=0 AND o.side='buy' AND o.status IN "
                    f"({','.join('?' * len(OPEN_BTC))})", OPEN_BTC)
        total += sum(float(r["qty"] or 0) for r in rows) * float(btc_price)
    return max(total, 0.0)


def check_buy(want_usd: float, cash: float, *, who: str, symbol: str, reserved: float = 0.0,
              dedupe: str | None = None) -> tuple[float, str | None]:
    """返回 (允许的买入金额, 缩量原因或 None)。允许金额 = min(想买的金额, (现金 − 未成交买单占用) × 98%)，不用保证金。
    允许金额不足 10 USD 时返回 0（调用方不下单）。缩量或放弃时写日志并发通知。"""
    want = max(float(want_usd), 0.0)
    avail = max(float(cash) - float(reserved), 0.0) * CASH_BUFFER
    if want <= avail + 1e-6:
        return want, None
    allowed = avail if avail >= MIN_ORDER_USD else 0.0
    occ = f"，另有未成交买单占用 {reserved:,.0f} USD" if reserved > 0.5 else ""
    neg = "（现金为负：账户可能已在使用保证金）" if cash < 0 else ""
    if allowed > 0:
        why = (f"现金不足：{who}想买 {symbol} {want:,.0f} USD，账户现金 {cash:,.0f} USD{occ}{neg}；"
               f"按可用现金缩量到 {allowed:,.0f} USD，不使用券商保证金")
    else:
        why = (f"现金不足：{who}想买 {symbol} {want:,.0f} USD，账户现金 {cash:,.0f} USD{occ}{neg}；"
               f"可用现金不足 {MIN_ORDER_USD:g} USD，本次不下单（不使用券商保证金）")
    _notify("cash_short", f"现金不足，订单缩量：{who}", why,
            dedupe or f"cash-{who}-{datetime.now(timezone.utc):%Y-%m-%d}")
    return round(allowed, 2), why


def exposure(acct: dict) -> dict:
    """账户总持仓市值（多头 + 空头绝对值）÷ 权益。acct 为 Alpaca /v2/account 的原始字段。"""
    eq = float(acct.get("equity") or 0)
    long_v = abs(float(acct.get("long_market_value") or 0))
    short_v = abs(float(acct.get("short_market_value") or 0))
    cash = float(acct.get("cash") or 0)
    ratio = (long_v + short_v) / eq if eq > 0 else 0.0
    return {"equity": eq, "long": long_v, "short": short_v, "cash": cash, "ratio": round(ratio, 4),
            "over": ratio > EXPOSURE_LIMIT + 1e-9, "negative_cash": cash < -0.01, "at": db.now_iso()}


def exposure_check(acct: dict | None = None) -> dict | None:
    """读一次账户，超过 100% 或现金为负时告警（日志 + 通知，每天一次）。结果存 settings.account_exposure 供页面显示。"""
    if acct is None:
        from ..data import alpaca
        code, b = alpaca.paper_get("/v2/account")
        if code != 200 or not isinstance(b, dict):
            return None
        acct = b
    x = exposure(acct)
    db.set_setting("account_exposure", x)
    if x["over"] or x["negative_cash"]:
        msg = (f"账户总持仓市值 {x['long'] + x['short']:,.0f} USD ÷ 权益 {x['equity']:,.0f} USD = {x['ratio']:.1%}"
               + ("，超过 100%" if x["over"] else "") + (f"；现金为负 {x['cash']:,.0f} USD（已在使用券商保证金）"
                                                         if x["negative_cash"] else ""))
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if db.get_setting("account_exposure_alert") != day:
            db.set_setting("account_exposure_alert", day)
            from . import runner as pr
            pr.event(None, "ERROR", f"杠杆告警：{msg}")
            _notify("exposure_high", "杠杆告警：持仓市值超过权益", msg + "\n规则：任何情况下都不使用保证金，请检查各策略持仓与额度。",
                    f"exposure-{day}")
    return x


def maybe_exposure_check(min_interval_sec: int = 300) -> dict | None:
    last = db.get_setting("account_exposure") or {}
    try:
        if last.get("at") and (datetime.now(timezone.utc) - datetime.fromisoformat(last["at"])).total_seconds() \
                < min_interval_sec:
            return None
    except ValueError:
        pass
    return exposure_check()


def _notify(kind: str, title: str, body: str, dedupe: str) -> None:
    log.warning("%s: %s", title, body)
    try:
        from .. import notify
        notify.event(kind, title, body, dedupe=dedupe)
    except Exception:  # noqa: BLE001
        log.warning("notify failed", exc_info=True)
