"""美股 ETF 趋势择时：信号缓冲逻辑、只用已收盘日线、下单决策。"""
from datetime import date, datetime, timezone

import pandas as pd
import pytest

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


# ---------------------------------------------------------------------------
# 数据卫生：只写已收盘日线、复权口径变化整只重下
# ---------------------------------------------------------------------------

def _bars(dates, close0=100.0, step=1.0):
    return pd.DataFrame({"date": [f"{d}T04:00:00Z" for d in dates], "open": close0, "high": close0, "low": close0,
                         "close": [close0 + i * step for i in range(len(dates))], "volume": 1000})


def test_us_daily_drops_unfinished_bar():
    from aiquant.data import us_daily as ud
    df = _bars(["2026-10-21", "2026-10-22", "2026-10-23"])
    out = ud.closed_only(df, date(2026, 10, 23))
    assert list(ud.et_date(out["date"]).astype(str)) == ["2026-10-21", "2026-10-22"]
    m = ud.merge(df.iloc[:1], df, date(2026, 10, 23))
    assert len(m) == 2                                              # 当天（未收盘）不进库


def test_us_daily_detects_adjustment_change():
    from aiquant.data import us_daily as ud
    old = _bars(["2026-10-20", "2026-10-21", "2026-10-22"])
    same = old.copy()
    assert not ud.adjustment_changed(old, same)
    split = old.copy()
    split["close"] = split["close"] / 2                             # 拆股后整段历史复权价减半
    assert ud.adjustment_changed(old, split)


def test_us_daily_refresh_merges_and_redownloads(env, monkeypatch, tmp_path):
    from aiquant.data import us_daily as ud
    monkeypatch.setattr(ud, "data_dir", lambda: tmp_path)
    ud._write("AAA", _bars(["2026-10-20", "2026-10-21"]))
    ud._write("BBB", _bars(["2026-10-20", "2026-10-21"]))
    calls = []

    def fake_fetch(symbols, start, end=None, feed="sip"):
        calls.append((tuple(symbols), start))
        if start == ud.START:                                       # 整只重下
            return {"BBB": _bars(["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"], 50, 0.5)}
        return {"AAA": _bars(["2026-10-21", "2026-10-22", "2026-10-23"], 101),
                "BBB": _bars(["2026-10-21", "2026-10-22", "2026-10-23"], 50.5, 0.5)}

    monkeypatch.setattr(ud, "fetch", fake_fetch)
    now = datetime(2026, 10, 23, 13, 40, tzinfo=timezone.utc)      # 美东 10-23 9:40，当天未收盘
    res = ud.refresh(["AAA", "BBB"], now)
    a = ud.load("AAA")
    assert list(ud.et_date(a["date"]).astype(str)) == ["2026-10-20", "2026-10-21", "2026-10-22"]
    assert "复权" in res["BBB"] and len(ud.load("BBB")) == 4         # 复权变化 → 整只重下（也不含当天）
    assert calls[0][0] == ("AAA", "BBB")                           # 一次多品种请求


# ---------------------------------------------------------------------------
# 下单健壮性：被拒不重试、部分成交补足、没有动作的原因
# ---------------------------------------------------------------------------

def _setup_step(monkeypatch, regime_up=True, pos_qty=0.0):
    et.ensure_tables()
    db.ex("DELETE FROM etf_orders")
    db.set_setting("etf_trend_state", {})
    db.set_setting("etf_trend_why", None)
    monkeypatch.setattr(et.http, "paper_orders_unlocked", lambda: True)
    monkeypatch.setattr(et, "_clock", lambda: {"is_open": True})
    monkeypatch.setattr(et, "update_daily_csv", lambda *a, **k: {})
    days = pd.bdate_range("2026-01-01", periods=200).date
    closes = [100 + i * 0.2 for i in range(200)] if regime_up else [200 - i * 0.2 for i in range(200)]
    monkeypatch.setattr(et, "daily_bars", lambda sym, days_=420: pd.DataFrame({"date": days, "close": closes}))
    monkeypatch.setattr(et, "_position", lambda sym: {"qty": pos_qty, "market_value": 0, "avg_entry_price": 0,
                                                      "unrealized_pl": 0})
    monkeypatch.setattr(et, "_account", lambda: {"equity": "100000", "cash": "90000"})


def test_rejected_order_not_retried_same_day(env, monkeypatch):
    _setup_step(monkeypatch)
    n = []

    def boom(sym, side, **kw):
        n.append(1)
        raise RuntimeError("Alpaca Paper 拒绝订单 403: insufficient buying power")

    monkeypatch.setattr(et, "_submit", boom)
    now = datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc)
    st = et.step(now)
    assert "被拒" in st["action"] and st["last_action_date"] == "2026-10-21"
    assert et.get_why()["code"] == "rejected"
    et.step(now)
    et.step(now)
    assert len(n) == 1                                              # 同一天不再重试


def test_partial_fill_topped_up_next_day(env, monkeypatch):
    _setup_step(monkeypatch, pos_qty=10.0)
    db.set_setting("etf_trend_state", {"last_action_date": "2026-10-20",
                                       "entry": {"signal_date": "2026-10-19", "target": 30000.0}})
    db.ex("INSERT INTO etf_orders(ts,symbol,side,notional,status,filled_qty,filled_price,client_order_id,signal_date) "
          "VALUES(?,?,?,?,?,?,?,?,?)", ("2026-10-20T13:35:00Z", "QLD", "buy", 30000, "expired", 100, 100.0, "x-1",
                                        "2026-10-19"))
    sent = []
    monkeypatch.setattr(et, "_submit", lambda sym, side, **kw: sent.append((side, kw)) or {})
    et.step(datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc))
    assert sent and sent[0][0] == "buy" and abs(sent[0][1]["notional"] - 20000) < 1   # 只成交 1 万，补 2 万
    assert et.get_why()["code"] == "topup"
    assert et.topup_needed(30000, 29800) == 0                      # 差额太小不补


def test_why_codes_for_idle_states(env, monkeypatch):
    _setup_step(monkeypatch, regime_up=False)
    monkeypatch.setattr(et, "_submit", lambda *a, **k: {})
    et.step(datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc))
    assert et.get_why()["code"] == "flat"
    db.set_setting("etf_trend_state", {})
    monkeypatch.setattr(et, "_clock", lambda: {"is_open": False, "next_open": "2026-10-22T13:30:00Z"})
    et.step(datetime(2026, 10, 21, 22, 0, tzinfo=timezone.utc))
    w = et.get_why()
    assert w["code"] == "closed" and "10-22 21:30" in w["text"]
    monkeypatch.setattr(et, "_clock", lambda: {"is_open": True})
    et.step(datetime(2026, 10, 22, 13, 31, tzinfo=timezone.utc))   # 美东 9:31
    assert et.get_why()["code"] == "pre_open"
    monkeypatch.setattr(et.http, "paper_orders_unlocked", lambda: False)
    et.step(datetime(2026, 10, 22, 14, 0, tzinfo=timezone.utc))
    assert et.get_why()["code"] == "locked"


# ---------------------------------------------------------------------------
# 美股交易节奏：回测（regime_bt）与模拟盘（step）同一套规则
# ---------------------------------------------------------------------------

def _whipsaw_df(n=400):
    """均线上下来回穿越 → 产生多笔亏损交易，用来触发连亏刹车。"""
    import numpy as np
    idx = pd.bdate_range("2024-01-02", periods=n)
    sig = 100 + 8 * np.sin(np.arange(n) / 6.0)
    trade = 50 + 2 * np.sin(np.arange(n) / 6.0 + 1.6)       # 交易品种与信号错位 → 每笔都亏
    return pd.DataFrame({"sig_close": sig, "open": trade, "close": trade}, index=idx)


def test_regime_bt_matches_plain_switching_without_rhythm():
    import numpy as np

    from aiquant import market as mk
    from aiquant.engine import regime_bt as rb
    df = _whipsaw_df()
    sim = rb.simulate(df, 20, 0.0, market=mk.US_EQUITY, rhythm=None)
    st = rb.regime_series(df["sig_close"], 20, 0.0)
    w = st.shift(1).fillna(0.0)
    assert np.allclose(sim["size"].to_numpy(), w.to_numpy())          # 没有节奏规则时就是普通的均线择时
    assert (sim["blocked"] == "").all()


def test_loss_streak_brake_in_backtest_uses_trading_days():
    from aiquant import market as mk
    from aiquant.engine import regime_bt as rb
    from aiquant.engine.rhythm import Rhythm
    df = _whipsaw_df()
    rh = Rhythm(loss_streak_n=2, loss_streak_pause_hours=20)
    sim = rb.simulate(df, 20, 0.0, market=mk.US_EQUITY, rhythm=rh)
    b = sim[sim["blocked"] != ""]
    assert len(b) and b["blocked"].iloc[0].startswith("连亏")
    assert (sim["size"] > 0).sum() < (rb.simulate(df, 20, 0.0, rhythm=None)["size"] > 0).sum()   # 暂停期间不持仓


def test_live_step_blocks_after_loss_streak_like_backtest(env, monkeypatch):
    from aiquant import market as mk
    from aiquant import rhythm
    monkeypatch.setattr(rhythm, "us_mode", lambda: "active")
    _setup_step(monkeypatch)
    rhythm.reset_gate(rhythm.ETF_KEY)
    sent = []
    monkeypatch.setattr(et, "_submit", lambda sym, side, **kw: sent.append(side) or {})
    # 两笔亏损离场（与回测相同的 Gate：连亏 2 笔 → 暂停 20 个交易日）
    for d in ("2026-10-12", "2026-10-14"):
        rhythm.live_on_exit(rhythm.ETF_KEY, datetime.fromisoformat(d + "T14:00:00+00:00"), "信号离场", -50.0,
                            mk.US_EQUITY)
    et.step(datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc))
    assert not sent and et.get_why()["code"] == "rhythm" and "连亏" in et.get_why()["text"]
    rhythm.reset_gate(rhythm.ETF_KEY)


def test_live_step_vol_sizing_and_long_pause(env, monkeypatch):
    from aiquant import rhythm
    _setup_step(monkeypatch)
    rhythm.reset_gate(rhythm.ETF_KEY)
    sent = []
    monkeypatch.setattr(et, "_submit", lambda sym, side, **kw: sent.append(kw) or {})
    monkeypatch.setattr(et, "_rhythm_gate", lambda sym, now: (None, 0.5))
    et.step(datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc))
    assert abs(sent[0]["notional"] - 15000) < 1                      # 30% × 波动率系数 0.5
    monkeypatch.undo()
    _setup_step(monkeypatch)
    monkeypatch.setattr(rhythm, "us_mode", lambda: "active")
    sent.clear()
    monkeypatch.setattr(et, "_submit", lambda sym, side, **kw: sent.append(kw) or {})
    monkeypatch.setattr(rhythm, "etf_long_metrics", lambda compute=False: {"ok": False, "mean_ret": -3.0,
                                                                          "pos_years": 1, "n_years": 6})
    et.step(datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc))
    assert not sent and et.get_why()["code"] == "long_pause"


def test_etf_long_metrics_yearly(env, monkeypatch):
    import numpy as np

    from aiquant import challenger, rhythm
    idx = pd.bdate_range("2016-01-04", "2021-12-31")
    up = 100 * np.exp(np.arange(len(idx)) * 0.0005)
    monkeypatch.setattr(challenger, "_etf_data", lambda: pd.DataFrame({"sig_close": up, "open": up, "close": up}, index=idx))
    db.set_setting("rhythm_long::etf", None)
    m = rhythm.etf_long_metrics(compute=True)
    assert m["ok"] and m["n_years"] >= 4 and m["pos_years"] == m["n_years"]
    assert rhythm.etf_long_metrics()["hist_end"] == "2021-12-31"       # 只读缓存


def test_us_rhythm_shadow_mode_never_blocks_but_records(env, monkeypatch):
    """影子模式：照常计算连亏刹车 / 长周期体检，但不阻止下单、不影响回测，只记录「如果生效会怎样」。"""
    from aiquant import market as mk
    from aiquant import rhythm
    from aiquant.console import audit_log
    rhythm.set_config(us_mode_at=None)
    rhythm.ensure_us_shadow_default()
    assert rhythm.us_mode() == "shadow" and rhythm.is_shadow(mk.US_EQUITY)
    assert any(a["action"] == "美股节奏模式" for a in audit_log(20, key="rhythm_us"))
    assert rhythm.current(mk.US_EQUITY) is None and rhythm.configured(mk.US_EQUITY) is not None
    assert rhythm.current(mk.CRYPTO) is not None                       # 加密货币不受影响
    _setup_step(monkeypatch)
    rhythm.reset_gate(rhythm.ETF_KEY)
    for d in ("2026-10-12", "2026-10-14"):
        rhythm.live_on_exit(rhythm.ETF_KEY, datetime.fromisoformat(d + "T14:00:00+00:00"), "信号离场", -50.0,
                            mk.US_EQUITY)
    sent = []
    monkeypatch.setattr(et, "_submit", lambda sym, side, **kw: sent.append(kw) or {})
    et.step(datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc))
    assert sent and abs(sent[0]["notional"] - 30000) < 1               # 影子：照常买入，不缩仓
    sh = db.get_setting("etf_rhythm_shadow")
    assert "影子模式" in sh["note"] and "连亏" in sh["note"]
    # 影子模式下 ETF 回测不带节奏规则（回测与模拟盘一致）
    from aiquant import challenger
    import numpy as np
    idx = pd.bdate_range("2020-01-02", periods=400)
    df = pd.DataFrame({"sig_close": 100 + 8 * np.sin(np.arange(400) / 6.0), "open": 50.0, "close": 50.0}, index=idx)
    assert challenger.etf_backtest(df, 20, 0.0, "2020-01-01", "2021-12-31")["blocked"] == 0
    with pytest.raises(PermissionError):
        rhythm.set_us_mode("active", actor="g", role="viewer")
    assert rhythm.set_us_mode("active", actor="t", role="trader")["result"] == "已切换"
    assert rhythm.current(mk.US_EQUITY) is not None
    rhythm.set_us_mode("shadow", actor="t", role="trader")
    rhythm.reset_gate(rhythm.ETF_KEY)
