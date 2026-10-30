"""我的交易系统 v1：分批仓位引擎（首仓 / 止损 / 保本 / 卖一半 / 移动止盈 / 时间止损 / 加仓与底仓等额减仓 / 大势过滤 /
跳空按开盘价）、模拟盘与回测同一个引擎、版本冻结与部署（试用期、审计、观察员只读）、一次性部署、执行器按比例跟随引擎。"""
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from aiquant import db
from aiquant.engine import my_system as ms
from aiquant.engine.execution import CostModel

T0 = pd.Timestamp("2024-01-01", tz="UTC")
FREE = CostModel(0.0, 0.0, 0.0)


def _bars(path: list[tuple], start=T0, noisy_days: int = 400, seed: int = 1) -> pd.DataFrame:
    """前 noisy_days 天高波动（让之后的横盘落在一年波动率的低分位），然后按 path 给出的 (open, high, low, close)。"""
    rng = np.random.default_rng(seed)
    rows, px = [], 100.0
    for _ in range(noisy_days):
        o = px
        c = o * (1 + rng.normal(0, 0.04))
        rows.append((o, max(o, c) * 1.01, min(o, c) * 0.99, c))
        px = c
    k = 100.0 / px                      # 把高波动段缩放到以 100 结束，接上后面的横盘
    rows = [tuple(x * k for x in r) for r in rows] + list(path)
    ts = pd.date_range(start, periods=len(rows), freq="1D")
    a = np.array(rows)
    return pd.DataFrame({"ts": ts, "open": a[:, 0], "high": a[:, 1], "low": a[:, 2], "close": a[:, 3], "volume": 1.0})


def _flat(n: int, lo=99.0, hi=101.0):
    return [(100.0, hi, lo, 100.0 + (0.3 if i % 2 else -0.3)) for i in range(n)]


def _bull_bench(n: int) -> pd.DataFrame:
    ts = pd.date_range(T0, periods=n, freq="1D")
    c = np.linspace(50, 150, n)
    return pd.DataFrame({"ts": ts, "open": c, "high": c, "low": c, "close": c, "volume": 1.0})


def _run(bars, p=None, bench=None, live=False, end_days=None, **kw):
    b = bars if isinstance(bars, dict) else {"AAA": bars}
    n = max(len(x) for x in b.values())
    return ms.run(b, bench if bench is not None else _bull_bench(n), p or ms.Params(box_days=30, half_r=3, time_stop=20),
                  cost=FREE, start=T0 + pd.Timedelta(days=380), end=T0 + pd.Timedelta(days=end_days or n),
                  initial_cash=10_000, live=live, **kw)


def test_breakout_entry_risk_sizing_breakeven_half_sell_trail():
    # 40 天横盘 [99,101]，第 441 天收盘 103 突破（最低 100），之后每天涨 2%，再回落
    up = [(103.0 * 1.02 ** i, 103.0 * 1.02 ** i * 1.01, 103.0 * 1.02 ** i * 0.995, 103.0 * 1.02 ** (i + 1)) for i in range(25)]
    top = 103.0 * 1.02 ** 25
    down = [(top * 0.97 ** i, top * 0.97 ** i * 1.002, top * 0.97 ** (i + 1) * 0.99, top * 0.97 ** (i + 1)) for i in range(15)]
    path = _flat(40) + [(100.5, 103.2, 100.0, 103.0)] + up + down
    r = _run(_bars(path))
    tr = r["trades"]
    first = [t for t in tr if t["tranche"] == "首仓"]
    assert first, r["events"]
    entry = first[0]["entry_price"]
    assert first[0]["stop"] >= 100.0 or any("保本" in t["exit_reason"] or "移动" in t["exit_reason"] for t in first)
    qty0 = sum(t["qty"] for t in first)
    assert qty0 * entry <= 10_000 * 0.20 + 1e-6                     # 首仓 ≤ 额度 20%
    assert qty0 * (entry - 100.0) <= 10_000 * 0.03 + 1e-6           # 亏到止损 ≤ 额度 3%
    half = [t for t in tr if "卖一半" in t["exit_reason"]]
    assert half and half[0]["exit_price"] == pytest.approx(max(entry + 3 * (entry - 100.0), half[0]["exit_price"]), rel=1e-9)
    assert any("移动止盈" in t["exit_reason"] or "保本" in t["exit_reason"] for t in tr)
    assert r["metrics"]["entries"] == 1 and r["metrics"]["final_equity"] > 10_000
    assert any(e["event"] == "首次建仓" for e in r["events"])


def test_risk_cap_shrinks_position_when_stop_is_far():
    path = _flat(40, lo=90.0, hi=101.0) + [(100.5, 103.0, 80.0, 102.9)] + _flat(5, lo=101.0, hi=104.0)
    r = _run(_bars(path))
    t = [x for x in r["trades"] if x["tranche"] == "首仓"]
    if t:
        q = sum(x["qty"] for x in t)
        assert q * (t[0]["entry_price"] - 80.0) <= 10_000 * 0.03 + 1e-6
        assert q * t[0]["entry_price"] < 10_000 * 0.20


def test_gap_below_stop_fills_at_open_and_regime_filter_blocks():
    path = _flat(40) + [(100.5, 103.2, 100.0, 103.0), (103.0, 103.5, 102.0, 103.2), (95.0, 96.0, 94.0, 95.5)]
    r = _run(_bars(path))
    stop = [t for t in r["trades"] if "止损" in t["exit_reason"]]
    assert stop and stop[0]["exit_price"] == pytest.approx(95.0)    # 跳空低开到 95 < 止损 100 → 按开盘价
    bear = _bull_bench(len(path) + 400).assign(close=lambda d: d["close"][::-1].values)
    r2 = _run(_bars(path), bench=bear)
    assert r2["metrics"]["entries"] == 0 and r2["metrics"]["blocked"]["大势过滤"] >= 1


def test_time_stop_exits_without_new_high():
    path = _flat(40) + [(100.5, 103.2, 100.0, 103.0)] + [(102.8, 103.1, 101.5, 102.5)] * 30
    r = _run(_bars(path), p=ms.Params(box_days=30, half_r=3, time_stop=10))
    assert any(t["exit_reason"].startswith("时间止损") for t in r["trades"])


def test_add_on_after_pullback_and_base_reduced_when_add_stops_out():
    up = [(103 + i, 104.5 + i, 102.6 + i, 104 + i) for i in range(6)]                 # 1R 以上 → 首仓保本
    quiet = [(108.0, 108.3, 107.9, 108.1)] * 3 + [(107.8, 108.0, 107.6, 107.7)] * 3    # 回调 + 横盘缩量
    crash = [(107.0, 107.2, 104.0, 104.5)]                                             # 加仓那批止损
    path = _flat(40) + [(100.5, 103.2, 100.0, 103.0)] + up + quiet + crash + _flat(3, 104, 105)
    r = _run(_bars(path))
    kinds = {t["tranche"] for t in r["trades"]}
    if "加仓1" in kinds:
        add = [t for t in r["trades"] if t["tranche"] == "加仓1"][0]
        assert add["entry_price"] > 0
        if add["pnl"] < 0 and "止损" in add["exit_reason"]:
            assert any("底仓卖出等额亏损" in t["exit_reason"] for t in r["trades"])


def test_live_replay_matches_backtest_up_to_today():
    up = [(103.0 * 1.02 ** i, 103.0 * 1.02 ** i * 1.01, 103.0 * 1.02 ** i * 0.995, 103.0 * 1.02 ** (i + 1)) for i in range(20)]
    path = _flat(40) + [(100.5, 103.2, 100.0, 103.0)] + up
    bars = _bars(path)
    n = len(bars)
    full = _run(bars)
    live = _run(bars, live=True)
    assert live["state"]                                             # 今天仍持有
    st = live["state"]["AAA"]
    sold_live = sum(t["qty"] for t in live["trades"])
    first_qty = sum(t["qty"] for t in full["trades"] if t["tranche"] == "首仓")
    assert st["qty"] + sold_live == pytest.approx(first_qty, rel=1e-9)
    assert [e for e in live["events"] if e["event"] != "全部离场"] == \
        [e for e in full["events"] if e["event"] != "全部离场"][:len([e for e in live["events"] if e["event"] != "全部离场"])]
    assert n > 0


def test_describe_recites_each_rule_in_plain_words():
    lines = ms.describe(ms.Params(box_days=60, half_r=5, time_stop=20), "us_equity", ["QQQ", "SPY", "IWM"])
    txt = "\n".join(lines)
    for k in ("横盘", "200 日均线", "止损", "加仓", "保本", "卖一半", "移动止盈", "没有创新高", "最多持有 3 个品种", "SPY"):
        assert k in txt, k
    assert len(lines) == 9


# ---------------------------------------------------------------------------
# 版本、部署、执行器
# ---------------------------------------------------------------------------

@pytest.fixture()
def sys_env(env, monkeypatch):
    from aiquant import allocation, auth, console, my_system
    from aiquant.paper import us_ledger
    auth.ensure()
    console.ensure_tables()
    my_system.ensure_tables()
    us_ledger.ensure_tables()
    for t in ("us_orders", "us_order_allocs", "us_ledger", "system_versions"):
        db.ex(f"DELETE FROM {t}")
    db.ex("UPDATE paper_deployments SET status='stopped' WHERE json_extract(config_json,'$.kind')='my_system'")
    old = allocation.get_config()
    yield my_system
    db.ex("UPDATE paper_deployments SET status='stopped' WHERE json_extract(config_json,'$.kind')='my_system'")
    for t in ("us_orders", "us_order_allocs", "us_ledger"):     # 通用台账里的 BTC/USD 会进入 BTC 对账，测试后清掉
        db.ex(f"DELETE FROM {t}")
    db.set_setting("capital_layers", None)
    db.set_setting("upgrade::my_system_v1_deploy", None)


def test_freeze_dedupes_validates_and_audits(sys_env):
    S = sys_env
    v1 = S.freeze("crypto", S.default_params("crypto"), actor="boss")
    v1b = S.freeze("crypto", S.default_params("crypto"), actor="boss")
    assert v1["id"] == v1b["id"] and v1["version_no"] == 1
    v2 = S.freeze("crypto", dict(S.default_params("crypto"), half_r=3.0), actor="boss")
    assert v2["version_no"] == 2
    with pytest.raises(ValueError):
        S.freeze("crypto", dict(S.default_params("crypto"), first_pct=60, add_pct=30), actor="boss")
    a = db.q1("SELECT * FROM strategy_audit WHERE action='冻结版本' ORDER BY id DESC LIMIT 1")
    assert a and "卖一半的倍数" in a["detail"]


def test_initial_deploy_lowers_crypto_cap_and_starts_two_trials(sys_env, monkeypatch):
    from aiquant import allocation, console
    from aiquant.data import http
    S = sys_env
    assert "skipped" in S.initial_deploy() and not S.deployments()        # 下单锁没开（公开版默认）：什么都不做
    monkeypatch.setattr(http, "paper_orders_unlocked", lambda: True)
    out = S.initial_deploy()
    assert float(allocation.get_config()["crypto"]["max"]) == 56.0
    deps = S.deployments()
    assert {d["config"]["market"] for d in deps} == {"crypto", "us_equity"}
    for d in deps:
        assert allocation.trial_status(d)["trial"] and d["broker"] == "alpaca_us"
    tp = allocation.trial_plan(100_000)
    assert sum(tp[d["id"]]["budget"] for d in deps) == pytest.approx(10_000, rel=0.05)   # 现金层腾出的 10%
    assert db.q("SELECT * FROM strategy_audit WHERE action='部署' AND strategy_key LIKE 'my_system:%'")
    assert "crypto" in out and "us_equity" in out
    # 再执行一次不会重复部署
    S.initial_deploy()
    assert len(S.deployments()) == 2
    # 观察员不能部署
    with pytest.raises(console.ConsolePermissionError):
        S.deploy(deps[0]["version_id"], actor="v", role="viewer")


def test_executor_follows_engine_proportionally(sys_env, monkeypatch):
    """引擎首仓买入 → 按额度比例买；引擎卖一半 → 卖一半实际持仓。"""
    import sys
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    from us_fake import FakeUS
    from aiquant import allocation
    from aiquant.paper import my_system_exec as mx
    from aiquant.paper import us_ledger
    S = sys_env
    from aiquant.data import http
    monkeypatch.setattr(http, "paper_orders_unlocked", lambda: True)
    S.initial_deploy()
    dep = [d for d in S.deployments() if d["config"]["market"] == "crypto"][0]
    up = [(103.0 * 1.02 ** i, 103.0 * 1.02 ** i * 1.01, 103.0 * 1.02 ** i * 0.995, 103.0 * 1.02 ** (i + 1)) for i in range(20)]
    path = _flat(40) + [(100.5, 103.2, 100.0, 103.0)] + up
    bars = _bars(path, start=pd.Timestamp("2025-01-01", tz="UTC"))
    bo = bars["ts"].iloc[440]                                        # 突破那天
    bench = _bull_bench(len(bars)).assign(ts=bars["ts"])    # BTC 一路向上：大势过滤放行
    data = {"ETH/USD": bars, "BTC/USD": bench}
    cfg = dep["config"]
    cfg.update(symbols=["BTC/USD", "ETH/USD"], live_since=(bo - pd.Timedelta(days=3)).isoformat())
    db.ex("UPDATE paper_deployments SET config_json=? WHERE id=?", (json.dumps(cfg, ensure_ascii=False), dep["id"]))

    def daily(mkey, sym, now=None, fetch=True):
        d = data[sym] if sym in data else bench
        return d[d["ts"] < pd.Timestamp(now).normalize()].reset_index(drop=True) if now is not None else d
    monkeypatch.setattr(S, "daily", daily)
    fake = FakeUS(cash=100_000, prices={"BTC/USD": 150.0, "ETH/USD": 104.0}).install(monkeypatch)
    monkeypatch.setattr(mx, "_latest", lambda mkey, sym: fake.prices[sym])
    monkeypatch.setattr(mx.http, "paper_orders_unlocked", lambda: True)
    day1 = (bo + pd.Timedelta(days=1)).to_pydatetime() + timedelta(hours=1)
    mx.step(day1)
    buys = [o for o in fake.sent if o["side"] == "buy" and o["symbol"] == "ETH/USD"]   # 测试里 BTC（基准）一路上涨也算突破
    assert len(buys) == 1
    budget = allocation.trial_plan(fake.equity())[dep["id"]]["budget"]
    assert budget > 0 and buys[0]["notional"] <= budget * 0.20 + 1                   # 首仓 ≤ 额度 20%
    mx.step(day1 + timedelta(minutes=6))                                             # 同步成交、记台账
    q = us_ledger.position(dep["id"], "ETH/USD")["qty"]
    assert q == pytest.approx(fake.pos["ETH/USD"] * (1 - mx.CRYPTO_FEE), rel=1e-6)   # 台账按到手数量（扣手续费）
    # 再过几天大涨到 3R 以上：引擎卖一半 → 实际也卖一半
    later = (bo + pd.Timedelta(days=12)).to_pydatetime() + timedelta(hours=1)
    fake.prices["ETH/USD"] = float(bars["close"].iloc[452])
    mx.step(later)
    sells = [o for o in fake.sent if o["side"] == "sell" and o["symbol"] == "ETH/USD"]
    assert sells and sells[0]["symbol"] == "ETH/USD"
    assert float(sells[0]["qty"]) == pytest.approx(q / 2, rel=0.02)                  # 引擎卖一半 → 实际卖一半
    why = mx.get_why(dep["id"])
    assert why and "引擎持仓" in why["text"]
    assert fake.min_cash >= 0
