"""核心单元测试：解析、Schema、行情聚合、回测语义、成本、版本。"""
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from aiquant.data import market_data as md
from aiquant.engine import backtest as bt
from aiquant.engine.execution import CostModel, fill, max_buy_qty
from aiquant.strategy import parser
from aiquant.strategy.schema import schema_hash, validate
from aiquant.strategy.templates import TEMPLATES

UTC = timezone.utc


# ---------------- 解析与 Schema ----------------

@pytest.mark.parametrize("name", list(TEMPLATES))
def test_templates_valid_and_parseable(name):
    obj, errs = validate(TEMPLATES[name]["schema"])
    assert obj and not errs
    r = parser.parse(TEMPLATES[name]["text"])
    assert r.status == "ok", (r.errors, r.unresolved)


def test_parse_example_sentence():
    r = parser.parse("当BTC的EMA20向上突破EMA60时买入，反向交叉时平仓，固定止损2%，盈利3%后启动移动止损。")
    assert r.status == "ok"
    s = r.schema
    assert s["entry"]["conditions"][0]["op"] == "cross_above"
    assert s["exit"]["conditions"][0]["op"] == "cross_below"
    assert s["stop_loss_pct"] == 2.0
    assert s["trailing"]["activate_pct"] == 3.0
    assert r.warnings  # 未指定回撤幅度需提示


def test_parse_unknown_is_not_guessed():
    r = parser.parse("随便买点什么")
    assert r.status != "ok"
    assert r.errors


def test_parse_multi_timeframe():
    r = parser.parse(TEMPLATES["多周期策略"]["text"])
    assert r.schema["signal_tf"] == 15 and r.schema["aux_tf"] == 240


def test_schema_rejects_bad_refs_and_no_exit():
    bad = {"name": "x", "indicators": [], "entry": {"conditions": [{"left": {"ref": "foo"}, "op": ">",
                                                                     "right": {"value": 1}}]},
           "stop_loss_pct": 1}
    assert validate(bad)[1]
    noexit = {"name": "x", "indicators": [{"id": "e", "type": "EMA", "params": {"period": 10}}],
              "entry": {"conditions": [{"left": {"ref": "close"}, "op": ">", "right": {"ref": "e"}}]}}
    assert any("退出" in e for e in validate(noexit)[1])
    bad_param = dict(TEMPLATES["趋势跟踪"]["schema"])
    bad_param = {**bad_param, "indicators": [{"id": "ema20", "type": "EMA", "params": {"period": 99999}},
                                             {"id": "ema60", "type": "EMA", "params": {"period": 60}}]}
    assert validate(bad_param)[1]


def test_schema_hash_stable():
    a, _ = validate(TEMPLATES["趋势跟踪"]["schema"])
    b, _ = validate(TEMPLATES["趋势跟踪"]["schema"])
    assert schema_hash(a) == schema_hash(b)


# ---------------- 行情 ----------------

def _bars(start, n, price=100.0, step=0.0, missing=()):
    idx = pd.date_range(start, periods=n, freq="5min", tz="UTC")
    p = price + step * np.arange(n)
    df = pd.DataFrame({"ts": idx, "open": p, "high": p + 0.5, "low": p - 0.5, "close": p, "volume": 1.0,
                       "trade_count": 1, "vwap": p})
    return df.drop(index=list(missing)).reset_index(drop=True)


def test_resample_utc_boundaries_and_incomplete():
    df = _bars("2025-01-01 00:00", 60)  # 5 小时
    k = md.resample(df, 60)
    assert list(k["open_time"].dt.hour) == [0, 1, 2, 3, 4]
    assert k["complete"].all()
    k2 = md.resample(df, 60, data_end=pd.Timestamp("2025-01-01 04:30", tz="UTC"))
    assert not k2["complete"].iloc[-1]
    k4 = md.resample(df, 240)
    assert k4["open_time"].iloc[0].hour == 0 and k4["n_5m"].iloc[0] == 48


def test_no_fake_bars_for_gaps():
    df = _bars("2025-01-01 00:00", 24, missing=range(12, 24))
    k = md.resample(df, 60)
    assert len(k) == 1  # 第二个小时无数据，不生成K线


def test_clean_and_quality():
    df = _bars("2025-01-01", 10)
    df = pd.concat([df, df.iloc[[3]]])
    df.loc[df.index[0], "high"] = 0
    c, rep = md.clean_bars(df)
    assert rep["duplicates_removed"] == 1 and rep["invalid_rows_removed"] == 1
    q = md.quality_report(c, datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 1, 1, 1, tzinfo=UTC))
    assert q["expected_5m_bars"] == 12 and q["missing_5m_bars"] >= 2


def test_missing_ranges():
    s = datetime(2025, 1, 1, tzinfo=UTC)
    cov = [(s, s + timedelta(days=1)), (s + timedelta(days=2), s + timedelta(days=3))]
    m = md.missing_ranges(s, s + timedelta(days=4), cov)
    assert m == [(s + timedelta(days=1), s + timedelta(days=2)), (s + timedelta(days=3), s + timedelta(days=4))]


def test_dataset_hash_tamper_detected(env, tmp_path):
    ds = md.get_dataset(md.to_utc(env["val_start"]) - timedelta(days=30), md.to_utc(env["val_start"]), source="test")
    df = md.load_dataset(ds)
    assert len(df) > 0
    fake = dict(ds)
    p = tmp_path / "x.csv.gz"
    df2 = df.copy()
    df2.loc[5, "close"] += 1
    df2.to_csv(p, index=False, compression="gzip", float_format="%.8f")
    fake["path"] = str(p)
    with pytest.raises(RuntimeError):
        md.load_dataset(fake)


def test_insufficient_data_not_silently_shortened(env):
    with pytest.raises(md.InsufficientData):
        md.get_dataset(datetime(2015, 1, 1, tzinfo=UTC), datetime(2015, 6, 1, tzinfo=UTC), source="cache")


# ---------------- 成本与回测 ----------------

def test_cost_model_charged_once():
    cm = CostModel(fee_bps=25, slippage_bps=2, spread_bps=1)
    f = fill("buy", 100.0, 1.0, cm)
    assert f.price == pytest.approx(100 * (1 + 0.00005 + 0.0002))
    assert f.fee == pytest.approx(f.notional * 0.0025)
    assert f.spread_cost == pytest.approx(0.005) and f.slippage_cost == pytest.approx(0.02)
    q = max_buy_qty(10000, 100, 100, cm, 0.5)
    assert q * f.price * (1 + 0.0025) <= 10000


def _simple_schema(**kw):
    d = {"name": "t", "indicators": [{"id": "dch", "type": "DONCHIAN_HIGH", "params": {"period": 3}},
                                     {"id": "dcl", "type": "DONCHIAN_LOW", "params": {"period": 3}}],
         "entry": {"conditions": [{"left": {"ref": "close"}, "op": ">", "right": {"ref": "dch"}}]},
         "exit": {"conditions": [{"left": {"ref": "close"}, "op": "<", "right": {"ref": "dcl"}}]}}
    d.update(kw)
    o, e = validate(d)
    assert not e, e
    return o


def test_fill_after_signal_no_lookahead():
    df = _bars("2025-01-01", 400, price=100, step=0.5)  # 持续上涨
    s = _simple_schema()
    cfg = bt.BTConfig(start=datetime(2025, 1, 1, 6, tzinfo=UTC), end=datetime(2025, 1, 2, 9, tzinfo=UTC),
                      cost=CostModel(0, 0, 0))
    r = bt.run(df, s, 15, None, cfg)
    t = r["trades"][0]
    assert pd.Timestamp(t["entry_fill_ts"]) >= pd.Timestamp(t["entry_signal_ts"])
    # 成交价 = 信号K线收盘后的第一根5分钟K线开盘价
    fill_open = df.loc[df["ts"] == pd.Timestamp(t["entry_fill_ts"]), "open"].iloc[0]
    assert t["entry_price"] == pytest.approx(fill_open)
    assert t["exit_reason"] == "回测结束平仓"


def test_stop_loss_and_gap_and_no_negative_cash():
    df = _bars("2025-01-01", 300, price=100, step=0.5)
    # 制造一次跳空下跌
    df.loc[250:, ["open", "high", "low", "close"]] -= 120
    s = _simple_schema(stop_loss_pct=2.0)
    cfg = bt.BTConfig(start=datetime(2025, 1, 1, 5, tzinfo=UTC), end=datetime(2025, 1, 2, tzinfo=UTC))
    r = bt.run(df, s, 15, None, cfg)
    stops = [t for t in r["trades"] if t["exit_reason"] == "止损"]
    assert stops
    gap = df.loc[250, "open"]
    assert stops[0]["exit_price"] <= gap  # 跳空按开盘价成交（再扣点差滑点）
    assert all(t["equity_after"] >= 0 for t in r["trades"])
    m = r["metrics"]
    assert m["total_cost"] == pytest.approx(m["fees"] + m["spread_cost"] + m["slippage_cost"])
    assert m["final_equity"] == pytest.approx(10000 + sum(t["pnl"] for t in r["trades"]), rel=1e-9)


def test_buy_and_hold_includes_costs():
    df = _bars("2025-01-01", 100, price=100, step=0.0)
    cfg = bt.BTConfig(start=datetime(2025, 1, 1, tzinfo=UTC), end=datetime(2025, 1, 2, tzinfo=UTC))
    b = bt.buy_and_hold(df, cfg)
    assert b["metrics"]["raw_price_change_pct"] == 0
    assert b["metrics"]["net_return_pct"] < -0.5  # 扣除双边手续费+点差+滑点


def test_aux_alignment_uses_closed_bars_only():
    from aiquant.engine.signals import build_frame
    df = _bars("2025-01-01", 24 * 12 * 2, price=100, step=0.01)
    s = validate(TEMPLATES["多周期策略"]["schema"])[0]
    f = build_frame(df, s, 15, 240)
    g = f.dropna(subset=["aux_close_time"])
    assert len(g) > 100 and (g["aux_close_time"] <= g["close_time"]).all()


def test_signal_dropped_after_long_gap_and_partial_bars():
    df = _bars("2025-01-01", 600, price=100, step=0.5, missing=range(200, 500))  # 25 小时缺口
    s = _simple_schema()
    cfg = bt.BTConfig(start=datetime(2025, 1, 1, 5, tzinfo=UTC), end=datetime(2025, 1, 3, 2, tzinfo=UTC),
                      cost=CostModel(0, 0, 0))
    r = bt.run(df, s, 60, None, cfg)
    for t in r["trades"]:
        delay = pd.Timestamp(t["entry_fill_ts"]) - pd.Timestamp(t["entry_signal_ts"])
        assert delay <= pd.Timedelta(minutes=15)
    k = md.resample(df, 60)
    assert not k[k["n_5m"] < 11]["complete"].any()
