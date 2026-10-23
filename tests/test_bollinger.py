"""布林带：衍生指标无未来数据、分析状态、日线中轨离场升级。全部合成数据。"""
import json

import numpy as np
import pandas as pd

from aiquant import bollinger as B
from aiquant import db, upgrades
from aiquant.engine import indicators as I
from aiquant.strategy.schema import validate


def _df(n=400, seed=1, drift=0.0, vol=0.01):
    r = np.random.default_rng(seed)
    c = pd.Series(100 * np.exp(np.cumsum(r.normal(drift, vol, n))))
    t = pd.date_range("2025-01-01", periods=n, freq="4h", tz="UTC")
    return pd.DataFrame({"open_time": t, "close_time": t + pd.Timedelta(hours=4), "open": c.shift(1).fillna(c[0]),
                         "high": c * 1.002, "low": c * 0.998, "close": c, "volume": 1.0})


def test_derived_indicators_match_bands_and_no_lookahead():
    df = _df()
    p = {"period": 20, "std": 2}
    up, lo, mid = (I.compute(df, t, p) for t in ("BB_UPPER", "BB_LOWER", "BB_MID"))
    pb, w = I.compute(df, "BB_PCTB", p), I.compute(df, "BB_WIDTH", p)
    i = 300
    assert abs(pb[i] - (df.close[i] - lo[i]) / (up[i] - lo[i]) * 100) < 1e-9
    assert abs(w[i] - (up[i] - lo[i]) / mid[i] * 100) < 1e-9
    sp = {"period": 20, "std": 2, "lookback": 120, "recent": 5}
    full = I.compute(df, "BB_SQUEEZE", sp)
    part = I.compute(df.iloc[:i + 1], "BB_SQUEEZE", sp)
    assert abs(full[i] - part.iloc[-1]) < 1e-9
    rk = I.compute(df, "BB_WIDTH_RANK", {"period": 20, "std": 2, "lookback": 120}).dropna()
    assert rk.between(0, 100).all()


def test_schema_accepts_new_indicators():
    s = {"name": "收口突破", "signal_tf": 240, "indicators": [
        {"id": "bbu", "type": "BB_UPPER", "params": {"period": 20, "std": 2}},
        {"id": "sq", "type": "BB_SQUEEZE", "params": {"period": 20, "std": 2, "lookback": 120, "recent": 5}},
        {"id": "pb", "type": "BB_PCTB", "params": {"period": 20, "std": 2}}],
        "entry": {"mode": "all", "conditions": [{"left": {"ref": "close"}, "op": ">", "right": {"ref": "bbu"}},
                                                {"left": {"ref": "sq"}, "op": "<", "right": {"value": 20}}]},
        "exit": {"mode": "any", "conditions": [{"left": {"ref": "pb"}, "op": "<", "right": {"value": 50}}]},
        "stop_loss_pct": 5}
    assert not validate(s)[1]
    bad = json.loads(json.dumps(s))
    bad["indicators"][1]["params"]["recent"] = 99
    assert validate(bad)[1]


def test_state_labels():
    up = B.state(_df(200, drift=0.004, vol=0.004), 240)
    assert up["ok"] and up["label"] == "上升趋势" and up["pctb"] > 50
    dn = B.state(_df(200, drift=-0.004, vol=0.004), 240)
    assert dn["label"] == "下降趋势"
    # 先正常波动，最后一段几乎不动 → 收口
    d = _df(220, vol=0.01)
    d.loc[190:, "close"] = d.loc[189, "close"] * (1 + np.random.default_rng(3).normal(0, 0.0005, 30))
    sq = B.state(d, 240)
    assert sq["rank"] is not None and sq["rank"] <= 15 and any(n == "收口" for n, _, _ in sq["signals"])
    assert not B.state(_df(20), 240)["ok"]


def test_mid_exit_candidate():
    sch = {"name": "策略1", "signal_tf": 1440, "indicators": [
        {"id": "ema9", "type": "EMA", "params": {"period": 9}}, {"id": "ema21", "type": "EMA", "params": {"period": 21}}],
        "entry": {"mode": "all", "conditions": [{"left": {"ref": "ema9"}, "op": "cross_above", "right": {"ref": "ema21"}}]},
        "exit": {"mode": "any", "conditions": [{"left": {"ref": "ema9"}, "op": "cross_below", "right": {"ref": "ema21"}}]},
        "trailing": {"activate_pct": 5, "trail_pct": 4}}
    obj, errs = validate(sch)
    assert not errs
    d = obj.model_dump()
    assert upgrades._is_daily_ema_cross(d, 1440)
    assert not upgrades._is_daily_ema_cross(d, 240)
    cand = upgrades.bb_mid_exit_candidate(d)
    assert not validate(cand)[1]
    assert not upgrades._is_daily_ema_cross(cand, 1440)          # 已有中轨离场，不重复升级
    assert len(cand["exit"]["conditions"]) == 2 and cand["entry"] == d["entry"]


def test_upgrades_run_once(env, monkeypatch):
    calls = []
    monkeypatch.setattr(upgrades, "UPGRADES", [("t_once", lambda: calls.append(1) or {"ok": 1})])
    db.set_setting("upgrade::t_once", None)
    assert upgrades.run_pending() and upgrades.run_pending() == []
    assert calls == [1]
