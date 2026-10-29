"""美股模拟盘台账：按策略（部署）分别记账，券商订单可以合并，但每一笔成交都能拆回到具体策略。

三张表：
- us_orders：发给券商的订单（同一交易日、同一标的的所有策略意图净额成一笔；被完全对冲时不发券商单）。
- us_order_allocs：每笔券商订单里各策略的份额（买：金额；卖：股数），成交后写回各自分到的股数和价格。
- us_ledger：按策略的成交台账（fill = 券商成交分到的部分；cross = 同一天买卖相抵、内部按同一价格成交；
  migrate = 迁移时的期初持仓）。持仓、成本（移动平均成本）、已实现盈亏都只从这张表算。

对账：每个标的「各策略台账股数之和」必须等于券商实际持仓（允许 0.01% 或 1e-6 股的误差）；不一致时该标的暂停下单。
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict

from .. import db

log = logging.getLogger("aiquant.us_ledger")
OPEN = ("new", "accepted", "pending_new", "partially_filled", "submitted")
FINAL = ("filled", "canceled", "expired", "rejected", "done_for_day")
QTY_EPS = 1e-6

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS us_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, client_order_id TEXT UNIQUE, symbol TEXT NOT NULL, side TEXT NOT NULL,
  notional REAL, qty REAL, status TEXT NOT NULL, filled_qty REAL NOT NULL DEFAULT 0, filled_price REAL,
  trade_date TEXT, ref_price REAL, allocated INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT,
  is_test INTEGER NOT NULL DEFAULT 0, raw_json TEXT);
CREATE TABLE IF NOT EXISTS us_order_allocs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, deployment_id INTEGER NOT NULL, symbol TEXT NOT NULL,
  side TEXT NOT NULL, req_notional REAL, req_qty REAL, ref_price REAL, purpose TEXT, reason TEXT,
  filled_qty REAL NOT NULL DEFAULT 0, filled_price REAL, status TEXT NOT NULL DEFAULT 'open', trade_date TEXT,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS us_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT, deployment_id INTEGER NOT NULL, symbol TEXT NOT NULL, ts TEXT NOT NULL,
  side TEXT NOT NULL, qty REAL NOT NULL, price REAL NOT NULL, source TEXT NOT NULL, order_id INTEGER,
  alloc_id INTEGER, note TEXT);
CREATE INDEX IF NOT EXISTS ix_us_ledger_dep ON us_ledger(deployment_id, symbol);
CREATE INDEX IF NOT EXISTS ix_us_allocs_order ON us_order_allocs(order_id);
"""


def ensure_tables() -> None:
    with db.tx() as c:
        c.executescript(SCHEMA_SQL)


# ---------------------------------------------------------------------------
# 持仓 / 成本 / 盈亏（只从台账计算）
# ---------------------------------------------------------------------------

def position(did: int, symbol: str | None = None) -> dict:
    """某个策略（某个标的）的持仓：qty、avg_cost、cost_basis、realized（移动平均成本法）、buys/sells 次数。"""
    ensure_tables()
    sql = "SELECT * FROM us_ledger WHERE deployment_id=?" + (" AND symbol=?" if symbol else "") + " ORDER BY id"
    rows = db.q(sql, (did, symbol) if symbol else (did,))
    out: dict[str, dict] = {}
    for r in rows:
        p = out.setdefault(r["symbol"], {"symbol": r["symbol"], "qty": 0.0, "avg_cost": 0.0, "realized": 0.0,
                                         "buys": 0, "sells": 0})
        q, px = float(r["qty"]), float(r["price"])
        if r["side"] == "buy":
            tot = p["qty"] + q
            p["avg_cost"] = (p["avg_cost"] * p["qty"] + px * q) / tot if tot > 0 else 0.0
            p["qty"] = tot
            p["buys"] += 1
        else:
            q = min(q, p["qty"])
            p["realized"] += (px - p["avg_cost"]) * q
            p["qty"] -= q
            p["sells"] += 1
            if p["qty"] <= QTY_EPS:
                p["qty"], p["avg_cost"] = 0.0, 0.0
    for p in out.values():
        p["cost_basis"] = p["qty"] * p["avg_cost"]
    if symbol:
        return out.get(symbol, {"symbol": symbol, "qty": 0.0, "avg_cost": 0.0, "realized": 0.0, "buys": 0,
                                "sells": 0, "cost_basis": 0.0})
    return out


def with_marks(p: dict, price: float | None) -> dict:
    p = dict(p)
    if price:
        p["market_value"] = p["qty"] * price
        p["unrealized"] = (price - p["avg_cost"]) * p["qty"]
    return p


def symbol_totals(dids: list[int] | None = None) -> dict[str, float]:
    """每个标的各策略台账股数之和（对账用）。"""
    ensure_tables()
    rows = db.q("SELECT deployment_id, symbol, side, qty FROM us_ledger ORDER BY id")
    tot: dict[str, float] = defaultdict(float)
    for r in rows:
        if dids is not None and r["deployment_id"] not in dids:
            continue
        tot[r["symbol"]] += float(r["qty"]) if r["side"] == "buy" else -float(r["qty"])
    return {k: max(v, 0.0) for k, v in tot.items()}


def by_strategy(symbol: str) -> dict[int, float]:
    ensure_tables()
    out: dict[int, float] = defaultdict(float)
    for r in db.q("SELECT deployment_id, side, qty FROM us_ledger WHERE symbol=?", (symbol,)):
        out[r["deployment_id"]] += float(r["qty"]) if r["side"] == "buy" else -float(r["qty"])
    return {k: v for k, v in out.items() if abs(v) > QTY_EPS}


def reconcile(broker_positions: dict[str, float], symbols: list[str] | None = None) -> dict[str, dict]:
    """券商持仓 vs 各策略台账之和。返回 {标的: {broker, ledger, diff, ok, by_strategy}}。"""
    led = symbol_totals()
    syms = sorted(set(symbols or []) | set(k for k, v in led.items() if v > QTY_EPS)
                  | set(k for k, v in broker_positions.items() if abs(v) > QTY_EPS))
    out = {}
    for s in syms:
        b, l_ = float(broker_positions.get(s, 0.0)), float(led.get(s, 0.0))
        tol = max(QTY_EPS, abs(l_) * 1e-4)
        out[s] = {"broker": b, "ledger": l_, "diff": b - l_, "ok": abs(b - l_) <= tol, "by_strategy": by_strategy(s)}
    return out


def has_open(did: int | None = None, symbol: str | None = None) -> bool:
    ensure_tables()
    sql = ("SELECT a.id FROM us_order_allocs a WHERE a.status='open'"
           + (" AND a.deployment_id=?" if did is not None else "") + (" AND a.symbol=?" if symbol else ""))
    args = tuple(x for x in (did, symbol) if x is not None)
    return db.q1(sql + " LIMIT 1", args) is not None


# ---------------------------------------------------------------------------
# 合并下单与成交拆分
# ---------------------------------------------------------------------------

def plan_net(intents: list[dict], ref_price: float) -> dict:
    """同一标的、同一交易日的全部策略意图 → 一笔净额券商订单 + 内部对冲部分。
    intents：[{deployment_id, side, notional（买）| qty（卖）, purpose, reason}]。
    买单按金额，卖单按股数；买单折算股数用参考价（前一日收盘）。返回
    {"side": buy|sell|None, "notional"/"qty": 券商订单数量, "buy_qty_est", "sell_qty", "cross_qty"}。"""
    buy_usd = sum(float(i["notional"]) for i in intents if i["side"] == "buy")
    sell_qty = sum(float(i["qty"]) for i in intents if i["side"] == "sell")
    buy_qty_est = buy_usd / ref_price if ref_price > 0 else 0.0
    cross = min(buy_qty_est, sell_qty)
    if buy_qty_est > sell_qty + QTY_EPS:
        return {"side": "buy", "notional": round(buy_usd - sell_qty * ref_price, 2), "buy_qty_est": buy_qty_est,
                "sell_qty": sell_qty, "cross_qty": cross}
    if sell_qty > buy_qty_est + QTY_EPS:
        return {"side": "sell", "qty": round(sell_qty - buy_qty_est, 6), "buy_qty_est": buy_qty_est,
                "sell_qty": sell_qty, "cross_qty": cross}
    return {"side": None, "buy_qty_est": buy_qty_est, "sell_qty": sell_qty, "cross_qty": cross}


def create_order(symbol: str, plan: dict, intents: list[dict], trade_date: str, ref_price: float, coid: str | None,
                 is_test: bool = False) -> int:
    """登记一笔（净额）券商订单及其各策略份额。完全对冲时 coid=None、状态 internal（不发券商）。"""
    ensure_tables()
    now = db.now_iso()
    with db.tx() as c:
        cur = c.execute("INSERT INTO us_orders(client_order_id,symbol,side,notional,qty,status,trade_date,ref_price,"
                        "created_at,updated_at,is_test) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (coid, symbol, plan["side"] or "cross", plan.get("notional"), plan.get("qty"),
                         "new" if coid else "internal", trade_date, ref_price, now, now, 1 if is_test else 0))
        oid = cur.lastrowid
        for i in intents:
            c.execute("INSERT INTO us_order_allocs(order_id,deployment_id,symbol,side,req_notional,req_qty,ref_price,"
                      "purpose,reason,trade_date,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (oid, i["deployment_id"], symbol, i["side"], i.get("notional"), i.get("qty"), ref_price,
                       i.get("purpose"), (i.get("reason") or "")[:300], trade_date, now))
    return oid


def update_order(oid: int, status: str, filled_qty: float = 0.0, filled_price: float | None = None,
                 raw: dict | None = None) -> None:
    db.ex("UPDATE us_orders SET status=?, filled_qty=?, filled_price=?, raw_json=COALESCE(?, raw_json), updated_at=? "
          "WHERE id=?", (status, float(filled_qty or 0), filled_price,
                         json.dumps(raw, ensure_ascii=False, default=str)[:3000] if raw else None, db.now_iso(), oid))


def allocate(oid: int, fallback_price: float | None = None) -> list[dict]:
    """订单终结（成交 / 部分成交后过期 / 被拒 / 内部对冲）后，把成交拆回各策略并写台账。幂等。
    规则：
    - 净买单：买方合计股数 = 券商成交股数 + 卖方被对冲的股数，按各自金额比例分；卖方按请求股数全部内部成交。
      券商只成交一部分时，卖方仍按请求全部对冲（对冲不依赖券商），买方少分到的部分次日补足。
    - 净卖单：卖方合计股数 = 券商成交股数 + 买方折算股数，按各自请求股数比例分；买方按折算股数全部内部成交。
    - 内部对冲（没有券商单）：双方按参考价全部成交。
    - 成交价：券商成交均价；没有券商成交时用参考价。"""
    ensure_tables()
    o = db.q1("SELECT * FROM us_orders WHERE id=?", (oid,))
    if not o or o["allocated"]:
        return []
    allocs = db.q("SELECT * FROM us_order_allocs WHERE order_id=? ORDER BY id", (oid,))
    filled = float(o["filled_qty"] or 0)
    px = float(o["filled_price"] or 0) or float(o["ref_price"] or 0) or float(fallback_price or 0)
    buys = [a for a in allocs if a["side"] == "buy"]
    sells = [a for a in allocs if a["side"] == "sell"]
    ref = float(o["ref_price"] or px or 1.0)
    buy_est = {a["id"]: float(a["req_notional"] or 0) / ref for a in buys}
    sell_req = {a["id"]: float(a["req_qty"] or 0) for a in sells}
    give: dict[int, float] = {}
    if o["side"] == "buy":
        for a in sells:
            give[a["id"]] = sell_req[a["id"]]
        total = filled + sum(sell_req.values())
        usd = sum(float(a["req_notional"] or 0) for a in buys) or 1.0
        _split(buys, total, lambda a: float(a["req_notional"] or 0) / usd, give)
    elif o["side"] == "sell":
        for a in buys:
            give[a["id"]] = buy_est[a["id"]]
        total = filled + sum(buy_est.values())
        sq = sum(sell_req.values()) or 1.0
        _split(sells, total, lambda a: sell_req[a["id"]] / sq, give)
    else:   # 完全内部对冲
        for a in buys:
            give[a["id"]] = buy_est[a["id"]]
        for a in sells:
            give[a["id"]] = sell_req[a["id"]]
    out = []
    now = db.now_iso()
    with db.tx() as c:
        for a in allocs:
            q = round(give.get(a["id"], 0.0), 9)
            src = "fill" if (a["side"] == o["side"]) else "cross"
            if q > QTY_EPS:
                c.execute("INSERT INTO us_ledger(deployment_id,symbol,ts,side,qty,price,source,order_id,alloc_id,note) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?)", (a["deployment_id"], a["symbol"], now, a["side"], q, px, src,
                                                          oid, a["id"], a["purpose"]))
            req = float(a["req_qty"] or 0) if a["side"] == "sell" else buy_est.get(a["id"], 0.0)
            st = "filled" if q >= req * 0.98 - QTY_EPS and q > QTY_EPS else ("partial" if q > QTY_EPS else "unfilled")
            c.execute("UPDATE us_order_allocs SET filled_qty=?, filled_price=?, status=? WHERE id=?",
                      (q, px if q > QTY_EPS else None, st, a["id"]))
            out.append({"alloc_id": a["id"], "deployment_id": a["deployment_id"], "side": a["side"], "qty": q,
                        "price": px, "status": st, "purpose": a["purpose"], "source": src})
        c.execute("UPDATE us_orders SET allocated=1, updated_at=? WHERE id=?", (now, oid))
    return out


def _split(rows: list[dict], total: float, share, give: dict) -> None:
    """按比例分配股数，四舍五入到 1e-9 股，余数给最后一个，保证合计严格等于 total。"""
    if not rows:
        return
    acc = 0.0
    for a in rows[:-1]:
        q = round(total * share(a), 9)
        give[a["id"]] = q
        acc += q
    give[rows[-1]["id"]] = round(total - acc, 9)


def record_opening(did: int, symbol: str, qty: float, price: float, note: str) -> int | None:
    """迁移时的期初持仓（没有订单）。已有同一部署同一标的的期初记录时不重复写。"""
    ensure_tables()
    if qty <= QTY_EPS:
        return None
    if db.q1("SELECT id FROM us_ledger WHERE deployment_id=? AND symbol=? AND source='migrate'", (did, symbol)):
        return None
    return db.ex("INSERT INTO us_ledger(deployment_id,symbol,ts,side,qty,price,source,note) VALUES(?,?,?,?,?,?,?,?)",
                 (did, symbol, db.now_iso(), "buy", float(qty), float(price), "migrate", note[:300]))


def open_orders() -> list[dict]:
    ensure_tables()
    return db.q(f"SELECT * FROM us_orders WHERE status IN ({','.join('?' * len(OPEN))}) ORDER BY id", OPEN)


def unallocated_final() -> list[dict]:
    ensure_tables()
    return db.q("SELECT * FROM us_orders WHERE allocated=0 AND status NOT IN "
                f"({','.join('?' * len(OPEN))}) ORDER BY id", OPEN)


def allocs_for(did: int, limit: int = 50) -> list[dict]:
    ensure_tables()
    return db.q("SELECT a.*, o.client_order_id, o.status AS order_status FROM us_order_allocs a "
                "LEFT JOIN us_orders o ON o.id=a.order_id WHERE a.deployment_id=? ORDER BY a.id DESC LIMIT ?",
                (did, limit))


def entry_filled_usd(did: int, symbol: str, since_date: str) -> float:
    """本轮持仓（自 since_date 那次买入起）该策略已分到的买入金额。"""
    ensure_tables()
    rows = db.q("SELECT l.qty, l.price FROM us_ledger l LEFT JOIN us_order_allocs a ON a.id=l.alloc_id "
                "WHERE l.deployment_id=? AND l.symbol=? AND l.side='buy' AND (a.trade_date>=? OR l.source='migrate')",
                (did, symbol, since_date))
    return float(sum(float(r["qty"]) * float(r["price"]) for r in rows))
