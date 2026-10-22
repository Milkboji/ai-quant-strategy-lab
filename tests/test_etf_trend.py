"""美股 ETF 趋势择时：信号缓冲逻辑、只用已收盘日线、下单决策。"""
from datetime import date, datetime, timezone

import pandas as pd

from aiquant import db
from aiquant.paper import etf_trend as et


def test_regime_hysteresis():
    close = pd.Series([100.0] * 20 + [106, 101, 95, 99])
    st = et.regime_series(close, 5, 0.02)
    assert st.iloc[20] == 1          # 明显站上均线上沿
    assert st.iloc[21] == 1          # 回到缓冲区内：保持
    assert st.iloc[22] == 0          # 跌破下沿：转空
    assert st.iloc[23] == 0          # 在缓冲区内：保持空


def test_signal_uses_only_completed_bars():
    days = pd.bdate_range("2026-01-01", periods=200).date
    df = pd.DataFrame({"date": days, "close": [100 + i * 0.1 for i in range(200)]})
    cfg = dict(et.DEFAULTS)
    today = days[-1]
    s = et.compute_signal(df, today, cfg)
    assert s["date"] == str(days[-2])                     # 当天未收盘的K线不参与
    assert s["regime"] == 1


def test_step_buys_when_bullish(env, monkeypatch):
    et.ensure_tables()
    db.set_setting("etf_trend_state", {})
    monkeypatch.setattr(et.http, "paper_orders_unlocked", lambda: True)
    monkeypatch.setattr(et, "_clock", lambda: {"is_open": True})
    days = pd.bdate_range("2026-01-01", periods=200).date
    monkeypatch.setattr(et, "daily_bars", lambda sym, days_=420: pd.DataFrame({"date": days, "close": [100 + i * 0.2 for i in range(200)]}))
    monkeypatch.setattr(et, "_position", lambda sym: {"qty": 0.0, "market_value": 0, "avg_entry_price": 0, "unrealized_pl": 0})
    monkeypatch.setattr(et, "_account", lambda: {"equity": "100000", "cash": "90000"})
    sent = []
    monkeypatch.setattr(et, "_submit", lambda sym, side, **kw: sent.append((sym, side, kw)) or {})
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)          # 美东 10:00
    st = et.step(now)
    assert sent and sent[0][0] == "QLD" and sent[0][1] == "buy" and abs(sent[0][2]["notional"] - 30000) < 1
    assert st["last_action_date"] == "2026-10-21"
    sent.clear()
    et.step(now)                                                      # 同一天不重复下单
    assert not sent
