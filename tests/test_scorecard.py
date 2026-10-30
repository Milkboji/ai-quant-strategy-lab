"""策略成绩单：重新回测实际运行区间、逐笔配对、按原因归类（中断导致不算策略偏差）、成交价 / 成本 / 收益偏差、
阈值通知（每个偏差只通知一次）、样本不足提示、卡片摘要。"""
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from aiquant import db, notify, scorecard, uptime
from aiquant.data import market_data as md
from aiquant.strategy import registry

START = datetime(2026, 9, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)
SCHEMA = {"name": "sc-test", "signal_tf": 60,
          "indicators": [{"id": "ef", "type": "EMA", "params": {"period": 3}},
                         {"id": "es", "type": "EMA", "params": {"period": 8}}],
          "entry": {"mode": "all", "conditions": [{"left": {"ref": "ef"}, "op": ">", "right": {"ref": "es"}}]},
          "exit": {"mode": "any", "conditions": [{"left": {"ref": "ef"}, "op": "<", "right": {"ref": "es"}}]}}


def _df5():
    idx = pd.date_range(START - timedelta(days=30), NOW, freq="5min", inclusive="left", tz="UTC")
    t = np.arange(len(idx))
    p = 60000 * (1 + 0.03 * np.sin(t / 400.0)) * np.exp(np.random.default_rng(3).normal(0, 0.0004, len(idx)).cumsum())
    return pd.DataFrame({"ts": idx, "open": p, "high": p * 1.0008, "low": p * 0.9992, "close": p, "volume": 1.0,
                         "trade_count": 1, "vwap": p})


@pytest.fixture()
def sc(env, monkeypatch):
    df5 = _df5()
    monkeypatch.setattr(md, "load_master", lambda *a, **k: df5.copy())
    v = registry.freeze(None, SCHEMA, source="test")
    cfg = {"label": "成绩单测试", "signal_tf": 60, "budget_usd": 10000, "live_since": (START + timedelta(days=1)).isoformat()}
    did = db.ex("INSERT INTO paper_deployments(version_id,config_json,broker,status,created_at,updated_at,is_test) "
                "VALUES(?,?,?,?,?,?,0)", (v["id"], json.dumps(cfg, ensure_ascii=False), "alpaca_paper", "running",
                                          START.isoformat(), START.isoformat()))
    uptime.ensure_tables()
    scorecard.ensure_tables()
    sent = []
    monkeypatch.setattr(notify, "event", lambda kind, title, body="", dedupe=None: sent.append((kind, title, body)) or 1)
    yield did, df5, sent
    db.ex("DELETE FROM paper_deployments WHERE id=?", (did,))
    db.ex("DELETE FROM paper_orders WHERE deployment_id=?", (did,))
    db.ex("DELETE FROM service_outages")
    db.ex("DELETE FROM scorecards WHERE deployment_id=?", (did,))


def _order(did, f, *, price=None, delay_s=20, qty=0.1, key=None, req=None):
    ts = f["ts"] + pd.Timedelta(seconds=delay_s)
    db.ex("INSERT INTO paper_orders(deployment_id,client_order_id,broker,side,qty,status,filled_qty,filled_price,signal_ts,"
          "created_at,updated_at,is_test) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
          (did, f"sc-{did}-{f['side']}-{ts.value}", "alpaca_paper", f["side"], req or qty, "filled", qty,
           price if price is not None else f["price"], (key or f["key"]).isoformat(), ts.isoformat(), ts.isoformat()))


def test_scorecard_matches_classifies_and_notifies_once(sc):
    did, df5, sent = sc
    from aiquant.paper import runner as pr
    dep = pr.get_deployment(did)
    start = scorecard.segment_start(dep)
    bt = scorecard._rebacktest(dep, start, pd.Timestamp(NOW))
    fills = sorted(bt["fills"], key=lambda x: x["key"])
    assert len(fills) >= 10 and fills[0]["side"] == "buy"
    # 前 4 笔：照回测执行（买入按 25 bps 手续费从币里扣）；第 3 笔（买入）成交价比回测高 50 bps = 差 50 bps
    for i, f in enumerate(fills[:4]):
        _order(did, f, price=f["price"] * (1 + 0.005) if i == 2 else None,
               qty=0.09975 if f["side"] == "buy" else 0.09975, req=0.1 if f["side"] == "buy" else None)
    # 第 5、6 笔（一买一卖）：入场信号时服务中断 → 中断导致，对应的离场同样归为中断导致
    b5 = fills[4]
    assert b5["side"] == "buy" and fills[5]["side"] == "sell"
    uptime.record(b5["key"].to_pydatetime() - timedelta(minutes=30), b5["key"].to_pydatetime() + timedelta(minutes=40),
                  "test")
    # 第 7 笔（买入）：没有中断也没有原因 → 漏单（策略偏差）；之后的不下单
    # 多一笔回测没有的买入 → 多单（策略偏差）
    extra_t = fills[6]["key"] - pd.Timedelta(days=2)
    db.ex("INSERT INTO paper_orders(deployment_id,client_order_id,broker,side,qty,status,filled_qty,filled_price,signal_ts,"
          "created_at,updated_at,is_test) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
          (did, f"sc-{did}-extra", "alpaca_paper", "buy", 0.1, "filled", 0.1, 60000.0, extra_t.isoformat(),
           extra_t.isoformat(), extra_t.isoformat()))
    out = scorecard.build(dep, NOW)
    st = {r["status"] for r in out["rows"]}
    assert {"一致", "漏单", "多单"} <= st
    cats = out["categories"]
    assert cats.get("中断导致") == 2                       # 入场 + 对应离场
    assert cats.get("成交价偏差") == 1
    assert cats.get("策略偏差", 0) >= 2                    # 第 7 笔漏单 + 多出来的买入
    assert "中断导致" not in out["strategy_categories"]
    pairs = [r for r in out["rows"] if r["status"] == "一致"]
    assert all(abs(r["dt_min"]) < 1 for r in pairs)
    dev = [r for r in out["rows"] if r.get("category") == "成交价偏差"][0]
    assert dev["price_dev_bps"] == pytest.approx(50, abs=0.5)
    # 买入手续费从币里扣：0.1 → 0.09975 = 25 bps，非估计；卖出按费率估计
    b0 = [r for r in pairs if r["side"] == "buy"][0]
    assert b0["fee_estimated"] is False and b0["cost_act_bps"] == pytest.approx(b0["cost_model_bps"], abs=0.2)
    assert b0["cost_model_bps"] == pytest.approx(25 + 0.5 + 2, abs=0.01)     # 手续费 + 半个点差 + 滑点
    assert any(r["fee_estimated"] for r in pairs if r["side"] == "sell")
    kinds = {a["kind"] for a in out["alerts"]}
    assert "成交价偏差" in kinds and "漏单·策略偏差" in kinds and "多单·策略偏差" in kinds
    assert not any("中断导致" in k for k in kinds)
    # 曲线：每天一个点，三条线，最后一个点 = NOW
    assert out["curve"][-1][0].startswith("2026-10-12") and len(out["curve"][0]) == 4
    assert out["forward_days"] == 40 and not out["insufficient"]
    assert out["verdict"].startswith("有偏差")
    # 保存 + 通知（每个偏差只通知一次）
    scorecard.save(out, NOW)
    sent.clear()                    # 只看成绩单的通知（中断记录本身也会发一条）
    assert scorecard._notify(out) >= 3 and len(sent) == 1 and sent[0][0] == "scorecard"
    assert "中断导致" not in sent[0][2]
    assert scorecard._notify(out) == 0 and len(sent) == 1
    cs = scorecard.card_summary(did)
    assert cs["tone"] == "orange" and "偏差" in cs["text"] and "中断导致 2" in cs["detail"]


def test_scorecard_consistent_and_curves_track_backtest(sc):
    """完全照回测执行（同价、同时）：一致；实际与回测的累计收益只差手续费口径。"""
    did, df5, sent = sc
    from aiquant.paper import runner as pr
    dep = pr.get_deployment(did)
    bt = scorecard._rebacktest(dep, scorecard.segment_start(dep), pd.Timestamp(NOW))
    for f in bt["fills"]:          # 买入手续费从币里扣（请求 0.1，到手 0.09975），和 Alpaca 加密货币一样
        _order(did, f, qty=0.09975, req=0.1 if f["side"] == "buy" else None)
    out = scorecard.build(dep, NOW)
    assert out["missed"] == 0 and out["extra"] == 0 and out["consistent"] == len(bt["fills"])
    assert abs(out["return_actual_pct"] - out["return_expected_pct"]) < 0.3
    assert out["verdict"] in ("与回测一致", "有偏差：成本偏差", "有偏差：收益偏差")
    assert out["return_bench_pct"] != 0


def test_scorecard_insufficient_sample_wording(sc):
    did, df5, sent = sc
    from aiquant.paper import runner as pr
    db.ex("UPDATE paper_deployments SET config_json=json_set(config_json,'$.live_since',?) WHERE id=?",
          ((NOW - timedelta(days=5)).isoformat(), did))
    out = scorecard.build(pr.get_deployment(did), NOW)
    assert out["insufficient"] and out["verdict"] == "样本不足，暂不能下结论"
    scorecard.save(out, NOW)
    assert scorecard.card_summary(did)["text"].startswith("成绩单：样本不足")
    # 样本不足时不发成本 / 收益类通知
    out["alerts"] = [{"kind": "收益偏差", "key": "ret", "text": "x"}]
    assert scorecard._notify(out) == 0


def test_match_tolerance_and_nearest():
    t0 = pd.Timestamp("2026-10-01T00:00:00Z")
    bt = [{"side": "buy", "key": t0, "ts": t0, "price": 1.0}, {"side": "sell", "key": t0 + pd.Timedelta(hours=5), "ts": t0, "price": 1.0}]
    paper = [{"side": "buy", "key": t0 + pd.Timedelta(hours=1), "ts": t0, "price": 1.0, "purpose": ""},
             {"side": "sell", "key": t0 + pd.Timedelta(hours=9), "ts": t0, "price": 1.0, "purpose": ""},
             {"side": "buy", "key": t0 + pd.Timedelta(hours=2), "ts": t0, "price": 1.0, "purpose": "add"}]
    pairs, missed, extra = scorecard.match(bt, paper, pd.Timedelta(hours=2))
    assert len(pairs) == 1 and pairs[0][1]["key"] == t0 + pd.Timedelta(hours=1)
    assert [m["side"] for m in missed] == ["sell"]
    assert len(extra) == 2 and scorecard.classify_extra({}, extra[-1], "buy") in ("资金调整", "策略偏差")
    assert scorecard.classify_extra({}, paper[2], "buy") == "资金调整"


def test_spread_report_runs_old_and_new_basis_for_deployed_versions(sc, monkeypatch):
    """点差对比报告：对已部署的版本在训练期分别用旧 / 新口径回测，写出差异。"""
    from aiquant import auth, research, spread_model
    did, df5, sent = sc
    auth.ensure()
    spread_model.ensure_tables()
    monkeypatch.setattr(spread_model, "symbols", lambda: ["BTC/USD"])
    monkeypatch.setattr(spread_model, "_deployed", lambda: db.q("SELECT * FROM paper_deployments WHERE id=?", (did,)))
    monkeypatch.setattr(spread_model, "_train_window", lambda m: (START, NOW))
    for d in range(12):
        for h in range(24):
            db.ex("INSERT OR REPLACE INTO spread_samples(symbol,day,bucket,n,median_bps,p90_bps,status,attempts,fetched_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?)", ("BTC/USD", f"2026-10-{d + 1:02d}", f"{h:02d}", 100, 4.0, 8.0, "ok", 1,
                                                 db.now_iso()))
    rep = spread_model.build_report("boss")
    row = [x for x in rep["rows"] if x["deployment"] == did][0]
    assert not row.get("skipped"), row
    assert row["new"]["spread"] > row["old"]["spread"] and row["diff_pp"] < 0
    assert row["old"]["trades"] == row["new"]["trades"]
    assert f"#{did} 成绩单测试" in rep["markdown"]
    assert spread_model.current_name() == "fixed_v1"
    db.ex("DELETE FROM spread_samples")
