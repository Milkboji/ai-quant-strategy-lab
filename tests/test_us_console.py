"""美股 ETF 接入策略总台：唯一部署入口、锁定、上下线（空仓才下线）、权限、为什么没有动作、每日摘要。"""
import json

import pytest

from aiquant import challenger, console, db, notify
from aiquant.paper import etf_trend, etf_versions


@pytest.fixture()
def etf(env):
    etf_versions.ensure_tables()
    etf_trend.ensure_tables()
    challenger.ensure_tables()
    saved = dict(db.get_setting("etf_trend_config") or {})
    db.set_setting("etf_version_lock", None)
    db.set_setting("promotion_config", {"require_confirm": False, "manual_lock_days": 7})
    db.set_setting("etf_trend_state", {})
    yield
    db.set_setting("etf_trend_config", saved)
    db.set_setting("etf_version_lock", None)
    db.set_setting("etf_trend_state", {})


def _audit(action: str) -> list[dict]:
    return [a for a in console.audit_log(50, key="etf") if a["action"] == action]


def test_single_activation_path_for_manual_approve_and_challenger(etf):
    cur = etf_versions.running()
    v = etf_versions.freeze(cur["sma"] + 20, 0.03, confirmed=True, actor="t1", role="trader")   # 交易员也可以
    res = etf_versions.deploy(v["id"], confirmed=True, actor="t1", role="trader")
    assert res["result"] == "已部署" and etf_trend.get_config()["sma"] == cur["sma"] + 20
    assert etf_versions.lock_state()["kind"] == "manual_deploy"            # 手动部署后自动锁定
    assert _audit("部署此版本")
    # 挑战者自动晋级同样走 activate：登记版本 + 审计（不自动锁定）
    db.set_setting("etf_version_lock", None)
    etf_versions.activate(cur["sma"] + 30, 0.01, trigger="challenger", note="测试")
    run = etf_versions.running()
    assert run["sma"] == cur["sma"] + 30 and run["source"] == "challenger" and run["deployed_at"]
    assert _audit("挑战者晋级") and etf_versions.lock_state() is None
    # 批准晋级：console.approve → activate
    c = etf_trend.get_config()
    cid = db.ex("INSERT INTO evolve_challengers(kind,label,champion_params,candidate_json,change,train_json,"
                "registered_at,status,decided_at,reason,is_test) VALUES('etf','ETF',?,?,?,?,?,?,?,?,1)",
                (json.dumps({"sma": int(c["sma"]), "band": float(c["band"])}),
                 json.dumps({"sma": int(c["sma"]) + 5, "band": float(c["band"])}), "均线+5", "{}",
                 db.now_iso(), "awaiting", db.now_iso(), "等待确认"))
    console.approve(cid, actor="boss", role="admin")
    assert etf_trend.get_config()["sma"] == int(c["sma"]) + 5 and _audit("批准晋级")


def test_viewer_cannot_change_etf(etf):
    v = etf_versions.running()
    for fn in (lambda: etf_versions.deploy(v["id"], confirmed=True, actor="g", role="viewer"),
               lambda: etf_versions.freeze(160, 0.02, confirmed=True, actor="g", role="viewer"),
               lambda: etf_versions.set_lock(True, actor="g", role="viewer"),
               lambda: etf_versions.set_online(False, actor="g", role="viewer")):
        with pytest.raises(PermissionError):
            fn()


def test_lock_toggle_routes_challenger_to_queue(etf):
    etf_versions.set_lock(True, actor="t1", role="trader")
    assert etf_versions.lock_state()["until"] is None
    assert "锁定" in challenger._etf_block()
    etf_versions.set_lock(False, actor="t1", role="trader")
    assert etf_versions.lock_state() is None and challenger._etf_block() is None


def test_offline_waits_for_flat_and_releases_budget(etf, monkeypatch):
    from aiquant import evolve
    db.set_setting("etf_trend_state", {"position": {"qty": 5.0, "market_value": 500}})
    res = etf_versions.set_online(False, actor="t1", role="trader")
    assert res["result"] == "等待空仓后下线" and etf_versions.status() == "等待空仓后下线"
    assert not etf_versions.finish_pending_offline(5.0)                   # 还有持仓：继续等
    assert etf_versions.finish_pending_offline(0.0) and etf_versions.status() == "已下线"
    db.set_setting("etf_trend_state", {"position": {"qty": 0.0, "market_value": 0.0}})
    plan = evolve.plan_budgets(100_000, [])
    assert plan == {}
    # 下线后 ETF 的资金比例不再从 BTC 额度中扣除
    cfgv = evolve.get_config()
    total_off = 100_000 * (1 - float(cfgv["reserve_pct"]) / 100)
    dep = {"id": 1, "status": "running", "config": {}, "version_id": None}
    monkeypatch.setattr(evolve, "_long_check", lambda d: None)
    monkeypatch.setattr(evolve, "_train_metrics", lambda d: {"return_pct": 10, "max_dd_pct": 5})
    assert abs(evolve.plan_budgets(100_000, [dep])[1]["budget"] - total_off) < 200
    etf_versions.set_online(True, actor="t1", role="trader")
    assert etf_versions.status() == "运行中"
    assert evolve.plan_budgets(100_000, [dep])[1]["budget"] < total_off - 20_000


def test_etf_row_and_explain(etf):
    cur = etf_versions.running()
    etf_versions.freeze(cur["sma"] + 40, 0.02, confirmed=True, actor="t1", role="trader")
    etf_trend.set_why("closed", "美股休市中，下次开盘 10-30 21:30（北京时间）")
    db.set_setting("etf_trend_state", {"signal": {"date": "2026-10-28", "close": 700.0, "sma": 650.0, "lower": 637.0,
                                                  "upper": 663.0, "regime": 1},
                                       "position": {"qty": 10.0, "market_value": 30000.0, "unrealized_pl": 120.0}})
    r = console.etf_row(100_000)
    assert r["key"] == "etf" and r["mismatch"] and r["diff"] and r["status"] in ("运行中", "暂停中")
    assert "休市" in r["why"]["headline"]
    assert any(t == "信号" for t, _ in r["why"]["items"])
    assert any("30%" in x or "资金比例" in t for t, x in r["why"]["calc"])


def test_daily_summary_includes_etf(etf):
    db.set_setting("etf_trend_state", {"signal": {"regime": 1}, "position": {"qty": 3.0, "market_value": 900.0,
                                                                            "unrealized_pl": -12.0}})
    etf_trend.set_why("hold", "看多且已持有 QLD，保持不动")
    db.ex("INSERT INTO etf_orders(ts,symbol,side,notional,status,client_order_id,signal_date) VALUES(?,?,?,?,?,?,?)",
          (db.now_iso(), "QLD", "buy", 900, "filled", f"sum-{db.now_iso()}", "2026-10-28"))
    _, body = notify.daily_summary_text()
    assert "美股 ETF" in body and "浮动盈亏 -12" in body and "保持不动" in body
    assert "其中美股 ETF 1 笔" in body and "含美股 ETF" in body


def test_console_page_shows_etf_card_read_only(etf):
    from streamlit.testing.v1 import AppTest

    def app():
        import streamlit as st

        from aiquant import auth
        from aiquant.ui import login, page_console
        auth.ensure()
        login._install_readonly_guard()
        st.session_state["auth_user"] = "guest_viewer"
        st.session_state["_aq_readonly"] = True
        page_console.render()

    at = AppTest.from_function(app, default_timeout=120)
    at.run()
    assert not at.exception
    assert any("美股 ETF 择时" in m.value for m in at.markdown)
    btns = [b for b in at.button if b.key in ("cs_dep_etf", "cs_off_etf", "cs_on_etf")]
    assert btns and all(b.disabled for b in btns)


def test_budget_pct_edit_and_actual_exposure(etf, monkeypatch):
    from aiquant import evolve
    with pytest.raises(PermissionError):
        etf_versions.set_budget_pct(20, actor="g", role="viewer")
    with pytest.raises(ValueError):
        etf_versions.set_budget_pct(90, actor="t1", role="trader")
    res = etf_versions.set_budget_pct(20, actor="t1", role="trader")
    assert res["result"] == "已修改" and etf_trend.get_config()["budget_pct"] == 20 and _audit("修改资金比例")
    assert etf_trend.capital_share(100_000) == 20
    db.set_setting("etf_trend_state", {"position": {"qty": 10, "market_value": 35_000}})    # 涨到 35%
    assert etf_trend.capital_share(100_000) == 35
    dep = {"id": 1, "status": "running", "config": {}, "version_id": None}
    monkeypatch.setattr(evolve, "_long_check", lambda d: None)
    monkeypatch.setattr(evolve, "_train_metrics", lambda d: {"return_pct": 10, "max_dd_pct": 5})
    cfgv = evolve.get_config()
    b = evolve.plan_budgets(100_000, [dep])[1]["budget"]
    assert abs(b - 100_000 * (1 - (float(cfgv["reserve_pct"]) + 35) / 100)) < 200   # 按实际占用扣除


def test_etf_holdout_uses_unified_split_and_counts(etf, monkeypatch):
    import numpy as np
    import pandas as pd

    from aiquant import holdout
    idx = pd.bdate_range("2016-01-04", "2026-10-28")
    rng = np.random.default_rng(5)
    px = 100 * np.cumprod(1 + rng.normal(0.0006, 0.012, len(idx)))
    df = pd.DataFrame({"sig_close": px, "open": px, "close": px}, index=idx)
    lin = challenger.etf_lineage()
    db.ex("DELETE FROM holdout_touches WHERE lineage_key=?", (lin,))
    db.set_setting("lineage_exposure", {})                                # 未暴露的谱系：正常使用新分区
    cur = {"sma": 150, "band": 0.02}
    for _ in range(holdout.LIMIT):
        ho = challenger.etf_holdout(df, cur, {"sma": 120, "band": 0.01})
        assert ho["kind"] == "验证集" and ho["start"] == "2025-07-31" and ho["end"] == "2026-06-22"
        assert ho["split_version"] == 1
        challenger._etf_touch(ho)
    assert holdout.used(lin) == holdout.LIMIT
    ho = challenger.etf_holdout(df, cur, {"sma": 120, "band": 0.01})
    assert ho["kind"] is None and "用完" in ho["reason"]                   # 次数用完 → 转为挑战者
    # 测试期结束后满 60 个交易日 → 前向新数据
    idx2 = pd.bdate_range("2016-01-04", "2027-02-26")
    df2 = pd.DataFrame({"sig_close": 100.0, "open": 100.0, "close": 100.0}, index=idx2)
    assert challenger.etf_holdout(df2, cur, cur)["kind"] == "前向新数据"
    # 已暴露的谱系：只用上线日之后的前向数据
    db.set_setting("lineage_exposure", None)
    assert holdout.exposure(lin)
    ho = challenger.etf_holdout(df, cur, {"sma": 120, "band": 0.01})
    assert ho["kind"] is None and ho["exposed"] and "已暴露" in ho["reason"]
    ho = challenger.etf_holdout(df2, cur, cur)
    assert ho["kind"] == "上线后前向数据" and ho["start"] > "2026-10-21"
    db.ex("DELETE FROM holdout_touches WHERE lineage_key=?", (lin,))


def test_merge_etf_counts_union(env):
    from aiquant import holdout
    holdout.ensure()
    db.set_setting("holdout_merge_report", None)
    lin = "etf:QQQ->QLD"
    db.ex("DELETE FROM holdout_touches WHERE lineage_key=? OR split='etf_validation'", (lin,))
    ins = ("INSERT INTO holdout_touches(version_id,split,window_start,window_end,backtest_id,touched_at,lineage_key) "
           "VALUES(?,?,?,?,?,?,?)")
    db.ex(ins, (1, "etf_validation", "2025-01-01", "2025-12-31", None, "2026-10-25T01:00:00", None))
    db.ex(ins, (1, "etf_validation", "2025-01-01", "2026-03-31", None, "2026-10-26T01:00:00", None))
    db.ex(ins, (1, "validation", "2025-01-01", "2025-12-31", None, "2026-10-25T01:00:30", lin))   # 同一次检验
    rep = holdout.merge_etf_counts(lin)
    assert rep["etf_validation_before"] == 2 and rep["validation_before"] == 1 and rep["after"] == 2
    assert not db.q("SELECT id FROM holdout_touches WHERE split='etf_validation'")
    assert holdout.merge_etf_counts(lin) == rep                              # 幂等
    db.ex("DELETE FROM holdout_touches WHERE lineage_key=?", (lin,))
    db.set_setting("holdout_merge_report", None)


def test_old_split_challenger_is_annotated(etf):
    from aiquant import holdout
    c = etf_trend.get_config()
    cid = db.ex("INSERT INTO evolve_challengers(kind,label,champion_params,candidate_json,change,train_json,registered_at,"
                "status) VALUES('etf','ETF',?,?,?,?,?, 'running')",
                (json.dumps({"sma": int(c["sma"]), "band": float(c["band"])}), json.dumps({"sma": 170, "band": 0.02}),
                 "均线 150→170 日", json.dumps({"calmar": 1.0}), "2026-10-21T13:22:43+00:00"))
    holdout.migrate()
    tj = json.loads(db.q1("SELECT train_json FROM evolve_challengers WHERE id=?", (cid,))["train_json"])
    assert tj["split_version"] == 0 and "旧分区" in tj["split_note"]
    assert db.q1("SELECT status FROM evolve_challengers WHERE id=?", (cid,))["status"] == "running"   # 不中断
    db.ex("UPDATE evolve_challengers SET status='expired' WHERE id=?", (cid,))


def test_etf_round_promotes_through_holdout_or_queues(etf, monkeypatch):
    monkeypatch.setattr(challenger, "etf_holdout", lambda df, cur, cand: {
        "kind": "前向新数据", "start": "2026-01-01", "end": "2026-10-01", "passed": True, "reason": "测试通过",
        "split_version": 1})
    import numpy as np
    import pandas as pd
    idx = pd.bdate_range("2016-01-04", "2025-12-31")
    # 构造一段让相邻参数明显更好的数据：均线越短越好
    px = 100 * np.cumprod(1 + 0.012 * np.sin(np.arange(len(idx)) / 40.0) + 0.0003)
    monkeypatch.setattr(challenger, "_etf_data", lambda: pd.DataFrame({"sig_close": px, "open": px, "close": px}, index=idx))
    db.ex("UPDATE evolve_challengers SET status='expired' WHERE kind='etf' AND status IN ('running','awaiting')")
    rec = challenger.etf_round()
    if rec["decision"] == "保留":
        pytest.skip("合成数据上相邻参数没有更好")
    assert rec["decision"] == "替换" and etf_trend.get_config()["sma"] == rec["best"]["sma"]
    assert _audit("挑战者晋级")
    db.set_setting("promotion_config", {"require_confirm": True, "manual_lock_days": 7})
    db.set_setting("etf_trend_config", dict(db.get_setting("etf_trend_config") or {}, sma=150, band=0.02))
    rec = challenger.etf_round()
    assert rec["decision"] == "待人工确认"
    assert any(a["kind"] == "etf" for a in console.awaiting())


def test_etf_forward_early_decision(etf, monkeypatch):
    import pandas as pd
    db.ex("UPDATE evolve_challengers SET status='expired' WHERE kind='etf' AND status IN ('running','awaiting')")
    c = etf_trend.get_config()
    cid = db.ex("INSERT INTO evolve_challengers(kind,label,champion_params,candidate_json,change,train_json,registered_at,"
                "status) VALUES('etf','ETF',?,?,?,?,?, 'running')",
                (json.dumps({"sma": int(c["sma"]), "band": float(c["band"])}), json.dumps({"sma": 100, "band": 0.0}),
                 "测试", json.dumps({"fast": {"verdict": "inconclusive"}}), "2026-08-01T00:00:00+00:00"))
    idx = pd.bdate_range("2016-01-04", "2026-10-28")
    monkeypatch.setattr(challenger, "_etf_data", lambda: pd.DataFrame({"sig_close": 1.0, "open": 1.0, "close": 1.0},
                                                                     index=idx))

    def fake_bt(df, sma, band, a, b):
        cand = sma == 100
        return {"ret": -5.0 if cand else 1.0, "dd": -6.0 if cand else -2.0, "days": 25, "switches": 1}

    monkeypatch.setattr(challenger, "etf_backtest", fake_bt)
    out = challenger.evaluate_etf()
    assert any("淘汰" in x for x in out)                                     # 25 个交易日就明显落后 → 提前淘汰
    assert db.q1("SELECT status FROM evolve_challengers WHERE id=?", (cid,))["status"] == "rejected"


def test_idle_etf_budget_lent_to_btc_and_reclaimed(etf, monkeypatch):
    """美股空仓时 BTC 最多临时使用一半额度；美股发出买入信号时收回：BTC 暂停新开仓，现金够了美股买入，额度归还；全程审计。"""
    import pandas as pd

    from aiquant import evolve
    from aiquant.paper import runner as pr
    from datetime import datetime, timezone
    from tests.us_fake import reset_us
    reset_us()
    db.set_setting("etf_lend", None)
    db.ex("DELETE FROM etf_orders")
    monkeypatch.setattr(etf_trend.http, "paper_orders_unlocked", lambda: True)
    monkeypatch.setattr(etf_trend, "_clock", lambda: {"is_open": True})
    monkeypatch.setattr(etf_trend, "update_daily_csv", lambda *a, **k: {})
    monkeypatch.setattr(etf_trend, "_position", lambda sym: {"qty": 0.0, "market_value": 0, "avg_entry_price": 0,
                                                            "unrealized_pl": 0})
    days = pd.bdate_range("2026-01-01", periods=200).date
    down = pd.DataFrame({"date": days, "close": [200 - i * 0.2 for i in range(200)]})
    up = pd.DataFrame({"date": days, "close": [100 + i * 0.2 for i in range(200)]})
    sent = []
    monkeypatch.setattr(etf_trend, "_submit", lambda sym, side, **kw: sent.append(kw) or {})
    # 1) 看空空仓 → 借出 50%：ETF 只占 15%
    monkeypatch.setattr(etf_trend, "daily_bars", lambda sym, days_=420: down)
    monkeypatch.setattr(etf_trend, "_account", lambda: {"equity": "100000", "cash": "100000"})
    etf_trend.step(datetime(2026, 10, 20, 14, 0, tzinfo=timezone.utc))
    assert etf_trend.lend_state()["state"] == "lent" and etf_trend.capital_share(100_000) == 15
    assert _audit("临时借出额度")
    # 2) 看多（开盘时才确认，收盘后检查没跑）→ 收回：BTC 按借用比例减仓、暂停新开仓；ETF 先等还款、当天不记已动作
    monkeypatch.setattr(etf_trend, "daily_bars", lambda sym, days_=420: up)
    monkeypatch.setattr(etf_trend, "_account", lambda: {"equity": "100000", "cash": "20000"})
    budgets = iter([{"999": 40_000.0}, {"999": 30_000.0}])
    monkeypatch.setattr(etf_trend, "_btc_budgets", lambda: next(budgets))
    st = etf_trend.step(datetime(2026, 10, 21, 14, 0, tzinfo=timezone.utc))
    assert etf_trend.pending_cut(999)["frac"] == 0.25
    assert etf_trend.reclaim_active() and not sent and st.get("last_action_date") != "2026-10-21"
    assert etf_trend.get_why()["code"] == "reclaim" and _audit("收回临时借出的额度")
    assert etf_trend.capital_share(100_000) == 30
    expl = console.explain({"id": 1, "status": "running", "config": {}, "broker": "alpaca_paper", "is_test": 0,
                            "version_id": None})
    assert "收回" in expl["headline"]
    # 3) BTC 按比例卖出后现金够了 → 同一天买入，额度归还
    etf_trend.mark_cut_done(999, 0.1, "测试：已卖出")
    assert etf_trend.reclaim_pending() == []
    assert any(a["action"] == "BTC 归还借用额度" for a in console.audit_log(50))
    monkeypatch.setattr(etf_trend, "_account", lambda: {"equity": "100000", "cash": "60000"})
    etf_trend.step(datetime(2026, 10, 21, 15, 0, tzinfo=timezone.utc))
    assert sent and abs(sent[0]["notional"] - 30000) < 1
    assert etf_trend.lend_state()["state"] == "none" and _audit("额度已归还")
    # 借出比例可配置，0 = 不借；观察员不能改
    with pytest.raises(PermissionError):
        etf_versions.set_lend_pct(30, actor="g", role="viewer")
    etf_versions.set_lend_pct(0, actor="t1", role="trader")
    assert _audit("修改借出比例")
    db.set_setting("etf_lend", None)
