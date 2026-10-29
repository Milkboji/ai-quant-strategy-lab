"""交易节奏：冷却 / 连亏刹车 / 波动率定仓 / 每日上限 / 长周期体检。全部离线合成数据。"""
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from aiquant import db, evolve, rhythm
from aiquant.engine import backtest as bt
from aiquant.engine.rhythm import Gate, Rhythm, daily_context, lookup
from aiquant.strategy.schema import validate

T0 = pd.Timestamp("2025-01-01", tz="UTC")


def _walk(days=200, seed=3, vol_spike_from=None):
    rng = np.random.default_rng(seed)
    n = days * 288
    sig = np.full(n, 0.002)
    if vol_spike_from is not None:          # 平静期年化约 23%，之后年化约 130%
        sig[:] = 0.0007
        sig[vol_spike_from * 288:] = 0.004
    r = rng.normal(0, sig)
    c = 30000 * np.exp(np.cumsum(r))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.0007, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.0007, n)))
    ts = pd.date_range(T0, periods=n, freq="5min", tz="UTC")
    return pd.DataFrame({"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1.0})


SCH = {"name": "快线交叉", "signal_tf": 60, "indicators": [
    {"id": "f", "type": "EMA", "params": {"period": 5}}, {"id": "s", "type": "EMA", "params": {"period": 20}}],
    "entry": {"mode": "all", "conditions": [{"left": {"ref": "f"}, "op": "cross_above", "right": {"ref": "s"}}]},
    "exit": {"mode": "any", "conditions": [{"left": {"ref": "f"}, "op": "cross_below", "right": {"ref": "s"}}]},
    "stop_loss_pct": 0.5}


def test_gate_cooldown_streak_and_cap():
    g = Gate(Rhythm(cooldown_hours_after_stop=72, loss_streak_n=3, loss_streak_pause_hours=72, max_entries_per_day=1))
    t = T0
    g.on_exit(t, "止损", -10)
    assert "止损后冷却" in g.check(t + timedelta(hours=71))
    assert g.check(t + timedelta(hours=73)) is None
    g2 = Gate(Rhythm(loss_streak_n=3, loss_streak_pause_hours=48))
    for k in range(2):
        g2.on_exit(t + timedelta(hours=k), "退出规则", -1)
    assert g2.check(t + timedelta(hours=3)) is None          # 2 连亏不刹车
    g2.on_exit(t + timedelta(hours=3), "退出规则", -1)
    assert "连亏" in g2.check(t + timedelta(hours=10))
    assert g2.streak == 0                                    # 刹车后重新计数
    g2.on_exit(t + timedelta(hours=60), "退出规则", 5)
    assert g2.streak == 0
    g.on_entry(t + timedelta(days=5, hours=1))
    assert "今日开仓" in g.check(t + timedelta(days=5, hours=9))
    assert g.check(t + timedelta(days=6, hours=1)) is None


def test_vol_sizing_uses_only_closed_days():
    df = _walk(60, vol_spike_from=40)
    rh = Rhythm(vol_target_pct=40)
    ctx = daily_context(df, rh)
    ok, vm = lookup(ctx, [T0 + timedelta(days=39, hours=23), T0 + timedelta(days=55)])
    assert vm[0] == 1.0                    # 平静期（年化约 20%）不缩仓，也看不到第 40 天以后的波动
    assert 0.3 <= vm[1] < 0.6              # 高波动期（年化 >100%）明显缩仓，不低于下限
    # 当天尚未收盘的日线不可用：第 40 天 00:00 之前只能看到第 39 天及更早
    ctx2 = daily_context(df[df["ts"] < T0 + timedelta(days=45)], rh)
    _, a = lookup(ctx2, [T0 + timedelta(days=40)])
    _, b = lookup(ctx, [T0 + timedelta(days=40)])
    assert a[0] == b[0]


def test_backtest_rhythm_reduces_overtrading():
    df = _walk(200)
    obj, errs = validate(SCH)
    assert not errs
    cfg = dict(start=(T0 + timedelta(days=30)).to_pydatetime(), end=(T0 + timedelta(days=199)).to_pydatetime())
    base = bt.run(df, obj, 60, None, bt.BTConfig(**cfg))["metrics"]
    rh = bt.run(df, obj, 60, None, bt.BTConfig(**cfg, rhythm=Rhythm(cooldown_hours_after_stop=72, loss_streak_n=3,
                                                                    loss_streak_pause_hours=72)))["metrics"]
    assert base["signals_skipped_by_rhythm"] == 0
    assert rh["signals_skipped_by_rhythm"] > 0
    assert rh["trades"] < base["trades"]
    assert rh["total_cost"] < base["total_cost"]


def test_live_gate_persists_across_restarts(env):
    did = 990001
    rhythm.reset_gate(did)
    now = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)
    df = _walk(40)
    assert rhythm.live_gate(did, now, df)[0] is None
    msg = rhythm.live_on_exit(did, now, "止损/移动止损", -50.0)
    assert msg and "止损后冷却" in msg
    why, vm, _ = rhythm.live_gate(did, now + timedelta(hours=10), df)   # 新的读取 = 模拟重启后
    assert why and "冷却" in why
    assert rhythm.live_gate(did, now + timedelta(hours=73), df)[0] is None
    rhythm.set_config(enabled=False)
    try:
        rhythm.live_on_exit(did, now, "止损", -1)
        assert rhythm.live_gate(did, now + timedelta(hours=1), df) == (None, 1.0, {})
    finally:
        rhythm.set_config(enabled=True)
        rhythm.reset_gate(did)


def _fake_paper_dep(vid: int) -> int:
    did = db.ex("INSERT INTO paper_deployments(version_id,config_json,broker,status,created_at,updated_at,is_test) "
                "VALUES(?,?,?,?,?,?,1)", (vid, json.dumps({"label": "长周期测试", "signal_tf": 240, "budget_usd": 1000}),
                                          "alpaca_paper", "running", db.now_iso(), db.now_iso()))
    return did


def test_long_veto_pauses_and_resumes(env):
    from aiquant.strategy import registry
    v = registry.freeze(None, dict(SCH, signal_tf=240), source="test", new_lineage=True, set_current=False)
    did = _fake_paper_dep(v["id"])
    key = rhythm._long_key(v["id"], 240, None)
    bad = {"rules": rhythm.rules_hash(), "years": [], "mean_ret": -4.0, "mean_dd": 15.0, "pos_years": 1,
           "n_years": 6, "ok": False}
    try:
        db.set_setting(key, bad)
        evolve.health_check()
        d = db.q1("SELECT status, config_json FROM paper_deployments WHERE id=?", (did,))
        assert d["status"] == "paused_entries"
        assert json.loads(d["config_json"])["auto_pause"]["kind"] == "long"
        from aiquant.paper import runner as pr
        assert evolve.plan_budgets(100000, [pr.get_deployment(did)])[did]["budget"] == 0   # 额度释放
        evolve.health_check()                                  # 幂等：仍暂停
        assert db.q1("SELECT status FROM paper_deployments WHERE id=?", (did,))["status"] == "paused_entries"
        good = dict(bad, mean_ret=6.0, mean_dd=10.0, pos_years=5, ok=True)
        db.set_setting(key, good)
        evolve.health_check()
        assert db.q1("SELECT status FROM paper_deployments WHERE id=?", (did,))["status"] == "running"
        from aiquant.paper import runner as pr
        plan = evolve.plan_budgets(100000, [pr.get_deployment(did)])
        assert "长周期" in plan[did]["why"] and plan[did]["weight"] == 1.2
    finally:
        db.ex("UPDATE paper_deployments SET status='stopped' WHERE id=?", (did,))


def test_backtest_paused_converted_to_long(env):
    from aiquant.strategy import registry
    v = registry.freeze(None, dict(SCH, signal_tf=240, stop_loss_pct=0.6), source="test", new_lineage=True,
                        set_current=False)
    did = _fake_paper_dep(v["id"])
    cfg = {"label": "旧暂停", "signal_tf": 240, "budget_usd": 5900,
           "auto_pause": {"kind": "backtest", "reason": "x", "version_id": v["id"], "at": db.now_iso()}}
    db.ex("UPDATE paper_deployments SET status='paused_entries', config_json=? WHERE id=?", (json.dumps(cfg), did))
    db.set_setting(rhythm._long_key(v["id"], 240, None), {"rules": rhythm.rules_hash(), "years": [], "mean_ret": -3.0,
                                                          "mean_dd": 6.0, "pos_years": 1, "n_years": 6, "ok": False})
    try:
        out = evolve.health_check()
        assert f"#{did} 转为长周期暂停" in out
        d = db.q1("SELECT status, config_json FROM paper_deployments WHERE id=?", (did,))
        assert d["status"] == "paused_entries" and json.loads(d["config_json"])["auto_pause"]["kind"] == "long"
        assert f"#{did} 转为长周期暂停" not in evolve.health_check()      # 只转换一次
    finally:
        db.ex("UPDATE paper_deployments SET status='stopped' WHERE id=?", (did,))


# ---------------------------------------------------------------------------
# 市场配置：交易日历、交易日时长、年化口径、统一回测配置入口
# ---------------------------------------------------------------------------

def test_market_calendar_and_trading_day_durations():
    from datetime import date

    import pandas as pd

    from aiquant import market as mk
    assert not mk.is_trading_day(date(2026, 4, 3))                  # 耶稣受难日
    assert not mk.is_trading_day(date(2025, 1, 9))                  # 国葬日
    assert not mk.is_trading_day(date(2026, 7, 3))                  # 独立日（周六）提前到周五休市
    assert mk.is_half_day(date(2026, 11, 27)) and mk.is_half_day(date(2025, 7, 3))
    assert mk.session_close(date(2026, 11, 27)).hour == 18            # 13:00 ET = 18:00 UTC（冬令时）
    # 周五收盘后止损、冷却 3 个交易日 → 周一二三不开仓，周四美东开盘起恢复
    t = pd.Timestamp("2026-10-23 20:30Z")
    assert mk.US_EQUITY.after(t, 3) == pd.Timestamp("2026-10-29 13:30Z")
    assert mk.CRYPTO.after(t, 72) == t + pd.Timedelta(hours=72)
    # 感恩节（11-26）顺延
    assert mk.US_EQUITY.after(pd.Timestamp("2026-11-25 21:00Z"), 1) == pd.Timestamp("2026-11-30 14:30Z")
    assert mk.of_symbol("BTC/USD") is mk.CRYPTO and mk.of_symbol("QLD") is mk.US_EQUITY


def test_gate_and_daily_context_follow_market():
    import numpy as np
    import pandas as pd

    from aiquant import market as mk
    from aiquant.engine.rhythm import Gate, Rhythm, daily_context
    rh = Rhythm(cooldown_hours_after_stop=3, vol_target_pct=40, vol_lookback_days=20)
    g = Gate(rh, mk.US_EQUITY)
    g.on_exit(pd.Timestamp("2026-10-23 20:30Z"), "止损", -1.0)
    assert g.check(pd.Timestamp("2026-10-28 15:00Z"))                  # 周三仍在冷却
    assert g.check(pd.Timestamp("2026-10-29 14:00Z")) is None          # 周四开盘后恢复
    rng = np.random.default_rng(1)
    days = pd.bdate_range("2026-01-02", periods=60, tz="America/New_York") + pd.Timedelta(hours=16)
    df = pd.DataFrame({"ts": days.tz_convert("UTC"), "close": 100 * np.exp(np.cumsum(rng.normal(0, 0.04, 60)))})
    us = daily_context(df, rh, mk.US_EQUITY)
    cr = daily_context(df.assign(ts=df["ts"].dt.floor("D") + pd.Timedelta(hours=12)), rh, mk.CRYPTO)
    # 同样的日收益：美股年化按 √252，加密货币按 √365 → 美股波动率更低、仓位系数更高
    assert us["vol_mult"].iloc[-1] > cr["vol_mult"].iloc[-1]
    assert us["close_time"].iloc[0].hour in (20, 21)                   # 美东 16:00 收盘


def test_lab_backtest_uses_same_rhythm_as_evolution(env):
    from aiquant import market as mk
    from aiquant import research, rhythm
    r = research.get_research()
    sp = research.get_split()
    s, e = research.md.to_utc(sp["val_start"]), research.md.to_utc(sp["test_start"])
    cfg = research.bt_config(s, e, r)
    assert cfg.rhythm == rhythm.current() and cfg.rhythm is not None
    assert cfg.market is mk.CRYPTO and cfg.cost.fee_bps == float(r["fee_bps"])
    us = research.bt_config(s, e, dict(r, symbol="QLD"))
    assert us.market is mk.US_EQUITY and us.cost.fee_bps == 0 and us.cost.sell_fee_bps > 0
    k1 = research.params_key(1, 60, None, s, e, r, "test")
    rhythm.set_config(enabled=False)
    try:
        assert research.params_key(1, 60, None, s, e, r, "test") != k1   # 节奏规则变化 → 不复用旧回测
    finally:
        rhythm.set_config(enabled=True)


def test_calendar_refresh_uses_alpaca_and_backs_off(env, monkeypatch):
    from datetime import date

    from aiquant import db
    from aiquant import market as mk
    from aiquant.data import alpaca
    calls = []

    def fake_get(path, params=None):
        calls.append(path)
        return 200, [{"date": "2030-01-02", "open": "09:30", "close": "16:00"},
                     {"date": "2030-01-04", "open": "09:30", "close": "13:00"}]

    monkeypatch.setattr(alpaca, "paper_get", fake_get)
    db.set_setting("us_calendar", None)
    db.set_setting("us_calendar_attempt", None)
    assert mk.refresh_calendar() is True
    assert mk.is_half_day(date(2030, 1, 4)) and not mk.is_trading_day(date(2030, 1, 3))   # 日历里没有 → 休市
    assert mk.refresh_calendar() is False and len(calls) == 1                              # 一周内不重复拉
    db.set_setting("us_calendar", None)
    mk.invalidate_cache()
    monkeypatch.setattr(alpaca, "paper_get", lambda path, params=None: (calls.append(path) or (None, None)))
    db.set_setting("us_calendar_attempt", None)
    assert mk.refresh_calendar() is False and mk.refresh_calendar() is False
    assert len(calls) == 2                                                                  # 失败后 6 小时内不再请求
    assert mk.is_trading_day(date(2030, 1, 3))                                              # 回到内置规则


def test_us_loss_streak_pause_counts_trading_days_across_weekend_and_holidays(env, monkeypatch):
    """美股连亏刹车「2 笔停 20」的单位是交易日（不是小时）：周末、感恩节、圣诞、元旦都不计入，
    第 20 个交易日收盘后、下一个交易日开盘起恢复。回测（Gate）与模拟盘（live_on_exit）同一结果。"""
    from aiquant import market as mk
    rh = rhythm.RULES_US_V1
    assert rh.loss_streak_n == 2 and rh.loss_streak_pause_hours == 20 and mk.US_EQUITY.duration_unit == "trading_days"
    assert "20 个交易日" in "；".join(rh.describe(mk.US_EQUITY.unit_cn))
    # 周五（2026-11-20）收盘后第 2 笔亏损离场：20 个交易日 = 11-23 … 12-21（跳过周末和 11-26 感恩节，11-27 半日市算 1 天）
    g = Gate(rh, mk.US_EQUITY)
    g.on_exit(pd.Timestamp("2026-11-19 21:05Z"), "信号离场", -1.0)
    g.on_exit(pd.Timestamp("2026-11-20 21:05Z"), "信号离场", -1.0)
    assert g.block_until == pd.Timestamp("2026-12-22 14:30Z")            # 12-22 美东 9:30 开盘起恢复
    assert g.check(pd.Timestamp("2026-11-23 15:00Z"))                    # 若按 20 小时，周一早就恢复了
    assert g.check(pd.Timestamp("2026-12-21 20:00Z"))                    # 第 20 个交易日仍暂停
    assert g.check(pd.Timestamp("2026-12-22 14:30Z")) is None
    # 跨圣诞（12-25）和元旦（2027-01-01）：12-11 离场 → 2027-01-13 开盘起恢复
    assert mk.US_EQUITY.after(pd.Timestamp("2026-12-11 21:05Z"), 20) == pd.Timestamp("2027-01-13 14:30Z")
    assert mk.CRYPTO.after(pd.Timestamp("2026-12-11 21:05Z"), 20) == pd.Timestamp("2026-12-12 17:05Z")   # 加密货币仍按小时
    # 模拟盘路径（生效模式下）：同一个 Gate，结果一致
    monkeypatch.setattr(rhythm, "us_mode", lambda: "active")
    db.set_setting("rhythm_state::etf_unit_test", None)
    rhythm.live_on_exit("etf_unit_test", datetime(2026, 11, 19, 21, 5, tzinfo=timezone.utc), "信号离场", -1.0, mk.US_EQUITY)
    msg = rhythm.live_on_exit("etf_unit_test", datetime(2026, 11, 20, 21, 5, tzinfo=timezone.utc), "信号离场", -1.0,
                              mk.US_EQUITY)
    assert msg and "连亏" in msg
    s = db.get_setting("rhythm_state::etf_unit_test")
    assert pd.Timestamp(s["block_until"]) == pd.Timestamp("2026-12-22 14:30Z")
    db.set_setting("rhythm_state::etf_unit_test", None)
