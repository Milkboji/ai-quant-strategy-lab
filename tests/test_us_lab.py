"""美股进入策略实验室：日线快照（SHA256、交易日缺口报告）、按市场分区、主引擎日线 + 跨品种信号、
与 ETF 择时回测同口径、实验室回测全流程、美股版本不能部署到 BTC 模拟盘、进化不再覆盖策略位指针。"""
import numpy as np
import pandas as pd
import pytest

from aiquant import actions, config, console, db, jobs, research
from aiquant import market as mk
from aiquant.data import market_data as md
from aiquant.data import us_daily
from aiquant.engine import backtest as bt
from aiquant.engine import regime_bt
from aiquant.strategy import parser, registry


def _write(sym, closes, start="2016-01-04"):
    days = [d for d in pd.bdate_range(start, periods=len(closes) * 2).date if mk.is_trading_day(d)][:len(closes)]
    out = us_daily.data_dir()
    out.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": [f"{d}T05:00:00Z" for d in days], "open": closes, "high": np.array(closes) * 1.01,
                  "low": np.array(closes) * 0.99, "close": closes, "volume": 1000}).to_csv(out / f"{sym}.csv", index=False)
    return days


@pytest.fixture(scope="module")
def us_data(env):
    rng = np.random.default_rng(11)
    r = rng.normal(0.0004, 0.012, 2800) + 0.01 * np.sin(np.arange(2800) / 60.0)
    qqq = 100 * np.cumprod(1 + r)
    qld = 50 * np.cumprod(1 + 2 * r)
    days = _write("QQQ", qqq)
    _write("QLD", qld)
    return days


def _schema(**kw):
    res = parser.parse("当QQQ收盘价高于1.02倍150日均线时买入，QQQ收盘价低于0.98倍150日均线时卖出")
    assert res.status == "ok"
    return dict(res.schema, name=kw.get("name", "美股择时测试"))


def test_us_dataset_snapshot_and_calendar_gaps(us_data):
    s, e = md.to_utc("2020-01-01"), md.to_utc("2021-01-01")
    ds = us_daily.get_dataset("QLD", s, e)
    assert ds["venue"] == "alpaca_stocks" and ds["timeframe_min"] == 1440 and len(ds["sha256"]) == 64
    df = md.load_dataset(ds)                                   # 哈希校验通过
    assert "close_time" in df.columns
    q = __import__("json").loads(ds["quality_json"])
    assert q["missing_days"] == 0 and q["half_days"] >= 2       # 按交易日历检查，含半日市
    assert us_daily.get_dataset("QLD", s, e)["id"] == ds["id"]  # 同数据同区间复用快照
    with pytest.raises(md.InsufficientData):
        us_daily.get_dataset("QLD", md.to_utc("2010-01-01"), e)


def test_us_split_is_fixed_versioned_and_shared(us_data):
    from aiquant import challenger
    sp_c, sp_u = research.get_split(), research.get_split(mk.US_EQUITY)
    assert sp_u["version"] == 1 and sp_u != sp_c
    assert sp_u["train_start"][:10] == "2017-01-01" and sp_u["val_start"][:10] == "2025-07-31"
    days = mk.trading_days(md.to_utc(sp_u["test_start"]).date(), (md.to_utc(sp_u["test_end"]) - pd.Timedelta(days=1)).date())
    assert len(days) == 90                                                  # 测试期 = 90 个交易日
    vs, ts_ = md.to_utc(sp_u["val_start"]), md.to_utc(sp_u["test_start"])
    assert research.split_label(vs, ts_, mk.US_EQUITY) == "验证集"
    assert research.split_label(md.to_utc("2020-01-01"), vs, mk.US_EQUITY) == "训练集"
    assert challenger.etf_select() == ("2017-01-01", "2025-07-30")         # ETF 进化与实验室同一套分区
    db.set_setting("us_split_version", None)
    assert research.us_split()["version"] == 1


def test_engine_daily_matches_regime_timing(us_data):
    """主引擎（日线 + QQQ 出信号、交易 QLD）与 ETF 择时回测在同一份数据上给出相同的买卖日期。"""
    from aiquant.strategy.schema import validate
    obj, errs = validate(_schema())
    assert not errs and obj.signal_symbol == "QQQ"
    s, e = md.to_utc("2017-01-01"), md.to_utc("2024-01-01")
    trade = md.load_dataset(us_daily.get_dataset("QLD", s, e))
    sig = md.load_dataset(us_daily.get_dataset("QQQ", s, e))
    cfg = bt.BTConfig(start=s, end=e, initial_cash=10_000, cost=mk.US_EQUITY.cost(), market=mk.US_EQUITY)
    res = bt.run(trade, obj, 1440, None, cfg, df_signal=sig)
    eng_buys = sorted(pd.to_datetime([t["entry_fill_ts"] for t in res["trades"]], utc=True)
                      .tz_convert(mk.ET).date)
    q, l_ = us_daily.load("QQQ"), us_daily.load("QLD")
    idx = pd.to_datetime(us_daily.et_date(q["date"]))
    df = pd.DataFrame({"sig_close": q["close"].values, "open": l_["open"].values, "close": l_["close"].values}, index=idx)
    sim = regime_bt.simulate(df, 150, 0.02, market=mk.US_EQUITY, rhythm=None)
    rb_buys = [d.date() for d in sim.index[(sim["event"] == "buy") & (sim.index >= "2017-01-01") & (sim.index < "2024-01-01")]]
    assert eng_buys and eng_buys == rb_buys[:len(eng_buys)]
    assert any("下一交易日的开盘价" in a for a in res["assumptions"])
    # 周五收盘出信号 → 下周一（或节假日后第一个交易日）开盘成交
    for t in res["trades"]:
        sig_d = pd.Timestamp(t["entry_signal_ts"]).tz_convert(mk.ET).date()
        fill_d = pd.Timestamp(t["entry_fill_ts"]).tz_convert(mk.ET).date()
        assert fill_d == mk.trading_days_after(sig_d, 1)[0]


def test_lab_backtest_end_to_end_for_us(us_data):
    r0 = research.get_research()
    try:
        research.set_research({"symbol": "QLD", "source": "alpaca", "window_preset": "1095天", "window_anchor": "train_end"})
        v = registry.freeze(4, _schema(name="美股择时实验室"), nl_text="QQQ 均线择时")
        jid, msg = actions.submit_backtest(4, version_id=v["id"])
        assert jid, msg
        jobs.wait_all(120)
        j = jobs.get(jid)
        assert j["status"] == "done", j.get("error")
        b = research.get_backtest(j["result_id"])
        assert b["signal_tf"] == 1440 and b["split_label"] == "训练集"
        ds = db.q1("SELECT * FROM datasets WHERE id=?", (b["dataset_id"],))
        assert ds["venue"] == "alpaca_stocks" and ds["symbol"] == "QLD"
        a = __import__("json").loads(b["assumptions_json"])
        assert a["cost_model"]["fee_bps_per_side"] == 0 and a["cost_model"]["sell_fee_bps"] > 0
        assert research.benchmark_for(b) is not None                 # QLD 买入持有基准
        k, _ = research.chart_data(b)
        assert len(k) > 100
        assert research.version_market(v).key == "us_equity"
        # 策略位里的美股版本可以部署：部署到通用美股日线执行器（同一套显式部署 / 锁定 / 审计）
        res = console.deploy_version("slot:4", v["id"], actor="t", role="trader", broker="mock")
        assert res["result"] == "已启动", res
        d = db.q1("SELECT * FROM paper_deployments WHERE id=?", (res["deployment_id"],))
        cfg = __import__("json").loads(d["config_json"])
        assert d["broker"] == "alpaca_us" and d["is_test"] == 1 and cfg["kind"] == "lab" and cfg["trade_symbol"] == "QLD"
        assert cfg["signal_symbol"] == "QQQ" and d["status"] == "running"
        assert console.active_deployment("slot:4", "mock")["id"] == d["id"]
        assert any(a["action"] == "部署此版本" and "美股日线执行器" in (a["detail"] or "")
                   for a in console.audit_log(20, key="slot:4"))
        from aiquant.paper import runner as pr
        with pytest.raises(ValueError, match="美股部署不支持"):
            pr.flatten(d["id"], confirmed=True)                       # 共用账户：不能用 BTC 的平仓按钮
        console.take_offline("slot:4", actor="t", role="trader", broker="mock")
        assert db.q1("SELECT status FROM paper_deployments WHERE id=?", (d["id"],))["status"] == "stopped"
    finally:
        research.set_research({k: r0[k] for k in ("symbol", "source", "window_preset", "window_anchor")})


def test_us_rejects_intraday_and_aux(us_data):
    r = dict(research.get_research(), symbol="QLD")
    assert research.effective_tf({"timeframe_mode": "60"}, {"signal_tf": 60}, r) == (1440, None)
    with pytest.raises(ValueError):
        research.effective_tf({}, {"aux_tf": 240}, r)


def test_schema_hash_unchanged_without_signal_symbol():
    from aiquant.strategy.schema import schema_hash, validate
    d = {"name": "x", "signal_tf": 60, "indicators": [{"id": "f", "type": "EMA", "params": {"period": 9}}],
         "entry": {"mode": "all", "conditions": [{"left": {"ref": "close"}, "op": ">", "right": {"ref": "f"}}]},
         "stop_loss_pct": 2.0}
    obj, _ = validate(d)
    dumped = obj.model_dump()
    assert "signal_symbol" in dumped
    legacy = {k: v for k, v in dumped.items() if k != "signal_symbol"}
    assert schema_hash(obj) == schema_hash(legacy)                      # 已有版本的哈希保持不变


def test_parser_band_multipliers():
    for txt in ("QQQ收盘价站上150日均线2%以上时买入，跌破150日均线2%以下时卖出",
                "当QQQ收盘价高于1.02倍150日均线时买入，QQQ收盘价低于0.98倍150日均线时卖出"):
        sc = parser.parse(txt).schema
        assert sc["signal_symbol"] == "QQQ" and sc["signal_tf"] == 1440
        assert sc["entry"]["conditions"][0]["right"]["mult"] == pytest.approx(1.02)
        assert sc["exit"]["conditions"][0]["right"]["mult"] == pytest.approx(0.98)


def test_evolution_does_not_move_slot_pointer_when_user_saved_newer(env):
    from aiquant.strategy.templates import TEMPLATES
    v1 = registry.freeze(3, dict(TEMPLATES["趋势跟踪"]["schema"], name="指针测试"), new_lineage=True)
    v2 = registry.freeze(3, dict(TEMPLATES["趋势跟踪"]["schema"], name="指针测试", stop_loss_pct=3.3))
    assert registry.slot_points_to(3, v2["id"]) and not registry.slot_points_to(3, v1["id"])
    # 进化替换的是 v1（模拟盘上的版本），用户刚保存的 v2 仍是策略位当前版本 → 不移动指针
    nv = registry.freeze(3, dict(TEMPLATES["趋势跟踪"]["schema"], name="指针测试", stop_loss_pct=4.4),
                         source="auto_evolve", parent_id=v1["id"], set_current=registry.slot_points_to(3, v1["id"]))
    assert registry.get_slot(3)["current_version_id"] == v2["id"] and nv["id"] != v2["id"]
