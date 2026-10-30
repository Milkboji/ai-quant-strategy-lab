"""点差模型：历史报价采样（唯一出口、只读、开盘前后暂停）、按时段估计、口径快照与切换（管理员确认、旧结果标为旧口径）、
回测按 UTC 小时取点差（全部小时等于固定值时与旧口径逐笔相同）。"""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from aiquant import db, research, spread_model
from aiquant.data import http
from aiquant.engine import backtest as bt
from aiquant.engine.execution import CostModel, fill


@pytest.fixture()
def sm(env, monkeypatch):
    spread_model.ensure_tables()
    from aiquant import auth
    auth.ensure()
    db.ex("DELETE FROM spread_samples")
    db.ex("DELETE FROM spread_reports")
    db.set_setting("spread_basis", None)
    monkeypatch.setattr(spread_model, "symbols", lambda: ["BTC/USD", "QLD"])
    yield
    db.set_setting("spread_basis", None)
    db.ex("DELETE FROM spread_samples")


def test_fill_uses_hourly_spread_only_when_profile_given():
    cm = CostModel(fee_bps=0, slippage_bps=0, spread_bps=2.0)
    a = fill("buy", 100.0, 1.0, cm, pd.Timestamp("2026-10-01T05:00Z"))
    assert a.price == pytest.approx(100.01)                       # 旧口径：忽略时间
    prof = tuple([2.0] * 24)
    prof = prof[:5] + (10.0,) + prof[6:]
    cm2 = replace(cm, spread_hourly=prof, spread_bps=10.0, basis="quotes_v1")
    assert fill("buy", 100.0, 1.0, cm2, pd.Timestamp("2026-10-01T05:30Z")).price == pytest.approx(100.05)
    assert fill("buy", 100.0, 1.0, cm2, pd.Timestamp("2026-10-01T06:00Z")).price == pytest.approx(100.01)


def test_backtest_with_flat_profile_equals_fixed_and_hourly_changes_costs(env):
    from aiquant.data import market_data as md
    from aiquant.strategy.schema import validate
    sp = research.get_split()
    df = md.load_dataset(md.get_dataset(md.to_utc(sp["val_start"]) - timedelta(days=60), md.to_utc(sp["val_start"]),
                                        source="test"))
    obj, _ = validate({"name": "t", "signal_tf": 60,
                       "indicators": [{"id": "a", "type": "EMA", "params": {"period": 3}},
                                      {"id": "b", "type": "EMA", "params": {"period": 8}}],
                       "entry": {"mode": "all", "conditions": [{"left": {"ref": "a"}, "op": ">", "right": {"ref": "b"}}]},
                       "exit": {"mode": "any", "conditions": [{"left": {"ref": "a"}, "op": "<", "right": {"ref": "b"}}]}})
    s, e = md.to_utc(sp["val_start"]) - timedelta(days=30), md.to_utc(sp["val_start"])
    base = bt.BTConfig(start=s, end=e, cost=CostModel(25, 2, 1))
    a = bt.run(df, obj, 60, None, base)
    flat = bt.BTConfig(start=s, end=e, cost=replace(base.cost, spread_hourly=tuple([1.0] * 24), basis="quotes_v1"))
    b = bt.run(df, obj, 60, None, flat)
    assert [t["exit_price"] for t in a["trades"]] == [t["exit_price"] for t in b["trades"]]
    assert a["metrics"]["final_equity"] == pytest.approx(b["metrics"]["final_equity"], rel=1e-12)
    wide = bt.BTConfig(start=s, end=e, cost=replace(base.cost, spread_hourly=tuple([1.0] * 12 + [8.0] * 12),
                                                     spread_bps=8.0, basis="quotes_v1"))
    c = bt.run(df, obj, 60, None, wide)
    assert c["metrics"]["spread_cost"] > a["metrics"]["spread_cost"]
    assert c["metrics"]["trades"] == a["metrics"]["trades"]


def test_summarize_drops_bad_quotes():
    q = [{"bp": 100.0, "ap": 100.02}] * 30 + [{"bp": 0, "ap": 1}, {"bp": 101, "ap": 100}]
    s = spread_model.summarize(q)
    assert s["n"] == 30 and s["median"] == pytest.approx(2.0, rel=1e-3)
    assert spread_model.summarize(q[:5]) is None


def test_collect_reads_quotes_through_single_exit_and_respects_quiet_window(sm, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "s")
    calls = []

    class R:
        status_code = 200

        def __init__(self, url):
            self.url = url

        def json(self):
            q = [{"bp": 100.0, "ap": 100.03, "t": "x"}] * 50
            return {"quotes": {"BTC/USD": q}} if "crypto" in self.url else {"quotes": q}

    def fake(method, url, **kw):
        calls.append((method, url, kw.get("params")))
        return R(url)
    monkeypatch.setattr(http.requests, "request", fake)
    quiet = datetime(2026, 10, 29, 13, 35, tzinfo=timezone.utc)          # 美东 09:35，交易日
    assert spread_model.collect(quiet) == 0 and not calls
    now = datetime(2026, 10, 29, 18, 0, tzinfo=timezone.utc)
    n = spread_model.collect(now, max_requests=30)
    assert n == 30 and all(m == "GET" and "data.alpaca.markets" in u for m, u, _ in calls)
    assert any("/v2/stocks/QLD/quotes" in u for _, u, _ in calls) or any("crypto" in u for _, u, _ in calls)
    a = db.q1("SELECT url FROM endpoint_audit ORDER BY id DESC LIMIT 1")
    assert "quotes" in a["url"] and "?" not in a["url"]
    assert db.q1("SELECT COUNT(*) AS n FROM spread_samples WHERE status='ok'")["n"] == 30


def _seed(sym, buckets, days=12, bps=lambda b: 3.0):
    for d in range(days):
        day = f"2026-10-{d + 1:02d}"
        for b in buckets:
            db.ex("INSERT OR REPLACE INTO spread_samples(symbol,day,bucket,n,median_bps,p90_bps,status,attempts,fetched_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?)", (sym, day, b, 100, bps(b), bps(b) * 2, "ok", 1, db.now_iso()))


def test_estimate_profiles_and_us_open_hours(sm):
    _seed("BTC/USD", [f"{h:02d}" for h in range(24)], bps=lambda b: 1.0 + int(b) / 10)
    _seed("QLD", spread_model.US_BUCKETS, bps=lambda b: 9.0 if b == "09:35" else 3.0)
    e = spread_model.estimate("BTC/USD")
    assert e["profile"][0] == pytest.approx(1.0) and e["profile"][23] == pytest.approx(3.3) and e["days"] == 12
    q = spread_model.estimate("QLD")["profile"]
    assert q[13] == q[14] == pytest.approx(9.0) and q[18] == pytest.approx(3.0)
    _seed("SSO", spread_model.US_BUCKETS, days=5)
    assert spread_model.estimate("SSO") is None                 # 样本不足 10 天
    p = spread_model.proposal()
    assert set(p["profiles"]) == {"BTC/USD", "QLD"} and p["version"] == 1


def test_switch_needs_admin_freezes_snapshot_and_marks_old_results(sm, monkeypatch):
    _seed("BTC/USD", [f"{h:02d}" for h in range(24)], bps=lambda b: 2.0)
    _seed("QLD", spread_model.US_BUCKETS, bps=lambda b: 6.0)
    monkeypatch.setattr(spread_model, "_deployed", lambda: [])
    rep = spread_model.build_report("boss")
    assert "新旧口径回测对比" in rep["markdown"] and "旧口径（固定）" in rep["markdown"]
    assert spread_model.current_name() == "fixed_v1"             # 生成报告不切换
    with pytest.raises(PermissionError):
        spread_model.enable(rep["id"], actor="t", role="trader")
    assert spread_model.enable(rep["id"], actor="boss", role="admin") == "已切换为实测点差 v1"
    assert spread_model.current_name() == "quotes_v1"
    cm = research.cost_model(research.get_research())
    assert cm.basis == "quotes_v1" and cm.spread_hourly == tuple([2.0] * 24)
    us = research.cost_model(dict(research.get_research(), symbol="QLD"), __import__("aiquant.market", fromlist=["x"]).US_EQUITY)
    assert us.spread_hourly[14] == 6.0 and us.fee_bps == 0
    other = research.cost_model(dict(research.get_research(), symbol="IWM"), __import__("aiquant.market", fromlist=["x"]).US_EQUITY)
    assert other.basis == "fixed_v1"                              # 没有样本的品种仍用固定代理值
    assert spread_model.is_old(None) and spread_model.is_old("fixed_v1") and not spread_model.is_old("quotes_v1")
    assert spread_model.basis_label(None) == "固定点差代理（旧口径）"
    # 采样继续进行不会改变已冻结的快照
    _seed("BTC/USD", [f"{h:02d}" for h in range(24)], days=20, bps=lambda b: 9.0)
    assert research.cost_model(research.get_research()).spread_hourly == tuple([2.0] * 24)
    # 重新估计 → 新报告是 v2，需要再次确认；确认后旧报告（v1）不能再用来切换
    rep2 = spread_model.build_report("boss")
    assert rep2["candidate"]["version"] == 2
    assert spread_model.enable(rep2["id"], actor="boss", role="admin") == "已切换为实测点差 v2"
    with pytest.raises(ValueError):
        spread_model.enable(rep["id"], actor="boss", role="admin")
    a = db.q1("SELECT * FROM user_audit WHERE action='切换点差口径' ORDER BY id DESC LIMIT 1")
    assert a and "v2" in a["detail"]


def test_params_key_changes_with_basis(sm):
    r = research.get_research()
    s, e = datetime(2025, 1, 1, tzinfo=timezone.utc), datetime(2025, 2, 1, tzinfo=timezone.utc)
    k0 = research.params_key(1, 60, None, s, e, r, "test")
    db.set_setting("spread_basis", {"version": 1, "profiles": {"BTC/USD": [1.0] * 24}, "hash": "h"})
    assert research.params_key(1, 60, None, s, e, r, "test") != k0
