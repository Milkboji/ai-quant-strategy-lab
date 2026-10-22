"""2 倍杠杆模拟账户（影子账户）。

跟随核心策略（默认部署 #1，日线趋势）在 Alpaca Paper 上的真实成交，按永续合约逐仓规则本地记账：
- 买入成交时：用全部保证金 × 杠杆 开多；卖出成交时平仓。成交价 = 该笔 Paper 订单的真实成交价。
- 手续费 0.07%（含滑点）按名义价值计；资金费率每 8 小时（UTC 0/8/16 点）按 0.01% × 名义价值扣除。
- 逐仓强平：价格跌到 开仓价 × (1 − 1/杠杆 + 维持保证金率) 时强平，保证金清零。
不连接任何交易所、不下任何订单，只是“如果加 2 倍杠杆会怎样”的实时记账。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pandas as pd

from .. import db

DEFAULTS = {"enabled": True, "source_deployment": 1, "leverage": 2.0, "margin0": 10000.0,
            "fee_rate": 0.0007, "funding_8h": 0.0001, "maint_margin": 0.004}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS lev_shadow_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, price REAL, qty REAL, notional REAL, fee REAL,
  pnl REAL, equity_after REAL, src_order_id INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS lev_shadow_equity (ts TEXT PRIMARY KEY, equity REAL, price REAL);
"""


def ensure_tables() -> None:
    with db.tx() as c:
        c.executescript(SCHEMA_SQL)


def get_config() -> dict:
    d = dict(DEFAULTS)
    d.update(db.get_setting("lev_shadow_config") or {})
    return d


def get_state() -> dict:
    c = get_config()
    s = db.get_setting("lev_shadow_state")
    if not s:
        s = {"cash": c["margin0"], "qty": 0.0, "entry": None, "last_order_id": _max_order_id(c),
             "last_funding": None, "started_at": db.now_iso(), "funding_paid": 0.0, "fees_paid": 0.0}
        db.set_setting("lev_shadow_state", s)
    return s


def _max_order_id(c: dict) -> int:
    r = db.q1("SELECT MAX(id) AS m FROM paper_orders WHERE deployment_id=?", (c["source_deployment"],))
    return int(r["m"] or 0)


def liq_price(entry: float, lev: float, mm: float) -> float:
    return entry * (1 - 1 / lev + mm)


def equity(s: dict, price: float | None) -> float:
    if s["qty"] and price:
        return s["cash"] + s["qty"] * (price - s["entry"])
    return s["cash"]


def _trade(kind, price, qty, fee, pnl, eq, oid=None, note="") -> None:
    db.ex("INSERT INTO lev_shadow_trades(ts,kind,price,qty,notional,fee,pnl,equity_after,src_order_id,note) "
          "VALUES(?,?,?,?,?,?,?,?,?,?)", (db.now_iso(), kind, price, qty, qty * price, fee, pnl, eq, oid, note))


def update(price: float | None, now: datetime | None = None) -> dict:
    """每轮服务循环调用：同步源策略成交、扣资金费、检查强平、记录权益。幂等。"""
    c = get_config()
    if not c["enabled"]:
        return {}
    ensure_tables()
    s = get_state()
    now = now or datetime.now(timezone.utc)
    lev, fee_r = float(c["leverage"]), float(c["fee_rate"])
    # 1) 跟随源策略的新成交
    for o in db.q("SELECT * FROM paper_orders WHERE deployment_id=? AND id>? AND status='filled' ORDER BY id",
                  (c["source_deployment"], s["last_order_id"])):
        px = float(o["filled_price"] or 0)
        if px <= 0:
            s["last_order_id"] = o["id"]
            continue
        if o["side"] == "buy" and not s["qty"] and s["cash"] > 0:
            notional = s["cash"] * lev
            fee = notional * fee_r
            s["cash"] -= fee
            s["qty"] = (s["cash"] * lev) / px
            s["entry"] = px
            s["fees_paid"] += fee
            s["last_funding"] = _funding_slot(now)
            _trade("开多", px, s["qty"], fee, -fee, s["cash"], o["id"], f"{lev:g} 倍杠杆，跟随部署 #{o['deployment_id']} 买入")
        elif o["side"] == "sell" and s["qty"]:
            gross = s["qty"] * (px - s["entry"])
            fee = s["qty"] * px * fee_r
            s["cash"] += gross - fee
            s["fees_paid"] += fee
            _trade("平多", px, s["qty"], fee, gross - fee, s["cash"], o["id"], "跟随源策略卖出")
            s["qty"], s["entry"] = 0.0, None
        s["last_order_id"] = o["id"]
    # 2) 资金费（持仓期间每 8 小时）
    if s["qty"] and price:
        slot = _funding_slot(now)
        if s.get("last_funding") and slot > s["last_funding"]:
            n = int((pd.Timestamp(slot) - pd.Timestamp(s["last_funding"])) / pd.Timedelta(hours=8))
            f = s["qty"] * price * float(c["funding_8h"]) * n
            s["cash"] -= f
            s["funding_paid"] += f
            _trade("资金费", price, s["qty"], 0.0, -f, s["cash"], None, f"{n} 期 × {c['funding_8h'] * 100:.2f}%")
        s["last_funding"] = slot
        # 3) 逐仓强平
        lp = liq_price(s["entry"], lev, float(c["maint_margin"]))
        if price <= lp:
            loss = s["cash"]
            s["cash"] = 0.0
            _trade("强平", price, s["qty"], 0.0, -loss, 0.0, None, f"价格 {price:,.2f} ≤ 强平价 {lp:,.2f}")
            s["qty"], s["entry"] = 0.0, None
    db.set_setting("lev_shadow_state", s)
    # 4) 每小时记一次权益
    if price:
        hour = now.replace(minute=0, second=0, microsecond=0).isoformat()
        db.ex("INSERT OR REPLACE INTO lev_shadow_equity(ts,equity,price) VALUES(?,?,?)",
              (hour, round(equity(s, price), 2), price))
    return s


def _funding_slot(now: datetime) -> str:
    h = (now.hour // 8) * 8
    return now.replace(hour=h, minute=0, second=0, microsecond=0).isoformat()


def summary(price: float | None) -> dict:
    c = get_config()
    s = get_state()
    eq = equity(s, price)
    return {"equity": eq, "margin0": c["margin0"], "pnl": eq - c["margin0"],
            "pnl_pct": (eq / c["margin0"] - 1) * 100, "qty": s["qty"], "entry": s["entry"],
            "liq": liq_price(s["entry"], c["leverage"], c["maint_margin"]) if s["qty"] else None,
            "leverage": c["leverage"], "source": c["source_deployment"], "fees": s["fees_paid"],
            "funding": s["funding_paid"], "started_at": s["started_at"]}


def trades(limit: int = 50) -> list[dict]:
    ensure_tables()
    return db.q("SELECT * FROM lev_shadow_trades ORDER BY id DESC LIMIT ?", (limit,))
