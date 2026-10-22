"""Paper 交易适配器。

- AlpacaPaperAdapter：只连接 paper-api.alpaca.markets；本版本只读，所有写操作被双重锁拦截（PAPER_ORDERS=0）。
- MockBroker：本地模拟撮合，仅用于测试，所有订单 is_test=1，不会访问任何网络。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .. import config, db
from ..data import alpaca, http


class OrderPermissionLocked(RuntimeError):
    pass


@dataclass
class Account:
    equity: float
    cash: float
    buying_power: float
    status: str
    trading_blocked: bool
    crypto_status: str | None


class AlpacaPaperAdapter:
    name = "alpaca_paper"
    is_test = False

    def __init__(self):
        assert config.ALPACA_PAPER_URL.startswith("https://paper-api."), "Paper 适配器只能指向 Paper 端点"

    # ---- 只读 -----------------------------------------------------------
    def account(self) -> Account | None:
        code, b = alpaca.paper_get("/v2/account")
        if code != 200 or not isinstance(b, dict):
            return None
        return Account(float(b.get("equity", 0)), float(b.get("cash", 0)), float(b.get("buying_power", 0)),
                       b.get("status", ""), bool(b.get("trading_blocked")), b.get("crypto_status"))

    def position(self, symbol: str = "BTC/USD") -> dict | None:
        code, b = alpaca.paper_get("/v2/positions")
        if code != 200 or not isinstance(b, list):
            return None
        for p in b:
            if p.get("symbol") in ("BTCUSD", "BTC/USD"):
                return {"qty": float(p.get("qty", 0)), "market_value": float(p.get("market_value", 0)),
                        "avg_entry_price": float(p.get("avg_entry_price", 0)),
                        "unrealized_pl": float(p.get("unrealized_pl", 0))}
        return {"qty": 0.0, "market_value": 0.0, "avg_entry_price": 0.0, "unrealized_pl": 0.0}

    def asset_rules(self, symbol: str = "BTC/USD") -> dict | None:
        code, b = alpaca.paper_get("/v2/assets/BTC%2FUSD")
        if code != 200 or not isinstance(b, dict):
            code, b = alpaca.paper_get("/v2/assets/BTCUSD")
        if code != 200 or not isinstance(b, dict):
            return None
        return {"tradable": b.get("tradable"), "status": b.get("status"),
                "min_order_size": b.get("min_order_size"), "min_trade_increment": b.get("min_trade_increment"),
                "fractionable": b.get("fractionable")}

    def open_orders(self) -> list[dict] | None:
        code, b = alpaca.paper_get("/v2/orders", {"status": "open"})
        return b if code == 200 and isinstance(b, list) else None

    # ---- 写操作（锁定） ---------------------------------------------------
    def _locked(self, what: str):
        if not http.paper_orders_unlocked():
            raise OrderPermissionLocked(f"{what}被拒绝：Paper 订单权限锁定（PAPER_ORDERS=0），需用户未来单独明确授权。")

    def get_order(self, client_order_id: str) -> dict | None:
        code, b = alpaca.paper_get("/v2/orders:by_client_order_id", {"client_order_id": client_order_id})
        return b if code == 200 and isinstance(b, dict) else None

    def submit_order(self, client_order_id: str, side: str, qty: float, ref_price: float | None = None) -> dict:
        """Paper 市价单（仅在双重锁解除后可用）。提交后最多轮询 20 秒等待成交。"""
        self._locked("提交订单")
        assert config.ALPACA_PAPER_URL == "https://paper-api.alpaca.markets"
        q = f"{qty:.9f}".rstrip("0").rstrip(".")
        r = http.request("POST", f"{config.ALPACA_PAPER_URL}/v2/orders", headers=alpaca._paper_headers(),
                         json_body={"symbol": "BTC/USD", "qty": q, "side": side, "type": "market",
                                    "time_in_force": "gtc", "client_order_id": client_order_id})
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Alpaca Paper 拒绝订单 {r.status_code}: {r.text[:200]}")
        info = r.json()
        for _ in range(20):
            if info.get("status") in ("filled", "canceled", "expired", "rejected"):
                break
            time.sleep(1)
            nxt = self.get_order(client_order_id)
            if nxt:
                info = nxt
        return info

    def cancel_open_orders(self) -> None:
        self._locked("撤单")
        http.request("DELETE", f"{config.ALPACA_PAPER_URL}/v2/orders", headers=alpaca._paper_headers())

    def close_position(self) -> None:
        self._locked("平仓")
        http.request("DELETE", f"{config.ALPACA_PAPER_URL}/v2/positions/BTCUSD", headers=alpaca._paper_headers())


class MockBroker:
    """测试用模拟券商：状态持久化在 settings 表，按给定参考价立即成交（含费用）。"""
    name = "mock"
    is_test = True

    def __init__(self, deployment_id: int, initial_cash: float = 10_000.0, fee_bps: float = 25.0):
        self.key = f"mockbroker::{deployment_id}"
        self.fee_bps = fee_bps
        st = db.get_setting(self.key)
        if st is None:
            st = {"cash": initial_cash, "qty": 0.0, "avg": 0.0, "realized": 0.0, "orders": [], "last_price": None}
            db.set_setting(self.key, st)
        self.offline = False

    def _st(self) -> dict:
        return db.get_setting(self.key)

    def _save(self, st: dict) -> None:
        db.set_setting(self.key, st)

    def mark(self, price: float) -> None:
        st = self._st(); st["last_price"] = price; self._save(st)

    def account(self) -> Account | None:
        if self.offline:
            return None
        st = self._st()
        px = st["last_price"] or st["avg"] or 0
        eq = st["cash"] + st["qty"] * px
        return Account(eq, st["cash"], st["cash"], "ACTIVE(MOCK)", False, "ACTIVE")

    def position(self, symbol: str = "BTC/USD") -> dict | None:
        if self.offline:
            return None
        st = self._st()
        px = st["last_price"] or st["avg"] or 0
        return {"qty": st["qty"], "market_value": st["qty"] * px, "avg_entry_price": st["avg"],
                "unrealized_pl": st["qty"] * (px - st["avg"]), "realized_pl": st["realized"]}

    def asset_rules(self, symbol: str = "BTC/USD") -> dict:
        return {"tradable": True, "status": "active", "min_order_size": "0.0001", "min_trade_increment": "0.000000001",
                "fractionable": True}

    def open_orders(self) -> list[dict]:
        return []

    def submit_order(self, client_order_id: str, side: str, qty: float, ref_price: float | None = None) -> dict:
        st = self._st()
        if any(o["client_order_id"] == client_order_id for o in st["orders"]):
            raise RuntimeError("重复的 client_order_id（MockBroker 拒绝）")
        px = float(ref_price)
        notional = px * qty
        fee = notional * self.fee_bps / 1e4
        if side == "buy":
            if notional + fee > st["cash"] + 1e-9:
                raise RuntimeError("MockBroker：现金不足")
            st["avg"] = (st["avg"] * st["qty"] + px * qty) / (st["qty"] + qty)
            st["qty"] += qty
            st["cash"] -= notional + fee
        else:
            if qty > st["qty"] + 1e-12:
                raise RuntimeError("MockBroker：卖出超过持仓")
            st["realized"] += (px - st["avg"]) * qty - fee
            st["qty"] -= qty
            st["cash"] += notional - fee
            if st["qty"] <= 1e-12:
                st["qty"], st["avg"] = 0.0, 0.0
        st["last_price"] = px
        o = {"client_order_id": client_order_id, "side": side, "qty": qty, "price": px, "fee": fee,
             "status": "filled", "ts": datetime.now(timezone.utc).isoformat()}
        st["orders"].append(o)
        self._save(st)
        return o

    def cancel_open_orders(self) -> None:
        return None

    def close_position(self) -> dict | None:
        st = self._st()
        if st["qty"] > 0:
            return self.submit_order(f"mock-close-{len(st['orders'])}", "sell", st["qty"], st["last_price"])
        return None
