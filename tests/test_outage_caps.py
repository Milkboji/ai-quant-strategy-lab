"""服务中断检测与恢复后补检查、健康检查文件、外部心跳 Worker、回测的跳空止损与持仓上限、BTC 持仓上限。"""
import json
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from aiquant import db, uptime

UTC = timezone.utc


def test_outage_recorded_with_missed_checks_and_health_file(env, monkeypatch):
    """距上次心跳超过 3 分钟 → 记一次中断，事件写明时长和错过的检查（BTC K 线检查次数、美股开盘 / 收盘后检查的交易日），
    标记恢复后补检查；健康检查文件只有状态和时间戳。"""
    uptime.ensure_tables()
    db.ex("DELETE FROM service_outages")
    sent = []
    import aiquant.notify as nt
    monkeypatch.setattr(nt, "event", lambda kind, title, body="", dedupe=None: sent.append((kind, body)) or 1)
    db.set_setting("paper_loop_heartbeat", "2026-10-25T15:23:00+00:00")          # 10-25 23:23 北京时间关机
    now = datetime(2026, 10, 28, 13, 36, tzinfo=UTC)                             # 10-28 21:36 开机
    out = uptime.beat(now)
    assert out and out["minutes"] == pytest.approx(70 * 60 + 13)
    if out["missed"]["us_open"]:      # 有美股部署时：10-26、10-27 两天，以及 10-28（13:36 恢复时仍在交易时段内，立即补做）
        assert out["missed"]["us_open"] == ["2026-10-26", "2026-10-27", "2026-10-28"]
        assert "2026-10-28 仍在交易时段内，恢复后立即补做" in out["missed"]["text"]
    row = db.q1("SELECT * FROM service_outages ORDER BY id DESC LIMIT 1")
    assert row["source"] == "heartbeat" and json.loads(row["missed_json"])["text"]
    ev = db.q1("SELECT message FROM paper_events ORDER BY id DESC LIMIT 1")["message"]
    assert "服务中断 70 小时 13 分钟" in ev and "不补单" in ev
    assert db.get_setting("catchup_pending")["minutes"] == pytest.approx(70 * 60 + 13)
    assert sent and sent[0][0] == "watchdog_restart"
    h = json.loads(uptime.health_path().read_text())
    assert set(h) == {"status", "ts"} and h["status"] == "ok" and h["ts"] == int(now.timestamp())
    # 正常间隔（60 秒）不算中断
    db.set_setting("paper_loop_heartbeat", (now + timedelta(minutes=1)).isoformat())
    assert uptime.beat(now + timedelta(minutes=2)) is None
    # 美股交易日是否「服务完整运行」：10-26、10-27 不是，10-29 是
    assert not uptime.full_run_day(date(2026, 10, 26)) and not uptime.full_run_day(date(2026, 10, 27))
    assert uptime.full_run_day(date(2026, 10, 29))
    assert uptime._bar_closes(datetime(2026, 10, 25, 15, 23, tzinfo=UTC), now, 240) == 18


def test_missed_checks_list_btc_and_us(env):
    from aiquant.paper import runner as pr
    did = db.ex("INSERT INTO paper_deployments(version_id,config_json,broker,status,created_at,updated_at,is_test) "
                "VALUES(1,?,?,?,?,?,0)", (json.dumps({"label": "中断测试", "signal_tf": 60}), "alpaca_paper", "running",
                                          db.now_iso(), db.now_iso()))
    us = db.ex("INSERT INTO paper_deployments(version_id,config_json,broker,status,created_at,updated_at,is_test) "
               "VALUES(1,?,?,?,?,?,0)", (json.dumps({"kind": "lab", "trade_symbol": "QLD"}), "alpaca_us", "running",
                                         db.now_iso(), db.now_iso()))
    try:
        m = uptime.missed_checks(datetime(2026, 10, 28, 14, 19, tzinfo=UTC), datetime(2026, 10, 29, 1, 29, tzinfo=UTC))
        b = next(x for x in m["btc"] if x["deployment"] == did)
        assert b["bars"] == 11 and m["us_close"] == ["2026-10-28"] and m["us_open"] == []
        assert "美股收盘后检查 1 个交易日" in m["text"]
    finally:
        pr.stop(did)
        db.ex("UPDATE paper_deployments SET status='stopped' WHERE id=?", (us,))


def test_shadow_days_only_count_full_run_trading_days(env, monkeypatch):
    """美股节奏影子模式「满 20 个交易日」只计服务完整运行的交易日：交易时段内中断过的那天不计。"""
    from aiquant import market as mk
    from aiquant import rhythm
    monkeypatch.setattr(rhythm, "us_mode", lambda: "shadow")
    db.set_setting(rhythm.SHADOW_LOG_KEY, None)
    db.set_setting("us_shadow_count_from", "2026-11-02")
    uptime.ensure_tables()
    db.ex("DELETE FROM service_outages")
    days = mk.trading_days(date(2026, 11, 2), date(2026, 12, 31))[:20]
    for d in days:
        rhythm.shadow_record(str(d), datetime.combine(d, datetime.min.time(), tzinfo=UTC) + timedelta(hours=15))
    assert rhythm.us_promotion_status()["days"] == 20
    s = mk.session_open(days[3]) + timedelta(hours=1)          # 第 4 个交易日开盘后中断 2 小时
    db.ex("INSERT INTO service_outages(start,end,minutes,source,created_at) VALUES(?,?,?,?,?)",
          (s.isoformat(), (s + timedelta(hours=2)).isoformat(), 120, "heartbeat", db.now_iso()))
    ps = rhythm.us_promotion_status()
    assert ps["days"] == 19 and not ps["ready"] and ps["interrupted"] == [str(days[3])] and "中断过" in ps["text"]
    db.ex("DELETE FROM service_outages")
    db.set_setting(rhythm.SHADOW_LOG_KEY, None)


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node")
def test_guard_worker_alerts_after_two_failures_and_on_recovery(tmp_path):
    src = Path(__file__).resolve().parent.parent / "tools" / "cloudflare_worker"
    (tmp_path / "qg.mjs").write_text((src / "quant-guard.js").read_text(encoding="utf-8")
                                     .replace("setTimeout(r, 8000)", "setTimeout(r, 1)"), encoding="utf-8")
    (tmp_path / "t.mjs").write_text((src / "test_guard.mjs").read_text(encoding="utf-8"), encoding="utf-8")
    r = subprocess.run(["node", str(tmp_path / "t.mjs")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "ALL OK" in r.stdout


def _daily(opens, lows, highs, closes, start="2026-01-05"):
    from aiquant import market as mk
    days = [d for d in pd.bdate_range(start, periods=len(opens) * 2).date if mk.is_trading_day(d)][:len(opens)]
    return pd.DataFrame({"ts": pd.to_datetime([mk.session_open(d) for d in days], utc=True),
                         "close_time": pd.to_datetime([mk.session_close(d) for d in days], utc=True),
                         "open": opens, "high": highs, "low": lows, "close": closes, "volume": 1.0})


def _always_long(stop=None, tp=None):
    from aiquant.strategy.schema import validate
    sch = {"name": "t", "signal_tf": 1440, "indicators": [{"id": "m", "type": "SMA", "params": {"period": 2}}],
           "entry": {"mode": "all", "conditions": [{"left": {"ref": "close"}, "op": ">", "right": {"value": 0}}]},
           "exit": {"mode": "any", "conditions": [{"left": {"ref": "close"}, "op": "<", "right": {"value": 0}}]}}
    if stop:
        sch["stop_loss_pct"] = stop
    if tp:
        sch["take_profit_pct"] = tp
    obj, errs = validate(sch)
    assert not errs, errs
    return obj


def test_backtest_gap_below_stop_fills_at_open_not_stop_price():
    """回测：开盘价已经跳空低于止损价 → 按开盘价成交，不按止损价（与券商止损单在开盘触发一致）。"""
    from aiquant import market as mk
    from aiquant.engine import backtest as bt
    from aiquant.engine.execution import CostModel
    n = 8
    o = [100.0] * n; lo = [99.0] * n; hi = [101.0] * n; c = [100.0] * n
    o[4], lo[4], hi[4], c[4] = 90.0, 88.0, 91.0, 90.0          # 第 5 天跳空低开到 90（止损价 95）
    df = _daily(o, lo, hi, c)
    cfg = bt.BTConfig(start=df["ts"].iloc[0].to_pydatetime(), end=(df["ts"].iloc[-1] + pd.Timedelta(days=1)).to_pydatetime(),
                      initial_cash=10_000, cost=CostModel(0, 0, 0), market=mk.US_EQUITY)
    res = bt.run(df, _always_long(stop=5.0), 1440, None, cfg)
    t = [x for x in res["trades"] if x["exit_reason"] == "止损"][0]
    assert t["exit_price"] == pytest.approx(90.0) and t["exit_price"] < t["entry_price"] * 0.95
    # 没有跳空：最低价穿过止损价 → 按止损价 95
    lo2 = list(lo); o2 = list(o); lo2[4], o2[4] = 94.0, 99.0
    res2 = bt.run(_daily(o2, lo2, hi, c), _always_long(stop=5.0), 1440, None, cfg)
    t2 = [x for x in res2["trades"] if x["exit_reason"] == "止损"][0]
    assert t2["exit_price"] == pytest.approx(95.0)


def test_backtest_take_profit_intraday_at_trigger_gap_up_at_open():
    """回测止盈与执行一致：盘中触及按止盈价成交；开盘已跳空高于止盈价 → 按开盘价成交。"""
    from aiquant import market as mk
    from aiquant.engine import backtest as bt
    from aiquant.engine.execution import CostModel
    n = 8
    o = [100.0] * n; lo = [99.0] * n; hi = [101.0] * n; c = [100.0] * n
    o[4], lo[4], hi[4], c[4] = 112.0, 111.0, 113.0, 112.0       # 第 5 天跳空高开到 112（止盈价 105）
    df = _daily(o, lo, hi, c)
    cfg = bt.BTConfig(start=df["ts"].iloc[0].to_pydatetime(), end=(df["ts"].iloc[-1] + pd.Timedelta(days=1)).to_pydatetime(),
                      initial_cash=10_000, cost=CostModel(0, 0, 0), market=mk.US_EQUITY)
    t = [x for x in bt.run(df, _always_long(tp=5.0), 1440, None, cfg)["trades"] if x["exit_reason"] == "止盈"][0]
    assert t["exit_price"] == pytest.approx(112.0)
    o2, hi2, c2 = list(o), list(hi), list(c)
    o2[4], hi2[4], c2[4] = 101.0, 106.0, 104.0                    # 盘中涨到 106 → 按 105
    t2 = [x for x in bt.run(_daily(o2, lo, hi2, c2), _always_long(tp=5.0), 1440, None, cfg)["trades"]
          if x["exit_reason"] == "止盈"][0]
    assert t2["exit_price"] == pytest.approx(105.0)
    assert any("跳空高开越过止盈价时按开盘价成交" in a for a in bt.ASSUMPTIONS_DAILY)


def test_backtest_position_cap_trims_and_noop_when_not_binding():
    """回测的持仓上限：占本策略资金 50%（按账户口径换算后 < 100% 时才有效）→ 开仓不超过上限，涨过上限下一根开盘减回。"""
    from aiquant import allocation, market as mk
    from aiquant.engine import backtest as bt
    from aiquant.engine import regime_bt
    from aiquant.engine.execution import CostModel
    n = 10
    o = [100.0 * (1.1 ** i) for i in range(n)]
    df = _daily(o, [x * 0.99 for x in o], [x * 1.01 for x in o], [x * 1.05 for x in o])
    base = dict(start=df["ts"].iloc[0].to_pydatetime(), end=(df["ts"].iloc[-1] + pd.Timedelta(days=1)).to_pydatetime(),
                initial_cash=10_000, cost=CostModel(0, 0, 0), market=mk.US_EQUITY, reserve_pct=0.0)
    free = bt.run(df, _always_long(), 1440, None, bt.BTConfig(**base))
    capped = bt.run(df, _always_long(), 1440, None, bt.BTConfig(**base, position_cap_frac=0.5))
    trims = [t for t in capped["trades"] if t["exit_reason"] == "仓位上限减仓"]
    assert trims and capped["metrics"]["final_equity"] < free["metrics"]["final_equity"]
    first = capped["trades"][0]
    assert first["entry_price"] * (first["qty"] + sum(t["qty"] for t in trims if t["entry_fill_ts"] == first["entry_fill_ts"])) \
        <= 10_000 * 0.5 * 1.0001 + 1e-6
    noop = bt.run(df, _always_long(), 1440, None, bt.BTConfig(**base, position_cap_frac=1.67))
    assert noop["trades"] == free["trades"]
    # 账户口径：上限 50% ÷ 额度 30% = 自身资金的 167% → 不会触发（None）；额度 80% 时 = 62.5%
    assert allocation.backtest_cap_frac(30) is None and allocation.backtest_cap_frac(80) == pytest.approx(0.625)
    # ETF 择时回测：仓位比例不超过上限
    idx = pd.bdate_range("2024-01-02", periods=300)
    d = pd.DataFrame({"sig_close": np.linspace(100, 200, 300), "open": np.linspace(50, 80, 300),
                      "close": np.linspace(50, 80, 300)}, index=idx)
    assert regime_bt.simulate(d, 20, 0.0, cap_frac=0.6)["size"].max() == pytest.approx(0.6)
    assert (regime_bt.simulate(d, 20, 0.0, cap_frac=None)["size"] == regime_bt.simulate(d, 20, 0.0)["size"]).all()
