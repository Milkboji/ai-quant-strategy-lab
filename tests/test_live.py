"""实时看盘：行情聚合、指标解读、虚拟交易（实时步进 + 快速回放）。使用模拟行情，不访问网络。"""
import time
from datetime import datetime, timezone

import pandas as pd
import pytest

from aiquant.live import analysis, sim
from aiquant.live.feed import TIMEFRAMES, LiveFeed


@pytest.fixture(scope="module")
def feed(env):
    f = LiveFeed("synthetic")
    for _ in range(3):
        f.tick()
    return f


def test_candles_all_timeframes(feed):
    for name, sec in TIMEFRAMES.items():
        c = feed.candles(sec, 120)
        assert len(c) > 0, name
        assert {"ts", "open", "high", "low", "close", "volume", "complete"} <= set(c.columns)
        assert (c["high"] >= c[["open", "close"]].max(axis=1) - 1e-9).all()
        assert (c["ts"].diff().dropna() >= pd.Timedelta(seconds=sec)).all(), name
        assert not c["complete"].iloc[:-1].eq(False).any() or sec < 60


def test_analysis_plain_language(feed):
    c = feed.candles(60, 200)
    r = analysis.analyze(c[c["complete"]])
    assert -100 <= r["score"] <= 100 and r["verdict"] and len(r["items"]) >= 4
    assert analysis.analyze(c.head(5))["verdict"] == "数据积累中"


def test_live_engine_trades_on_bar_close(feed, monkeypatch):
    sim.ensure_tables()
    sid = sim.ENGINE.start("trend", 5, 10_000, 25, 1, "synthetic")
    # 强制信号：入场条件恒真
    orig = sim.signal_frame
    monkeypatch.setattr(sim, "signal_frame", lambda c, s: (c, [True] * len(c), [False] * len(c)))
    sim.db.ex("UPDATE sim_sessions SET last_bar=NULL WHERE id=?", (sid,))
    sim.ENGINE.step(feed)
    a = sim.db.q1("SELECT * FROM sim_sessions WHERE id=?", (sid,))
    assert a["qty"] > 0 and a["cash"] < 10_000
    t = sim.trades_of(sid)
    assert list(t["side"]) == ["buy"] and t["price"].iloc[0] >= feed.ask  # 买在卖一价+滑点
    # 同一根K线不重复买
    sim.ENGINE.step(feed)
    assert len(sim.trades_of(sid)) == 1
    # 止损：把买一价压低
    monkeypatch.setattr(sim, "signal_frame", orig)
    feed.bid = a["entry_price"] * 0.98
    sim.ENGINE.step(feed)
    t = sim.trades_of(sid)
    assert t["side"].iloc[-1] == "sell" and "止损" in t["reason"].iloc[-1]
    assert t["pnl"].iloc[-1] < 0
    sim.ENGINE.stop(False)
    assert sim.ENGINE.active() is None


@pytest.mark.parametrize("preset", list(sim.PRESETS))
def test_replay_all_presets(feed, preset):
    c = feed.candles(60, 3 * 1440)
    r = sim.replay(preset, 60, c, 10_000, 25, 1, source="synthetic")
    assert r["bars"] > 1000 and "return_pct" in r and "buy_hold_pct" in r
    t = sim.trades_of(r["session_id"])
    if len(t):
        assert t["side"].iloc[0] == "buy" and t["side"].iloc[-1] == "sell"
        # 余额守恒：最终现金 = 初始 + 所有已平仓盈亏
        assert abs(r["final_equity"] - (10_000 + t["pnl"].fillna(0).sum())) < 1e-6
