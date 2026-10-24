"""策略组合：把全部策略（5 个研究策略位 + 3 个实时预设）同时部署到同一个 Alpaca Paper 账户。

每个策略有独立资金额度（budget_usd）和独立订单台账；券商实际持仓与全部策略台账合计对账。
仍然只做多、1 倍、无杠杆；交易决策只来自冻结的确定性规则。
"""
from __future__ import annotations

import json

from .. import db, jobs, research
from ..live import sim
from ..strategy import registry
from . import runner as pr
from .brokers import AlpacaPaperAdapter

PRESET_TF_MIN = 15  # 实时预设在模拟盘上使用 15 分钟K线（模拟盘行情基础粒度为 5 分钟）


def preset_version(key: str) -> dict:
    """把实时预设冻结为可部署的策略版本（每个预设一条独立谱系，内容相同不重复创建）。"""
    p = sim.PRESETS[key]
    sch = json.loads(json.dumps(p["schema"]))
    sch["name"] = f"实时-{p['label'].split('（')[0]}"
    sch["signal_tf"] = PRESET_TF_MIN
    sch["hypothesis"] = p["plain"]
    evolved = db.get_setting(f"preset_current::{sch['name']}")
    if evolved:   # 自动进化替换过的版本优先
        v = registry.get_version(int(evolved))
        if v:
            return v
    ex = db.q1("SELECT id FROM strategy_versions WHERE source='preset' AND name=? ORDER BY id DESC LIMIT 1", (sch["name"],))
    if ex:
        v = registry.get_version(ex["id"])
        if v["schema"]["signal_tf"] == PRESET_TF_MIN:
            return v
    return registry.freeze(None, sch, nl_text=p["plain"], source="preset", change_summary="由实时看盘预设冻结",
                           new_lineage=True, set_current=False)


def targets(include_offline: bool = False) -> list[dict]:
    """组合目标：每个策略位的实验室当前版本 + 3 个实时预设。在策略总台下线的策略默认不在其中。"""
    from .. import console
    out = []
    for slot in registry.all_slots():
        if slot["current_version_id"]:
            v = registry.get_version(slot["current_version_id"])
            key = f"slot:{slot['slot_no']}"
            if not include_offline and console.is_offline(key):
                continue
            out.append({"version": v, "key": key, "label": f"策略位{slot['slot_no']}·{v['name']} v{v['version_no']}"})
    for pk in sim.PRESETS:
        v = preset_version(pk)
        key = f"preset:{v['name']}"
        if not include_offline and console.is_offline(key):
            continue
        tf = int(v["schema"].get("signal_tf") or PRESET_TF_MIN)
        tf_cn = {15: "15分钟", 30: "30分钟", 60: "1小时", 240: "4小时", 1440: "日线"}.get(tf, f"{tf}分钟")
        out.append({"version": v, "key": key, "label": f"{v['name']}（{tf_cn}）" if v["version_no"] == 1
                    else f"{v['name']} v{v['version_no']}（{tf_cn}）"})
    return out


def submit_missing_backtests() -> list[str]:
    """为还没有真实数据回测的目标版本提交训练集回测（部署门禁要求）。"""
    msgs = []
    r = research.get_research()
    start, end = research.resolve_window(r)
    for t in targets():
        v = t["version"]
        if research.latest_backtest_for_version(v["id"]) is not None:
            continue
        tf = int(v["schema"].get("signal_tf") or r["timeframe_min"])
        aux = v["schema"].get("aux_tf")
        params = {"start": start.isoformat(), "end": end.isoformat(), "tf": tf, "aux": aux, "source": r["source"],
                  "research": r, "allow_test_touch": False}
        key = research.params_key(v["id"], tf, aux, start, end, r, r["source"], "backtest")
        jid, _ = jobs.submit("backtest", slot_no=None, version_id=v["id"], params=params, dedupe_key=key)
        msgs.append(f"{t['label']}：已提交训练集回测 #{jid}")
    return msgs


def deploy_all(reserve_pct: float = 4.0, *, actor: str | None = None, role: str | None = "admin") -> list[str]:
    """为全部目标策略创建部署并启动；已有部署的策略只更新资金额度。返回逐条结果。

    默认行为：实验室手动保存的新版本不会替换运行中的版本 —— 已在运行的策略保持原版本、原标签、原K线周期，
    结果里提示到「策略总台」确认部署。在总台下线的策略不会被重新部署。
    """
    from .. import console
    console.require_role(role)
    acct = AlpacaPaperAdapter().account()
    if acct is None:
        raise RuntimeError("无法读取 Alpaca Paper 账户")
    ts = targets()
    if not ts:
        return ["没有需要部署的策略（全部已在策略总台下线）"]
    budget = int(acct.equity * (1 - reserve_pct / 100) / len(ts) // 100 * 100)
    out = []
    # 先把现有运行中的部署更新为带额度的组合成员，避免额度校验冲突
    for t in ts:
        v = t["version"]
        # 同一策略（含自动进化产生的新版本、待切换版本）只保留一个部署
        ex = console.active_deployment(t["key"])
        if ex is None:
            ex = db.q1("SELECT d.* FROM paper_deployments d JOIN strategy_versions sv ON sv.id=d.version_id "
                       "WHERE d.broker='alpaca_paper' AND sv.lineage_id=? "
                       "AND d.status IN ('configured','running','paused_entries') ORDER BY d.id DESC LIMIT 1",
                       (v["lineage_id"],))
            if ex:
                ex = pr.get_deployment(ex["id"])
        if ex:
            cfg = ex["config"]
            cfg.update({"budget_usd": budget, "alloc_pct": 100, "portfolio": True, "strategy_key": t["key"]})
            cfg.setdefault("stop_mode", "keep")
            db.ex("UPDATE paper_deployments SET config_json=?, updated_at=? WHERE id=?",
                  (json.dumps(cfg, ensure_ascii=False), db.now_iso(), ex["id"]))
            did = ex["id"]
            pr.event(did, "INFO", f"加入策略组合，资金额度 {budget:,} USD")
            if ex["version_id"] != v["id"]:
                run = registry.get_version(ex["version_id"])
                out.append(f"ℹ {t['label']}：实验室有新版本，运行中的仍是 {registry.label(run)}，"
                           f"未自动替换（请在策略总台确认部署）")
            t["did"] = did
            continue
        ok, checks = pr.deploy_gate(v["id"])
        if not ok:
            out.append(f"✗ {t['label']}：门禁未通过（" + "；".join(f"{n}" for n, p, _ in checks if not p) + "）")
            continue
        b = (research.latest_backtest_for_version(v["id"], kind_split="训练集")
             or research.latest_backtest_for_version(v["id"]))
        cfg = {"label": t["label"], "budget_usd": budget, "alloc_pct": 100, "signal_tf": b["signal_tf"],
               "aux_tf": b["aux_tf"], "portfolio": True, "stop_mode": "keep", "strategy_key": t["key"]}
        t["did"] = pr.save_deployment(v["id"], cfg, "alpaca_paper")
        t["new"] = True
    for t in ts:
        if "did" not in t:
            continue
        d = pr.get_deployment(t["did"])
        if d["status"] in ("running", "paused_entries"):
            out.append(f"✓ {t['label']}：运行中（部署 #{t['did']}，额度 {budget:,} USD）")
            continue
        try:
            pr.start(t["did"])
            out.append(f"✓ {t['label']}：已启动（部署 #{t['did']}，额度 {budget:,} USD）")
            console.audit(actor, role, "一键部署", t["key"], t["version"]["name"], t["did"], None, d["version_id"],
                          "已启动")
        except Exception as e:  # noqa: BLE001
            out.append(f"✗ {t['label']}：{e}")
            console.audit(actor, role, "一键部署", t["key"], t["version"]["name"], t["did"], None, d["version_id"],
                          "启动失败", str(e))
    return out


def overview() -> list[dict]:
    """每个模拟盘部署的状态、持仓、盈亏与最近决策。"""
    rows = []
    for d in db.q("SELECT * FROM paper_deployments WHERE broker='alpaca_paper' ORDER BY id"):
        cfg = json.loads(d["config_json"])
        v = registry.get_version(d["version_id"])
        orders = db.q("SELECT side, filled_qty, filled_price, status, created_at FROM paper_orders "
                      "WHERE deployment_id=? ORDER BY id", (d["id"],))
        filled = [o for o in orders if o["status"] == "filled"]
        qty = sum(o["filled_qty"] if o["side"] == "buy" else -o["filled_qty"] for o in filled)
        cash_flow = sum((-1 if o["side"] == "buy" else 1) * o["filled_qty"] * (o["filled_price"] or 0) for o in filled)
        last = db.get_setting(f"paper_loop::{d['id']}") or {}
        rows.append({"id": d["id"], "label": cfg.get("label") or (f"{v['name']} v{v['version_no']}" if v else "--"),
                     "status": pr.STATE_CN.get(d["status"], d["status"]), "raw_status": d["status"],
                     "tf": cfg.get("signal_tf"), "budget": cfg.get("budget_usd"), "qty": max(qty, 0.0),
                     "cash_flow": cash_flow, "orders": len(orders), "filled": len(filled),
                     "last_at": last.get("at"), "last_action": last.get("action"), "last_reason": last.get("reason"),
                     "trailing_stop": last.get("trailing_stop")})
    return rows
