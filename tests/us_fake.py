"""测试辅助：通用美股执行器的干净状态（ETF 择时部署、空台账）。"""
from aiquant import db


def reset_us(pos_qty: float = 0.0, price: float = 100.0, symbol: str = "QLD") -> dict:
    from aiquant.paper import etf_trend as et
    from aiquant.paper import runner as pr
    from aiquant.paper import us_exec, us_ledger
    et.ensure_tables()
    us_ledger.ensure_tables()
    for t in ("us_orders", "us_order_allocs", "us_ledger"):
        db.ex(f"DELETE FROM {t}")
    for k in ("etf_trend_state", "etf_trend_why", "etf_lend", "us_after_close", "etf_after_close"):
        db.set_setting(k, None)
    db.ex("UPDATE paper_deployments SET status='stopped' WHERE broker='alpaca_us' "
          "AND json_extract(config_json,'$.kind')<>'etf_sma'")
    cfg = dict(db.get_setting("etf_trend_config") or {})
    cfg.update(enabled=True, pending_offline=False)
    db.set_setting("etf_trend_config", cfg)
    us_exec.ensure_migrated()
    dep = us_exec.etf_deployment()
    pr.set_status(dep["id"], "running")
    if pos_qty:
        us_ledger.record_opening(dep["id"], symbol, pos_qty, price, "测试期初")
    return pr.get_deployment(dep["id"])


class FakeUS:
    """假券商（只在内存里）：现金、各标的持仓、DAY 市价单。按设定的成交比例立即撮合，同步订单时返回终结状态。
    买单金额超过现金会直接断言失败（任何情况下不得使用保证金）。"""

    def __init__(self, cash: float = 100_000.0, prices: dict | None = None):
        self.cash = cash
        self.prices = dict(prices or {"QLD": 100.0})
        self.pos: dict[str, float] = {}
        self.orders: dict[str, dict] = {}
        self.sent: list[dict] = []
        self.fill_ratio: dict[str, float] = {}
        self.reject: set[str] = set()
        self.min_cash = cash

    def install(self, monkeypatch, bars: dict | None = None) -> "FakeUS":
        from aiquant.paper import etf_trend as et
        from aiquant.paper import us_exec
        monkeypatch.setattr(et, "_submit", self.submit)
        monkeypatch.setattr(et, "_position", self.position)
        monkeypatch.setattr(et, "_account", self.account)
        monkeypatch.setattr(et, "_clock", lambda: {"is_open": True})
        monkeypatch.setattr(et.http, "paper_orders_unlocked", lambda: True)
        monkeypatch.setattr(et, "update_daily_csv", lambda *a, **k: {})
        monkeypatch.setattr(us_exec.alpaca, "paper_get", self.paper_get)
        if bars is not None:
            monkeypatch.setattr(et, "daily_bars", lambda sym, days_=420: bars[sym])
        return self

    def equity(self) -> float:
        return self.cash + sum(q * self.prices.get(s, 0) for s, q in self.pos.items())

    def account(self) -> dict:
        return {"equity": str(self.equity()), "cash": str(self.cash), "long_market_value": str(self.equity() - self.cash)}

    def position(self, sym: str) -> dict:
        q = self.pos.get(sym, 0.0)
        return {"qty": q, "market_value": q * self.prices.get(sym, 0), "avg_entry_price": 0, "unrealized_pl": 0}

    def submit(self, sym, side, notional=None, qty=None, coid=None, **kw):
        if sym in self.reject:
            raise RuntimeError("Alpaca Paper 拒绝订单 403: test reject")
        px = self.prices[sym]
        ratio = self.fill_ratio.get(sym, 1.0)
        want = notional / px if notional is not None else float(qty)
        q = round(want * ratio, 9)
        if side == "buy":
            assert q * px <= self.cash + 1e-6, f"买入 {q * px:.2f} 超过现金 {self.cash:.2f}：不得使用保证金"
            self.cash -= q * px
            self.pos[sym] = self.pos.get(sym, 0.0) + q
        else:
            assert q <= self.pos.get(sym, 0.0) + 1e-9, "卖出超过持仓（不得做空）"
            self.pos[sym] = self.pos.get(sym, 0.0) - q
            self.cash += q * px
        self.min_cash = min(self.min_cash, self.cash)
        self.orders[coid] = {"client_order_id": coid, "status": "filled" if ratio >= 1 else "expired",
                             "filled_qty": str(q), "filled_avg_price": str(px)}
        self.sent.append({"symbol": sym, "side": side, "notional": notional, "qty": qty, "coid": coid, "filled": q})
        return {"status": "accepted", "client_order_id": coid}

    def paper_get(self, path, params=None):
        if path == "/v2/orders:by_client_order_id":
            o = self.orders.get((params or {}).get("client_order_id"))
            return (200, o) if o else (404, {"message": "not found"})
        if path.startswith("/v2/positions/"):
            s = path.rsplit("/", 1)[-1]
            return (200, self.position(s)) if self.pos.get(s) else (404, {})
        return 404, {}
