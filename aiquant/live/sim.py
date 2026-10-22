"""虚拟交易（纸上模拟）引擎：完全在本地记账，不向任何券商发送订单。

- 实时模式：后台每秒运行；在所选周期的K线收盘时判断买卖信号；止损/止盈每秒按买一/卖一价检查。
- 快速回放：用同一套规则把最近的历史K线逐根"重放"，几秒内看到结果，不需要按天等待。
- 成交价：买入用卖一价（ask），卖出用买一价（bid），再加滑点；手续费按成交金额计。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .. import db
from ..engine import indicators as ind
from ..engine.signals import group_signal
from ..strategy.schema import StrategySchema, validate


def _c(l, op, r, mult=1.0):
    return {"left": {"ref": l}, "op": op, "right": ({"ref": r, "mult": mult} if isinstance(r, str) else {"value": r})}


PRESETS: dict[str, dict] = {
    "trend": {
        "label": "趋势跟随（新手推荐）",
        "plain": "短期均线从下往上穿过长期均线（金叉），并且价格在 50 均线上方时买入；"
                 "出现死叉时卖出。亏 1% 自动止损，赚 2% 自动止盈。",
        "schema": {"name": "趋势跟随", "indicators": [
            {"id": "ema9", "type": "EMA", "params": {"period": 9}},
            {"id": "ema21", "type": "EMA", "params": {"period": 21}},
            {"id": "ema50", "type": "EMA", "params": {"period": 50}}],
            "entry": {"mode": "all", "conditions": [_c("ema9", "cross_above", "ema21"), _c("close", ">", "ema50")]},
            "exit": {"mode": "any", "conditions": [_c("ema9", "cross_below", "ema21")]},
            "stop_loss_pct": 1.0, "take_profit_pct": 2.0},
    },
    "reversion": {
        "label": "超跌反弹",
        "plain": "价格跌破布林带下轨且 RSI 低于 30（跌过头了）时买入；RSI 回到 55 以上或价格回到中轨时卖出。"
                 "亏 1% 自动止损。",
        "schema": {"name": "超跌反弹", "indicators": [
            {"id": "rsi", "type": "RSI", "params": {"period": 14}},
            {"id": "bb_low", "type": "BB_LOWER", "params": {"period": 20, "std": 2}},
            {"id": "bb_mid", "type": "BB_MID", "params": {"period": 20, "std": 2}}],
            "entry": {"mode": "all", "conditions": [_c("rsi", "<", 30), _c("close", "<", "bb_low")]},
            "exit": {"mode": "any", "conditions": [_c("rsi", ">", 55), _c("close", ">", "bb_mid")]},
            "stop_loss_pct": 1.0},
    },
    "breakout": {
        "label": "突破追涨",
        "plain": "价格突破最近 20 根K线的最高价、且 MACD 在信号线上方时买入；跌破最近 10 根K线最低价时卖出。"
                 "亏 1% 止损；赚到 1.5% 后启动移动止损，从最高点回落 0.8% 离场。",
        "schema": {"name": "突破追涨", "indicators": [
            {"id": "dch", "type": "DONCHIAN_HIGH", "params": {"period": 20}},
            {"id": "dcl", "type": "DONCHIAN_LOW", "params": {"period": 10}},
            {"id": "macd", "type": "MACD", "params": {"fast": 12, "slow": 26, "signal": 9}},
            {"id": "macd_sig", "type": "MACD_SIGNAL", "params": {"fast": 12, "slow": 26, "signal": 9}}],
            "entry": {"mode": "all", "conditions": [_c("close", ">", "dch"), _c("macd", ">", "macd_sig")]},
            "exit": {"mode": "any", "conditions": [_c("close", "<", "dcl")]},
            "stop_loss_pct": 1.0, "trailing": {"activate_pct": 1.5, "trail_pct": 0.8}},
    },
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sim_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, preset TEXT NOT NULL, tf_sec INTEGER NOT NULL,
    cash0 REAL NOT NULL, fee_bps REAL NOT NULL, slip_bps REAL NOT NULL,
    status TEXT NOT NULL, is_replay INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL,
    cash REAL NOT NULL, qty REAL NOT NULL DEFAULT 0, entry_price REAL, entry_ts TEXT, peak REAL,
    last_bar TEXT, started_at TEXT NOT NULL, stopped_at TEXT, result_json TEXT
);
CREATE TABLE IF NOT EXISTS sim_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL, side TEXT NOT NULL,
    ts TEXT NOT NULL, bar_ts TEXT, price REAL NOT NULL, qty REAL NOT NULL, fee REAL NOT NULL,
    reason TEXT NOT NULL, pnl REAL, cash_after REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sim_trades ON sim_trades(session_id);
CREATE TABLE IF NOT EXISTS sim_equity (
    session_id INTEGER NOT NULL, ts TEXT NOT NULL, equity REAL NOT NULL
);
"""


def ensure_tables() -> None:
    with db.tx() as c:
        c.executescript(SCHEMA_SQL)


def schema_of(preset: str) -> StrategySchema:
    o, e = validate(PRESETS[preset]["schema"])
    assert not e, e
    return o


def signal_frame(c: pd.DataFrame, s: StrategySchema) -> tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    f = c.copy()
    for i in s.indicators:
        f[i.id] = ind.compute(f, i.type, i.params, i.source).values
    return f, group_signal(f, s.entry), group_signal(f, s.exit)


def _iso(t) -> str:
    return pd.Timestamp(t).tz_convert("UTC").isoformat() if pd.Timestamp(t).tzinfo else pd.Timestamp(t).tz_localize(
        "UTC").isoformat()


# ---------------------------------------------------------------------------
# 账户记账（实时与回放共用）
# ---------------------------------------------------------------------------

@dataclass
class Book:
    sid: int
    cash: float
    qty: float
    fee_bps: float
    slip_bps: float
    entry_price: float | None = None
    entry_ts: str | None = None
    peak: float | None = None

    def buy(self, ts, bar_ts, ask: float, reason: str) -> dict | None:
        px = ask * (1 + self.slip_bps / 1e4)
        qty = self.cash * 0.999 / (px * (1 + self.fee_bps / 1e4))
        if qty * px < 10:
            return None
        fee = qty * px * self.fee_bps / 1e4
        self.cash -= qty * px + fee
        self.qty, self.entry_price, self.entry_ts, self.peak = qty, px, _iso(ts), px
        self._cost = qty * px + fee
        return self._rec("buy", ts, bar_ts, px, qty, fee, reason, None)

    def sell(self, ts, bar_ts, bid: float, reason: str) -> dict | None:
        if self.qty <= 0:
            return None
        px = bid * (1 - self.slip_bps / 1e4)
        qty = self.qty
        fee = qty * px * self.fee_bps / 1e4
        proceeds = qty * px - fee
        cost = getattr(self, "_cost", None) or qty * (self.entry_price or px)
        self.cash += proceeds
        self.qty, self.entry_price, self.entry_ts, self.peak = 0.0, None, None, None
        return self._rec("sell", ts, bar_ts, px, qty, fee, reason, proceeds - cost)

    def _rec(self, side, ts, bar_ts, px, qty, fee, reason, pnl) -> dict:
        r = {"session_id": self.sid, "side": side, "ts": _iso(ts), "bar_ts": _iso(bar_ts) if bar_ts is not None else None,
             "price": float(px), "qty": float(qty), "fee": float(fee), "reason": reason,
             "pnl": None if pnl is None else float(pnl), "cash_after": float(self.cash)}
        return r

    def stop_check(self, s: StrategySchema, bid: float, high: float | None = None) -> str | None:
        """按卖出价检查止损/止盈/移动止损。high 为可用的最高价（用于更新峰值）。"""
        if self.qty <= 0 or self.entry_price is None:
            return None
        ep = self.entry_price
        if s.stop_loss_pct and bid <= ep * (1 - s.stop_loss_pct / 100):
            return f"止损（亏损达到 {s.stop_loss_pct:g}%）"
        if s.trailing and self.peak and self.peak >= ep * (1 + s.trailing.activate_pct / 100):
            if bid <= self.peak * (1 - s.trailing.trail_pct / 100):
                return f"移动止损（从最高点回落 {s.trailing.trail_pct:g}%）"
        if s.take_profit_pct and bid >= ep * (1 + s.take_profit_pct / 100):
            return f"止盈（盈利达到 {s.take_profit_pct:g}%）"
        if high is not None:
            self.peak = max(self.peak or ep, high)
        return None


def _save_trade(t: dict) -> None:
    db.ex("INSERT INTO sim_trades(session_id,side,ts,bar_ts,price,qty,fee,reason,pnl,cash_after) VALUES(?,?,?,?,?,?,?,?,?,?)",
          (t["session_id"], t["side"], t["ts"], t["bar_ts"], t["price"], t["qty"], t["fee"], t["reason"], t["pnl"],
           t["cash_after"]))


def _save_book(b: Book, last_bar: str | None = None) -> None:
    db.ex("UPDATE sim_sessions SET cash=?, qty=?, entry_price=?, entry_ts=?, peak=?, last_bar=COALESCE(?, last_bar) "
          "WHERE id=?", (b.cash, b.qty, b.entry_price, b.entry_ts, b.peak, last_bar, b.sid))


ENTRY_REASON = {"trend": "金叉且价格在 50 均线上方", "reversion": "跌破布林下轨且 RSI<30（超卖）",
                "breakout": "突破 20 根K线高点且 MACD 向上"}
EXIT_REASON = {"trend": "死叉（短期均线下穿长期均线）", "reversion": "RSI 回升或回到布林中轨",
               "breakout": "跌破 10 根K线低点"}


# ---------------------------------------------------------------------------
# 实时引擎
# ---------------------------------------------------------------------------

class SimEngine:
    def __init__(self):
        self.lock = threading.Lock()
        self._ready = False

    def _init(self):
        if not self._ready:
            ensure_tables()
            self._ready = True

    def active(self) -> dict | None:
        self._init()
        return db.q1("SELECT * FROM sim_sessions WHERE status='running' AND is_replay=0 ORDER BY id DESC LIMIT 1")

    def start(self, preset: str, tf_sec: int, cash: float = 10_000.0, fee_bps: float = 25.0, slip_bps: float = 1.0,
              source: str = "alpaca") -> int:
        self._init()
        if preset not in PRESETS:
            raise ValueError("未知策略")
        with self.lock:
            db.ex("UPDATE sim_sessions SET status='stopped', stopped_at=? WHERE status='running' AND is_replay=0",
                  (db.now_iso(),))
            return db.ex("INSERT INTO sim_sessions(preset,tf_sec,cash0,fee_bps,slip_bps,status,is_replay,source,cash,"
                         "started_at) VALUES(?,?,?,?,?,'running',0,?,?,?)",
                         (preset, tf_sec, cash, fee_bps, slip_bps, source, cash, db.now_iso()))

    def stop(self, sell_all: bool, feed=None) -> None:
        a = self.active()
        if not a:
            return
        with self.lock:
            if sell_all and a["qty"] > 0 and feed is not None and feed.bid:
                b = self._book(a)
                t = b.sell(datetime.now(timezone.utc), None, feed.bid, "手动停止并卖出")
                if t:
                    _save_trade(t)
                _save_book(b)
            db.ex("UPDATE sim_sessions SET status='stopped', stopped_at=? WHERE id=?", (db.now_iso(), a["id"]))

    def _book(self, a: dict) -> Book:
        b = Book(a["id"], a["cash"], a["qty"], a["fee_bps"], a["slip_bps"], a["entry_price"], a["entry_ts"], a["peak"])
        if b.qty > 0:
            last_buy = db.q1("SELECT price, qty, fee FROM sim_trades WHERE session_id=? AND side='buy' ORDER BY id DESC "
                             "LIMIT 1", (a["id"],))
            if last_buy:
                b._cost = last_buy["price"] * last_buy["qty"] + last_buy["fee"]
        return b

    def step(self, feed) -> None:
        a = self.active()
        if not a or not feed.bid or not feed.ask:
            return
        with self.lock:
            s = schema_of(a["preset"])
            b = self._book(a)
            now = datetime.now(timezone.utc)
            changed = False
            # 每秒：止损/止盈/移动止损
            if b.qty > 0:
                reason = b.stop_check(s, feed.bid, high=feed.bid)
                changed = True
                if reason:
                    t = b.sell(now, None, feed.bid, reason)
                    if t:
                        _save_trade(t)
            # K线收盘：信号判断
            c = feed.candles(a["tf_sec"], 300)
            done = c[c["complete"]] if len(c) else c
            last_bar = None
            if len(done) >= 30:
                bar_ts = _iso(done["ts"].iloc[-1])
                if bar_ts != a["last_bar"]:
                    last_bar = bar_ts
                    f, ent, ex = signal_frame(done.reset_index(drop=True), s)
                    i = len(f) - 1
                    if b.qty > 0 and ex[i]:
                        t = b.sell(now, done["ts"].iloc[-1], feed.bid, EXIT_REASON[a["preset"]])
                        if t:
                            _save_trade(t)
                    elif b.qty <= 0 and ent[i] and not ex[i]:
                        t = b.buy(now, done["ts"].iloc[-1], feed.ask, ENTRY_REASON[a["preset"]])
                        if t:
                            _save_trade(t)
                    changed = True
            if changed or last_bar:
                _save_book(b, last_bar)
            # 每 15 秒记录一次权益
            if int(now.timestamp()) % 15 == 0:
                eq = b.cash + b.qty * feed.bid
                db.ex("INSERT INTO sim_equity(session_id, ts, equity) VALUES(?,?,?)", (a["id"], now.isoformat(), eq))


ENGINE = SimEngine()


# ---------------------------------------------------------------------------
# 快速回放测试
# ---------------------------------------------------------------------------

def replay(preset: str, tf_sec: int, candles: pd.DataFrame, cash: float = 10_000.0, fee_bps: float = 25.0,
           slip_bps: float = 1.0, half_spread_bps: float = 0.5, source: str = "alpaca") -> dict:
    """逐根重放已完成K线。信号在K线收盘后、以下一根K线开盘价成交；止损/止盈用K线内最高/最低价检查（先止损）。"""
    ensure_tables()
    s = schema_of(preset)
    c = candles[candles["complete"]].reset_index(drop=True) if "complete" in candles else candles.reset_index(drop=True)
    if len(c) < 60:
        raise ValueError("可用K线不足 60 根，无法回放")
    sid = db.ex("INSERT INTO sim_sessions(preset,tf_sec,cash0,fee_bps,slip_bps,status,is_replay,source,cash,started_at)"
                " VALUES(?,?,?,?,?,'running',1,?,?,?)", (preset, tf_sec, cash, fee_bps, slip_bps, source, cash,
                                                        db.now_iso()))
    f, ent, ex = signal_frame(c, s)
    b = Book(sid, cash, 0.0, fee_bps, slip_bps)
    hs = half_spread_bps / 1e4
    trades, eq = [], []
    pending = None
    for i in range(len(c)):
        row = c.iloc[i]
        ts = row["ts"]
        if pending and i > 0:
            side, reason, sig_bar = pending
            if side == "buy":
                t = b.buy(ts, sig_bar, row["open"] * (1 + hs), reason)
            else:
                t = b.sell(ts, sig_bar, row["open"] * (1 - hs), reason)
            if t:
                trades.append(t)
            pending = None
        if b.qty > 0:
            ep = b.entry_price
            lo_bid, hi_bid = row["low"] * (1 - hs), row["high"] * (1 - hs)
            reason = None
            if s.stop_loss_pct and lo_bid <= ep * (1 - s.stop_loss_pct / 100):
                reason, px = f"止损（亏损达到 {s.stop_loss_pct:g}%）", min(row["open"] * (1 - hs), ep * (1 - s.stop_loss_pct / 100))
            elif s.trailing and b.peak and b.peak >= ep * (1 + s.trailing.activate_pct / 100) and \
                    lo_bid <= b.peak * (1 - s.trailing.trail_pct / 100):
                reason, px = "移动止损", min(row["open"] * (1 - hs), b.peak * (1 - s.trailing.trail_pct / 100))
            elif s.take_profit_pct and hi_bid >= ep * (1 + s.take_profit_pct / 100):
                reason, px = f"止盈（盈利达到 {s.take_profit_pct:g}%）", max(row["open"] * (1 - hs), ep * (1 + s.take_profit_pct / 100))
            if reason:
                t = b.sell(ts, ts, px, reason)
                trades.append(t)
            else:
                b.peak = max(b.peak or ep, hi_bid)
        if i < len(c) - 1:
            if b.qty > 0 and ex[i]:
                pending = ("sell", EXIT_REASON[preset], ts)
            elif b.qty <= 0 and ent[i] and not ex[i]:
                pending = ("buy", ENTRY_REASON[preset], ts)
        eq.append((ts, b.cash + b.qty * row["close"] * (1 - hs)))
    if b.qty > 0:
        last = c.iloc[-1]
        trades.append(b.sell(last["ts"], last["ts"], last["close"] * (1 - hs), "回放结束平仓"))
        eq[-1] = (eq[-1][0], b.cash)
    for t in trades:
        _save_trade(t)
    e = np.array([x[1] for x in eq])
    peak = np.maximum.accumulate(e)
    sells = [t for t in trades if t["side"] == "sell"]
    wins = [t for t in sells if (t["pnl"] or 0) > 0]
    bh = (c["close"].iloc[-1] * (1 - hs)) / (c["open"].iloc[0] * (1 + hs)) * (1 - fee_bps / 1e4) ** 2 - 1
    res = {"start": _iso(c["ts"].iloc[0]), "end": _iso(c["ts"].iloc[-1]), "bars": len(c),
           "final_equity": float(b.cash), "return_pct": (b.cash / cash - 1) * 100,
           "max_drawdown_pct": float(((e - peak) / peak).min() * 100), "trades": len(sells),
           "win_rate_pct": (len(wins) / len(sells) * 100) if sells else None,
           "fees": float(sum(t["fee"] for t in trades)), "buy_hold_pct": bh * 100}
    db.ex("UPDATE sim_sessions SET status='done', cash=?, qty=0, stopped_at=?, result_json=? WHERE id=?",
          (b.cash, db.now_iso(), json.dumps(res, ensure_ascii=False), sid))
    res["session_id"] = sid
    res["equity"] = [(_iso(t), float(v)) for t, v in eq[:: max(1, len(eq) // 600)]]
    return res


def trades_of(sid: int) -> pd.DataFrame:
    return pd.DataFrame(db.q("SELECT * FROM sim_trades WHERE session_id=? ORDER BY id", (sid,)))
