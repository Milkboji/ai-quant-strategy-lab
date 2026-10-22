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
