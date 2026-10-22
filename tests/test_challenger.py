"""冠军–挑战者：登记、前向评估、晋级自动部署（BTC 与美股 ETF）。"""
import json
from datetime import timedelta

import numpy as np
import pandas as pd

from aiquant import challenger, config, db, evolve
from aiquant.data import market_data as md
from aiquant.paper import runner as pr
from aiquant.strategy import registry
from tests.test_evolve import evo_env  # noqa: F401  共享夹具

SCH = {"name": "挑战者测试", "indicators": [
    {"id": "f", "type": "EMA", "params": {"period": 9}}, {"id": "s", "type": "EMA", "params": {"period": 21}}],
    "entry": {"mode": "all", "conditions": [{"left": {"ref": "f"}, "op": "cross_above", "right": {"ref": "s"}}]},
    "exit": {"mode": "any", "conditions": [{"left": {"ref": "f"}, "op": "cross_below", "right": {"ref": "s"}}]},
    "signal_tf": 60}


def _deploy():
    v = registry.freeze(None, SCH, source="test", new_lineage=True, set_current=False)
    did = pr.save_deployment(v["id"], {"label": "测试·挑战者", "signal_tf": 60, "alloc_pct": 100}, "mock")
    db.ex("UPDATE paper_deployments SET status='running', is_test=1 WHERE id=?", (did,))
    return did, v


def test_btc_challenger_forward_promotion(evo_env):
    did, v = _deploy()
    dep = db.q1("SELECT * FROM paper_deployments WHERE id=?", (did,))
    cand = json.loads(json.dumps(SCH))
    cand["take_profit_pct"] = 400.0            # 几乎不会触发：前向表现与现役相同 → 应晋级
    cid = challenger.register_btc(dep, v, cand, "增加 400% 止盈（测试）", {"score": 1.0, "return_pct": 5.0}, True)
    assert challenger.running("btc")
    end = md.to_utc(evo_env["test_end"])
    db.ex("UPDATE evolve_challengers SET registered_at=? WHERE id=?", ((end - timedelta(days=40)).isoformat(), cid))
    out = challenger.evaluate_btc(now=end - timedelta(days=1))
    row = db.q1("SELECT * FROM evolve_challengers WHERE id=?", (cid,))
    ev = json.loads(row["last_eval_json"])
    assert ev["days"] >= 14 and "challenger" in ev
    assert row["status"] == "promoted", (row["reason"], ev)
    d = pr.get_deployment(did)
    assert d["version_id"] == row["new_version_id"]          # 空仓 → 已自动部署到模拟盘
    assert registry.get_version(d["version_id"])["source"] == "auto_evolve"
    assert any("晋级" in x for x in out)


def test_btc_challenger_replaced_only_by_better(evo_env):
    did, v = _deploy()
    dep = db.q1("SELECT * FROM paper_deployments WHERE id=?", (did,))
    a = dict(SCH, stop_loss_pct=3.0)
    b = dict(SCH, stop_loss_pct=5.0)
    c1 = challenger.register_btc(dep, v, a, "A", {"score": 2.0}, True)
    c2 = challenger.register_btc(dep, v, b, "B", {"score": 1.0}, True)      # 训练分更低：不替换
    assert c1 == c2
    c3 = challenger.register_btc(dep, v, b, "B", {"score": 3.0}, True)      # 更高：替换
    assert c3 != c1
    assert db.q1("SELECT status FROM evolve_challengers WHERE id=?", (c1,))["status"] == "replaced"


def _write_etf_csv(n=2600, seed=3):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2016-01-04", periods=n, tz="America/New_York")
    r = rng.normal(0.0005, 0.012, n)
    qqq = 100 * np.cumprod(1 + r)
    qld = 50 * np.cumprod(1 + 2 * r)
    out = config.DATA_DIR / "etf_daily"
    out.mkdir(parents=True, exist_ok=True)
    for sym, px in (("QQQ", qqq), ("QLD", qld)):
        pd.DataFrame({"date": dates.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ"), "open": px, "high": px, "low": px,
                      "close": px, "volume": 1}).to_csv(out / f"{sym}.csv", index=False)
    return dates


def test_etf_challenger_round_and_promotion(evo_env):
    dates = _write_etf_csv()
    db.set_setting("etf_trend_config", {"sma": 150, "band": 0.02})
    df = challenger._etf_data()
    m = challenger.etf_backtest(df, 150, 0.02, "2016-01-01", "2024-12-31")
    assert m["days"] > 1000 and "calmar" in m
    rec = challenger.etf_round()
    assert rec["decision"] in ("保留", "挑战者上线")
    db.ex("UPDATE evolve_challengers SET status='expired' WHERE kind='etf'")
    cid = db.ex("INSERT INTO evolve_challengers(kind,label,champion_params,candidate_json,change,registered_at,status) "
                "VALUES('etf','美股 ETF 择时',?,?,?,?, 'running')",
                (json.dumps({"sma": 150, "band": 0.02}), json.dumps({"sma": 150, "band": 0.02}), "相同参数（测试）",
                 dates[-100].tz_convert("UTC").isoformat()))
    challenger.evaluate_etf()
    row = db.q1("SELECT * FROM evolve_challengers WHERE id=?", (cid,))
    assert row["status"] == "promoted"                                   # 表现相同 → 晋级
    assert db.get_setting("etf_trend_config")["sma"] == 150


def test_fast_verdict_rules():
    def rows(c, p, n=3, t=2):
        return [{"cand": c, "champ": p, "cand_dd": -5, "champ_dd": -5, "cand_trades": t, "champ_trades": t} for _ in range(n)]
    assert challenger._fast_verdict({"btc": rows(5, 2), "eth": rows(3, 2)})["verdict"] == "pass"
    assert challenger._fast_verdict({"btc": rows(-5, 2), "eth": rows(3, 2)})["verdict"] == "fail"
    assert challenger._fast_verdict({"btc": rows(5, 2), "eth": rows(-8, 2)})["verdict"] == "inconclusive"
    assert challenger._fast_verdict({"btc": rows(5, 2, n=1), "eth": []})["verdict"] == "inconclusive"   # 历史不足


def test_fast_check_btc_uses_unseen_history(evo_env, tmp_path):
    from aiquant.data import market_data as md2
    sp = evo_env
    m = md2.load_master("TEST_SYNTHETIC")
    hist = config.DATA_DIR / "history"
    hist.mkdir(parents=True, exist_ok=True)
    m.to_csv(hist / "BTCUSD_5m.csv.gz", index=False, compression="gzip")
    m.to_csv(hist / "ETHUSD_5m.csv.gz", index=False, compression="gzip")
    r = dict(__import__("aiquant").research.get_research(), source="test")
    res = challenger.fast_check_btc(SCH, dict(SCH, stop_loss_pct=3.0), r)
    assert res["verdict"] in ("pass", "fail", "inconclusive")
    for f in res["btc"]:
        assert f["end"] <= md.to_utc(sp["val_start"]).strftime("%Y-%m-%d")   # 只用训练窗口之前的历史
