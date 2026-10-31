"""2 倍杠杆模拟账户（本地永续合约记账，不连接交易所、不下任何订单）。

2026-10-31 起跟随全部 BTC 策略，并且多空两个方向：
- 多头腿：每个 BTC 模拟盘部署（含实验位）的真实 Paper 成交都跟。某策略从空仓买入 → 开一条多头腿，
  名义价值 = 杠杆 × 起始保证金 × 该策略额度占全部 BTC 策略额度的比例；卖出多少比例的现货 → 平同样比例的多头腿。
- 空头腿：shortside 自动资格通过的策略，用规则上下翻转后的「做空版」：熊市（BTC 日线 < 200 日均线）里做空版入场规则成立时开空，
  做空版退出规则 / 止损 / 止盈 / 最长持仓时平空，
  名义价值的算法与多头腿相同（同一个策略同时只有多或空其中一条腿）。
- 成本：手续费 0.07% × 名义价值；资金费每 8 小时 0.01% × 名义价值（多头付、空头收）。
- 强平（整个账户）：权益 ≤ 维持保证金率 × 全部名义价值时，全部腿按当前价平仓。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from .. import db

DEFAULTS = {"enabled": True, "leverage": 2.0, "margin0": 10000.0, "fee_rate": 0.0007, "funding_8h": 0.0001,
            "maint_margin": 0.004, "shorts": True}

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


def _btc_deps() -> list[dict]:
    from . import runner as pr
    return [pr.get_deployment(r["id"]) for r in db.q(
        "SELECT id FROM paper_deployments WHERE broker='alpaca_paper' AND is_test=0 ORDER BY id")]


def get_state() -> dict:
    c = get_config()
    s = db.get_setting("lev_shadow_state")
    if not s or "legs" not in s:      # 旧格式（只跟随一个策略）：保留起点，从起点回放全部 BTC 策略的成交
        started = (s or {}).get("started_at") or db.now_iso()
        s = {"cash": c["margin0"], "legs": {}, "spot": {}, "last_order_id": 0, "last_funding": None,
             "started_at": started, "funding_paid": 0.0, "fees_paid": 0.0, "short_bar": {}, "v": 2}
        db.set_setting("lev_shadow_state", s)
    return s


def _weight(did: int) -> float:
    deps = [d for d in _btc_deps() if d["status"] in ("running", "paused_entries")]
    tot = sum(float(d["config"].get("budget_usd") or 0) for d in deps)
    mine = next((float(d["config"].get("budget_usd") or 0) for d in deps if d["id"] == did), 0.0)
    if tot <= 0 or mine <= 0:
        return 1.0 / max(len(deps), 1)
    return mine / tot


def equity(s: dict, price: float | None) -> float:
    e = s["cash"]
    if price:
        for lg in s["legs"].values():
            e += lg["qty"] * (price - lg["entry"]) * (1 if lg["side"] == "long" else -1)
    return e


def _trade(kind, price, qty, fee, pnl, eq, oid=None, note="", ts=None) -> None:
    db.ex("INSERT INTO lev_shadow_trades(ts,kind,price,qty,notional,fee,pnl,equity_after,src_order_id,note) "
          "VALUES(?,?,?,?,?,?,?,?,?,?)", (ts or db.now_iso(), kind, price, qty, qty * price, fee, pnl, eq, oid, note))


def _label(did: int) -> str:
    from . import runner as pr
    return pr.dep_label(did)


def _open(s, c, key, side, did, px, ts, oid=None, why="") -> None:
    if s["cash"] <= 0:
        return
    notional = float(c["leverage"]) * float(c["margin0"]) * _weight(did)
    fee = notional * float(c["fee_rate"])
    s["cash"] -= fee
    s["fees_paid"] += fee
    s["legs"][key] = {"side": side, "qty": notional / px, "entry": px, "did": did, "opened": ts}
    _trade("开多" if side == "long" else "开空", px, notional / px, fee, -fee, s["cash"], oid,
           f"{_label(did)}：{why}", ts)


def _close(s, c, key, px, ts, frac=1.0, oid=None, why="") -> None:
    lg = s["legs"].get(key)
    if not lg:
        return
    q = lg["qty"] * min(max(frac, 0.0), 1.0)
    sign = 1 if lg["side"] == "long" else -1
    gross = q * (px - lg["entry"]) * sign
    fee = q * px * float(c["fee_rate"])
    s["cash"] += gross - fee
    s["fees_paid"] += fee
    _trade("平多" if lg["side"] == "long" else "平空", px, q, fee, gross - fee, s["cash"], oid,
           f"{_label(lg['did'])}：{why}", ts)
    lg["qty"] -= q
    if lg["qty"] <= 1e-12 or frac >= 0.999:
        s["legs"].pop(key, None)


def _follow_fills(s: dict, c: dict) -> None:
    """多头腿：跟随全部 BTC 部署的真实成交（含旧成交回放，从账户起点开始）。"""
    rows = db.q("SELECT o.* FROM paper_orders o JOIN paper_deployments d ON d.id=o.deployment_id "
                "WHERE d.broker='alpaca_paper' AND o.is_test=0 AND o.status IN ('filled','reconciled') "
                "AND o.filled_qty>0 AND o.id>? AND o.created_at>=? ORDER BY o.id",
                (s["last_order_id"], s["started_at"][:19]))
    for o in rows:
        did, px, q = o["deployment_id"], float(o["filled_price"] or 0), float(o["filled_qty"])
        s["last_order_id"] = o["id"]
        if px <= 0:
            continue
        key = f"L{did}"
        before = float(s["spot"].get(str(did), 0.0))
        if o["side"] == "buy":
            s["spot"][str(did)] = before + q
            sk = f"S{did}"
            if sk in s["legs"]:
                _close(s, c, sk, px, o["created_at"], why="策略买入，先平掉做空腿")
            if key not in s["legs"]:
                _open(s, c, key, "long", did, px, o["created_at"], o["id"], "跟随真实买入")
        else:
            s["spot"][str(did)] = max(before - q, 0.0)
            if key in s["legs"] and before > 0:
                _close(s, c, key, px, o["created_at"], frac=q / before, oid=o["id"], why="跟随真实卖出")


def _shorts(s: dict, c: dict, df5: pd.DataFrame | None, price: float | None, now: datetime) -> None:
    """空头腿：自动资格通过的策略，在本地按镜像规则开平空。"""
    from .. import shortside
    if not c.get("shorts") or df5 is None or df5.empty or not price:
        return
    for dep in _btc_deps():
        if dep["status"] not in ("running", "paused_entries"):
            continue
        q = shortside.status(dep["version_id"])
        key = f"S{dep['id']}"
        if not q or not q.get("ok"):
            if key in s["legs"]:
                _close(s, c, key, price, now.isoformat(), why="做空资格取消，平空")
            continue
        try:
            sig = shortside.live_signal(dep, df5, now)
        except Exception:  # noqa: BLE001
            continue
        if not sig:
            continue
        lg = s["legs"].get(key)
        if lg:   # 止损 / 止盈每轮按最新价检查；信号平空只在新K线收盘时
            if sig["sl"] and price >= lg["entry"] * (1 + sig["sl"] / 100):
                _close(s, c, key, price, now.isoformat(), why=f"做空止损 {sig['sl']:g}%")
                continue
            if sig["tp"] and price <= lg["entry"] * (1 - sig["tp"] / 100):
                _close(s, c, key, price, now.isoformat(), why=f"做空止盈 {sig['tp']:g}%")
                continue
        if s["short_bar"].get(str(dep["id"])) == sig["bar"]:
            continue
        s["short_bar"][str(dep["id"])] = sig["bar"]
        lg = s["legs"].get(key)
        if lg and sig["cover"]:
            _close(s, c, key, price, now.isoformat(), why="做空版退出规则成立，平空")
        elif lg and sig["max_hold"] and (pd.Timestamp(now) - pd.Timestamp(lg["opened"])) >= \
                pd.Timedelta(minutes=sig["tf"] * sig["max_hold"]):
            _close(s, c, key, price, now.isoformat(), why="达到最长持仓，平空")
        elif not lg and sig["open"] and f"L{dep['id']}" not in s["legs"]:
            _open(s, c, key, "short", dep["id"], price, now.isoformat(), None, "做空版入场规则成立（熊市），开空")


def update(price: float | None, now: datetime | None = None, df5: pd.DataFrame | None = None) -> dict:
    """每轮服务循环调用：跟随全部 BTC 策略成交、做空腿、资金费、强平、记录权益。幂等。"""
    c = get_config()
    if not c["enabled"]:
        return {}
    ensure_tables()
    s = get_state()
    now = now or datetime.now(timezone.utc)
    _follow_fills(s, c)
    _shorts(s, c, df5, price, now)
    if s["legs"] and price:
        slot = _funding_slot(now)
        if s.get("last_funding") and slot > s["last_funding"]:
            n = int((pd.Timestamp(slot) - pd.Timestamp(s["last_funding"])) / pd.Timedelta(hours=8))
            f = sum(lg["qty"] * price * float(c["funding_8h"]) * n * (1 if lg["side"] == "long" else -1)
                    for lg in s["legs"].values())
            s["cash"] -= f
            s["funding_paid"] += f
            _trade("资金费", price, 0.0, 0.0, -f, s["cash"], None, f"{n} 期 × {c['funding_8h'] * 100:.2f}%（多头付、空头收）")
        s["last_funding"] = slot
        notional = sum(lg["qty"] for lg in s["legs"].values()) * price
        eq = equity(s, price)
        if eq <= float(c["maint_margin"]) * notional:
            for k in list(s["legs"]):
                _close(s, c, k, price, now.isoformat(), why="账户强平")
            _trade("强平", price, 0.0, 0.0, 0.0, s["cash"], None, f"权益 {eq:,.2f} ≤ 维持保证金")
    elif not s["legs"]:
        s["last_funding"] = _funding_slot(now)
    db.set_setting("lev_shadow_state", s)
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
    legs = []
    for k, lg in s["legs"].items():
        legs.append({"key": k, "label": _label(lg["did"]), "side": "做多" if lg["side"] == "long" else "做空",
                     "qty": lg["qty"], "entry": lg["entry"], "opened": lg["opened"],
                     "pnl": (lg["qty"] * (price - lg["entry"]) * (1 if lg["side"] == "long" else -1)) if price else None})
    net = sum(lg["qty"] * (1 if lg["side"] == "long" else -1) for lg in s["legs"].values())
    notional = sum(lg["qty"] for lg in s["legs"].values()) * (price or 0)
    liq = None
    if price and notional:      # 价格整体朝不利方向走到哪里会强平（按净头寸近似）
        if net:
            liq = price - (eq - float(c["maint_margin"]) * notional) / net
    return {"equity": eq, "margin0": c["margin0"], "pnl": eq - c["margin0"], "pnl_pct": (eq / c["margin0"] - 1) * 100,
            "qty": net, "entry": None, "liq": liq if liq and liq > 0 else None, "leverage": c["leverage"],
            "source": None, "fees": s["fees_paid"], "funding": s["funding_paid"], "started_at": s["started_at"],
            "legs": legs, "n_long": sum(1 for x in legs if x["side"] == "做多"),
            "n_short": sum(1 for x in legs if x["side"] == "做空")}


def trades(limit: int = 50) -> list[dict]:
    ensure_tables()
    return db.q("SELECT * FROM lev_shadow_trades ORDER BY id DESC LIMIT ?", (limit,))
