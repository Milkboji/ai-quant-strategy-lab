"""Alpaca Paper 实时路径测试：使用本地假 Alpaca 服务（拦截 requests），不访问网络。"""
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
import requests

from aiquant import config, db
from aiquant.data import http
from aiquant.paper import runner as pr
from aiquant.strategy import registry

UTC = timezone.utc


class FakeAlpaca:
    def __init__(self, price=60000.0, fee=0.0025):
        self.cash, self.qty, self.price, self.fee = 100000.0, 0.0, price, fee
        self.orders = {}
        self.calls = []

    def __call__(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append((method, url))
        assert "api.alpaca.markets/" not in url.replace("paper-api.", "").replace("data.", "") or "paper-api" in url
        r = requests.models.Response()
        r.status_code = 200
        body = None
        if url.endswith("/v2/account"):
            body = {"equity": self.cash + self.qty * self.price, "cash": self.cash, "buying_power": self.cash * 2,
                    "status": "ACTIVE", "trading_blocked": False, "crypto_status": "ACTIVE"}
        elif url.endswith("/v2/positions") and method == "GET":
            body = [{"symbol": "BTCUSD", "qty": str(self.qty), "market_value": str(self.qty * self.price),
                     "avg_entry_price": str(self.price), "unrealized_pl": "0"}] if self.qty > 0 else []
        elif url.endswith("/v2/orders") and method == "POST":
            q = float(json["qty"])
            coid = json["client_order_id"]
            if coid in self.orders:
                r.status_code = 422; body = {"message": "client_order_id must be unique"}
            else:
                if json["side"] == "buy":
                    self.cash -= q * self.price; self.qty += q * (1 - self.fee)
                else:
                    self.qty -= q; self.cash += q * self.price * (1 - self.fee)
                o = {"client_order_id": coid, "status": "filled", "filled_qty": str(q),
                     "filled_avg_price": str(self.price), "side": json["side"]}
                self.orders[coid] = o; body = o
        elif "/v2/orders:by_client_order_id" in url:
            o = self.orders.get(params["client_order_id"])
            if o is None:
                r.status_code = 404; body = {"message": "not found"}
            else:
                body = o
        else:
            r.status_code = 404; body = {}
        r._content = __import__("json").dumps(body).encode()
        return r


def _bars(end, days=120, start_price=50000.0, trend=0.0):
    idx = pd.date_range(end - timedelta(days=days), end, freq="5min", inclusive="left", tz="UTC")
    n = len(idx)
    rng = np.random.default_rng(1)
    p = start_price * np.exp(np.cumsum(rng.normal(trend, 0.001, n)))
    return pd.DataFrame({"ts": idx, "open": p, "high": p * 1.001, "low": p * 0.999, "close": p, "volume": 1.0,
                         "trade_count": 1, "vwap": p})


@pytest.fixture()
def live(env, monkeypatch, tmp_path):
    os_env = {"ALPACA_PAPER_KEY_ID": "PKTEST", "ALPACA_PAPER_SECRET_KEY": "x"}
    for k, v in os_env.items():
        monkeypatch.setenv(k, v)
    f = config.CONFIG_DIR / "paper_authorization.json"
    f.write_text(json.dumps({"test": True}))
    monkeypatch.setattr(config, "PAPER_ORDER_SUBMISSION_UNLOCKED", True)
    fake = FakeAlpaca()
    monkeypatch.setattr(http.requests, "request", fake)
    schema = {"name": "live-test", "signal_tf": 60,
              "indicators": [{"id": "ef", "type": "EMA", "params": {"period": 3}},
                             {"id": "es", "type": "EMA", "params": {"period": 6}}],
              "entry": {"mode": "all", "conditions": [{"left": {"ref": "ef"}, "op": ">", "right": {"ref": "es"}}]},
              "exit": {"mode": "any", "conditions": [{"left": {"ref": "ef"}, "op": "<", "right": {"ref": "es"}}]},
              "trailing": {"activate_pct": 1.0, "trail_pct": 0.5}}
    v = registry.freeze(None, schema, source="test")
    did = db.ex("INSERT INTO paper_deployments(version_id,config_json,broker,status,created_at,updated_at,is_test) "
                "VALUES(?,?,?,?,?,?,1)", (v["id"], json.dumps({"alloc_pct": 50, "signal_tf": 60}), "alpaca_paper",
                                          "configured", db.now_iso(), db.now_iso()))
    yield fake, did
    pr.stop(did)
    f.unlink()


def test_live_paper_buy_dedupe_reconcile_and_exit(live):
    fake, did = live
    pr.start(did)
    end = datetime(2026, 1, 10, tzinfo=UTC)
    df = _bars(end, days=10, trend=0.0003)
    fake.price = float(df["close"].iloc[-1])
    run = pr.PaperRunner(did)
    r = run.step(df, end + timedelta(minutes=5))
    assert r["action"] == "buy", r
    assert any(m == "POST" and u.endswith("/v2/orders") for m, u in fake.calls)
    o = db.q1("SELECT * FROM paper_orders WHERE deployment_id=? AND side='buy'", (did,))
    assert o["status"] == "filled" and o["broker"] == "alpaca_paper"
    # 同一信号K线不重复下单；手续费导致的持仓差异（0.25%）在容差内
    r2 = run.step(df, end + timedelta(minutes=6))
    assert r2["action"] == "none" and "持仓中" in r2["reason"], r2
    assert sum(1 for m, u in fake.calls if m == "POST") == 1
    # 行情过期 → 阻止
    r3 = run.step(df, end + timedelta(hours=2))
    assert r3["action"] == "blocked"
    # 价格大幅回落触发移动止损 / 退出 → 卖出券商真实持仓数量
    df2 = pd.concat([df, _bars(end + timedelta(hours=3), days=3 / 24, start_price=fake.price * 0.97)])
    df2 = df2.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    fake.price = float(df2["close"].iloc[-1])
    r4 = run.step(df2, df2["ts"].max().to_pydatetime() + timedelta(minutes=5))
    assert r4["action"] == "sell", r4
    assert fake.qty < 1e-9
    assert all("api.alpaca.markets/v2" not in u or "paper-api" in u for _, u in fake.calls)


def test_live_paper_reconcile_mismatch_pauses(live):
    fake, did = live
    pr.start(did)
    fake.qty = 0.5  # 券商有台账之外的持仓
    end = datetime(2026, 1, 10, tzinfo=UTC)
    df = _bars(end, days=10)
    r = pr.PaperRunner(did).step(df, end + timedelta(minutes=5))
    assert r["action"] == "blocked" and "对账" in r["reason"]
    assert pr.get_deployment(did)["status"] == "paused_entries"


def test_live_write_requires_both_locks(live, monkeypatch):
    monkeypatch.setattr(config, "PAPER_ORDER_SUBMISSION_UNLOCKED", False)
    with pytest.raises(http.ForbiddenEndpoint):
        http.check_url("POST", "https://paper-api.alpaca.markets/v2/orders")
    monkeypatch.setattr(config, "PAPER_ORDER_SUBMISSION_UNLOCKED", True)
    http.check_url("POST", "https://paper-api.alpaca.markets/v2/orders")
    with pytest.raises(http.ForbiddenEndpoint):
        http.check_url("POST", "https://api.alpaca.markets/v2/orders")


def test_two_strategies_share_one_paper_account(live, monkeypatch):
    fake, did1 = live
    # 第二个策略：与第一个规则相同但独立额度
    d1 = pr.get_deployment(did1)
    cfg = {"budget_usd": 20000, "signal_tf": 60, "label": "测试策略B"}
    did2 = db.ex("INSERT INTO paper_deployments(version_id,config_json,broker,status,created_at,updated_at,is_test) "
                 "VALUES(?,?,?,?,?,?,1)", (d1["version_id"], json.dumps(cfg), "alpaca_paper", "configured",
                                           db.now_iso(), db.now_iso()))
    c1 = dict(d1["config"], budget_usd=30000, label="测试策略A")
    db.ex("UPDATE paper_deployments SET config_json=? WHERE id=?", (json.dumps(c1), did1))
    pr.start(did1); pr.start(did2)
    end = datetime(2026, 1, 10, tzinfo=UTC)
    df = _bars(end, days=10, trend=0.0003)
    fake.price = float(df["close"].iloc[-1])
    r1, r2 = pr.PaperRunner(did1), pr.PaperRunner(did2)
    a = r1.step(df, end + timedelta(minutes=5)); b = r2.step(df, end + timedelta(minutes=5))
    assert a["action"] == "buy" and b["action"] == "buy", (a, b)
    q1, q2 = r1.ledger_qty(), r2.ledger_qty()
    assert q1 * fake.price == pytest.approx(30000 * 0.99 * (1 - fake.fee), rel=0.02)
    assert q2 * fake.price == pytest.approx(20000 * 0.99 * (1 - fake.fee), rel=0.02)
    assert abs(fake.qty - (q1 + q2)) < 1e-9          # 台账记录实际到手数量（扣除币本位手续费）
    assert r1.step(df, end + timedelta(minutes=6))["action"] == "none"   # 合计对账通过
    # 只有策略A触发卖出：卖出数量 = 自己的台账数量，策略B仓位不受影响
    db.ex("UPDATE paper_deployments SET status='stopped' WHERE id=?", (did2,))
    df2 = pd.concat([df, _bars(end + timedelta(hours=3), days=3 / 24, start_price=fake.price * 0.97)])
    df2 = df2.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    fake.price = float(df2["close"].iloc[-1])
    s = r1.step(df2, df2["ts"].max().to_pydatetime() + timedelta(minutes=5))
    assert s["action"] == "sell"
    assert abs(fake.qty - q2) < 1e-9 and r2.ledger_qty() == pytest.approx(q2)
    from aiquant import config as cfgmod
    logs = list(cfgmod.LOG_DIR.glob("trading_*.log"))
    assert logs and "测试策略A" in logs[0].read_text(encoding="utf-8")


def test_portfolio_targets_include_all_strategies(env):
    from aiquant.paper import portfolio as pf
    from aiquant.live import sim as lsim
    t1 = pf.targets(); t2 = pf.targets()
    assert len(t1) == sum(1 for s in registry.all_slots() if s["current_version_id"]) + len(lsim.PRESETS)
    assert [t["version"]["id"] for t in t1] == [t["version"]["id"] for t in t2]   # 预设版本不重复创建
    assert all(t["version"]["schema"]["signal_tf"] == 15 for t in t1 if t["version"]["source"] == "preset")


def test_stale_entry_signal_is_skipped(live, monkeypatch):
    """服务中断后重启：K线收盘超过 15 分钟的入场信号不再执行（与回测一致）。"""
    fake, did = live
    pr.start(did)
    end = datetime(2026, 1, 10, tzinfo=UTC)
    df = _bars(end, days=10, trend=0.0003)
    fake.price = float(df["close"].iloc[-1])
    run = pr.PaperRunner(did)
    fake.qty = run.ledger_qty_all()        # 其他测试留下的台账持仓：让券商持仓与之一致，只测试过期信号
    real_build = pr.build_frame

    def stale_frame(*a, **k):
        f = real_build(*a, **k)
        f = f.copy()
        f.loc[f.index[-1], "close_time"] = pd.Timestamp(end) - pd.Timedelta(minutes=40)
        return f
    monkeypatch.setattr(pr, "build_frame", stale_frame)
    entry = run.schema.entry
    monkeypatch.setattr(pr, "group_signal", lambda frame, g: np.full(len(frame), g is entry))
    r = run.step(df, end + timedelta(minutes=5))
    assert r["action"] == "none" and "已过期" in r["reason"], r
    assert not any(m == "POST" for m, u in fake.calls)
    st = db.get_setting(f"stale_signal::{did}")          # 记下最近一次过期信号，供页面解释“为什么没有动作”
    assert st and st["age_min"] >= 40 and st["limit_min"] == 15 and "已过期" in st["reason"]


def test_btc_entries_paused_while_etf_reclaims_budget(live):
    """美股 ETF 收回临时借给 BTC 的额度期间，BTC 不开新仓（持仓照常按规则卖出）。"""
    fake, did0 = live
    d0 = db.q1("SELECT * FROM paper_deployments WHERE id=?", (did0,))
    did = db.ex("INSERT INTO paper_deployments(version_id,config_json,broker,status,created_at,updated_at,is_test) "
                "VALUES(?,?,?,?,?,?,1)", (d0["version_id"], d0["config_json"], "alpaca_paper", "configured",
                                          db.now_iso(), db.now_iso()))
    pr.start(did)
    end = datetime(2026, 1, 10, tzinfo=UTC)
    df = _bars(end, days=10, trend=0.0003)
    fake.price = float(df["close"].iloc[-1])
    run = pr.PaperRunner(did)
    fake.qty = run.ledger_qty_all()
    n0 = sum(1 for m, u in fake.calls if m == "POST")
    db.set_setting("etf_lend", {"state": "reclaim", "since": db.now_iso(), "lend_pct": 50})
    try:
        r = run.step(df, end + timedelta(minutes=5))
        assert r["action"] == "blocked" and "收回" in r["reason"], r
        assert sum(1 for m, u in fake.calls if m == "POST") == n0
    finally:
        db.set_setting("etf_lend", None)
        pr.stop(did)
