"""实验位：新挑战者自动开到小额模拟盘部署（不替换现役）、额度 2% 从现金层出、满额不再开、挑战者有结论后下线；
买卖点看板：真实成交 + 规则信号。"""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from aiquant import allocation, challenger, chartboard, console, db, evolve, experiments, research
from aiquant.data import http
from aiquant.data import market_data as md
from aiquant.paper import runner as pr
from aiquant.strategy import registry

BASE = {"indicators": [{"id": "f", "type": "EMA", "params": {"period": 9}},
                       {"id": "s", "type": "EMA", "params": {"period": 21}}],
        "entry": {"mode": "all", "conditions": [{"left": {"ref": "f"}, "op": "cross_above", "right": {"ref": "s"}}]},
        "exit": {"mode": "any", "conditions": [{"left": {"ref": "f"}, "op": "cross_below", "right": {"ref": "s"}}]}}


@pytest.fixture()
def ex_env(env, monkeypatch):
    md.install_synthetic_test_data(md.to_utc(env["val_start"]) - timedelta(days=440), md.to_utc(env["test_end"]))
    monkeypatch.setattr(http, "paper_orders_unlocked", lambda: True)
    import aiquant.paper.brokers as br
    monkeypatch.setattr(br, "AlpacaPaperAdapter", lambda: SimpleNamespace(account=lambda: SimpleNamespace(equity=100_000.0, cash=100_000.0)))
    challenger.ensure_tables()
    db.set_setting("experiment_config", None)
    before = {r["id"] for r in db.q("SELECT id FROM paper_deployments")}
    yield env
    for r in db.q("SELECT id FROM paper_deployments"):
        if r["id"] not in before:
            db.ex("UPDATE paper_deployments SET status='stopped' WHERE id=?", (r["id"],))
            db.ex("DELETE FROM paper_orders WHERE deployment_id=?", (r["id"],))   # 看板测试的成交会进入 BTC 对账
    left = db.q("SELECT id, config_json FROM paper_deployments WHERE status IN ('running','paused_entries','configured') "
                "AND broker='alpaca_paper'")
    assert not [r for r in left if r["id"] not in before], left
    db.ex("UPDATE evolve_challengers SET status='expired' WHERE status IN ('running','awaiting')")
    db.set_setting("experiment_config", None)
    db.set_setting("capital_layers", None)


def _parent(name: str) -> tuple[dict, dict]:
    v = registry.freeze(4, dict(BASE, name=name, signal_tf=60), source="manual", new_lineage=True)
    cfg = {"label": f"策略位4·{name}", "strategy_key": "slot:4", "signal_tf": 60, "budget_usd": 1000, "portfolio": True}
    did = db.ex("INSERT INTO paper_deployments(version_id,config_json,broker,status,created_at,updated_at,is_test) "
                "VALUES(?,?,?,?,?,?,0)", (v["id"], json.dumps(cfg, ensure_ascii=False), "alpaca_paper", "running",
                                          db.now_iso(), db.now_iso()))
    return db.q1("SELECT * FROM paper_deployments WHERE id=?", (did,)), v


def _challenger(dep: dict, champ: dict, period: int) -> int:
    cand = json.loads(json.dumps(champ["schema"]))
    cand["indicators"][1]["params"]["period"] = period
    return challenger.register_btc(dep, champ, cand, f"s.period 21→{period}", {"score": 1.0, "return_pct": 2.0}, False)


def test_new_challenger_opens_experiment_and_ends_with_it(ex_env):
    allocation.set_config(actor="t", role="admin", crypto_max=50.0)        # 现金层留出空间
    dep, champ = _parent("实验位测试A")
    cid = _challenger(dep, champ, 30)                                       # register_btc 会自动开实验位
    exp = experiments._for_challenger(cid)
    assert exp and exp["status"] == "running" and exp["broker"] == "alpaca_paper"
    assert exp["config"]["strategy_key"] == f"exp:{cid}" and exp["config"]["trial"]["pct"] == 2.0
    assert exp["config"]["budget_usd"] == 2000                              # 2% × 100,000
    v = registry.get_version(exp["version_id"])
    assert v["source"] == "experiment" and v["schema"]["indicators"][1]["params"]["period"] == 30
    assert pr.get_deployment(dep["id"])["version_id"] == champ["id"]        # 现役不变
    assert research.latest_backtest_for_version(v["id"], kind_split="训练集")  # 部署门禁：训练集回测已补
    assert db.q("SELECT * FROM strategy_audit WHERE action='开实验位' AND strategy_key=?", (f"exp:{cid}",))
    rows = console.experiment_rows()
    assert any(r["deployment"]["id"] == exp["id"] for r in rows)
    assert all(not r["key"].startswith("exp:") for r in console.rows())     # 不混进普通策略卡片
    # 实验位不参与进化
    deps = [d for d in db.q("SELECT * FROM paper_deployments WHERE status='running' AND is_test=0 AND broker<>'alpaca_us'")
            if not json.loads(d["config_json"]).get("experiment")]
    assert exp["id"] not in [d["id"] for d in deps]
    # 挑战者被淘汰 → 实验位下线（空仓立即停止），写审计
    db.ex("UPDATE evolve_challengers SET status='rejected', reason='测试淘汰' WHERE id=?", (cid,))
    out = experiments.sync()
    assert out and pr.get_deployment(exp["id"])["status"] == "stopped"
    assert pr.get_deployment(exp["id"])["config"]["experiment"]["ended"]["status"] == "rejected"
    assert db.q("SELECT * FROM strategy_audit WHERE action='下线' AND strategy_key=?", (f"exp:{cid}",))


def test_slots_are_capped_and_can_be_switched_off(ex_env):
    allocation.set_config(actor="t", role="admin", crypto_max=50.0)
    experiments.set_config(actor="t", role="admin", max_slots=1)
    d1, c1 = _parent("实验位测试B")
    d2, c2 = _parent("实验位测试C")
    a = _challenger(d1, c1, 31)
    b = _challenger(d2, c2, 33)
    assert experiments._for_challenger(a) and not experiments._for_challenger(b)   # 满了不再开
    assert "已满" in experiments.launch(b)["why"]
    experiments.set_config(actor="t", role="admin", enabled=False, max_slots=5)
    assert experiments.launch(b)["why"] == "实验位已关闭"
    with pytest.raises(console.ConsolePermissionError):
        experiments.set_config(actor="v", role="viewer", pct=3)
    with pytest.raises(ValueError):
        experiments.set_config(actor="t", role="admin", pct=50)


def test_experiment_is_not_graduated_from_trial(ex_env):
    allocation.set_config(actor="t", role="admin", crypto_max=50.0)
    dep, champ = _parent("实验位测试D")
    cid = _challenger(dep, champ, 34)
    exp = experiments._for_challenger(cid)
    cfg = exp["config"]
    cfg["trial"]["since"] = "2020-01-01T00:00:00+00:00"                    # 早就超过 60 天
    db.ex("UPDATE paper_deployments SET config_json=? WHERE id=?", (json.dumps(cfg, ensure_ascii=False), exp["id"]))
    allocation.graduate_trials()
    assert not pr.get_deployment(exp["id"])["config"]["trial"].get("graduated_at")


def test_chartboard_shows_fills_and_signals(ex_env):
    dep, champ = _parent("看板测试")
    df5 = md.load_master(md.TEST_VENUE) if hasattr(md, "TEST_VENUE") else None
    if df5 is None or df5.empty:
        df5 = md.load_dataset(md.get_dataset(md.to_utc(ex_env["val_start"]) - timedelta(days=60),
                                             md.to_utc(ex_env["val_start"]), source="test"))
    now = df5["ts"].max().to_pydatetime() + timedelta(minutes=5)
    t = (now - timedelta(days=2)).isoformat()
    db.ex("INSERT INTO paper_orders(deployment_id,client_order_id,broker,side,qty,status,filled_qty,filled_price,"
          "signal_ts,created_at,updated_at,is_test) VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
          (dep["id"], f"cb-{dep['id']}", "alpaca_paper", "buy", 0.01, "filled", 0.01, 50000.0, t, t, t))
    d = chartboard.board(60, 20, [dep["id"]], True, now=now, df5=df5)
    assert len(d["bars"]) > 100 and not d["errors"]
    f = d["fills"]
    assert len(f) == 1 and f.iloc[0]["side"] == "buy" and f.iloc[0]["label"] == "策略位4·看板测试"
    s = d["signals"]
    assert len(s) > 0 and set(s["side"]) <= {"buy", "sell"}
    assert (s["ts"] >= d["since"]).all()
    # 只标「刚成立」的K线：同一方向不会连续两根
    ss = chartboard.signals(dep["id"], df5, now)
    assert not any((ss["side"].shift() == ss["side"]) & ((ss["ts"] - ss["ts"].shift()) <= timedelta(hours=1)))


def test_pages_render_with_experiment(ex_env):
    from streamlit.testing.v1 import AppTest
    allocation.set_config(actor="t", role="admin", crypto_max=50.0)
    dep, champ = _parent("实验位页面")
    cid = _challenger(dep, champ, 35)
    assert experiments._for_challenger(cid)

    def console_app():
        from aiquant import auth
        from aiquant.ui import page_console
        auth.ensure()
        page_console.render()

    def chart_app():
        from aiquant import auth
        from aiquant.ui import page_chart
        auth.ensure()
        page_chart.render()

    at = AppTest.from_function(console_app, default_timeout=180)
    at.run()
    assert not at.exception
    assert any("实验位" in (m.value or "") for m in at.markdown)
    at2 = AppTest.from_function(chart_app, default_timeout=180)
    at2.run()
    assert not at2.exception
