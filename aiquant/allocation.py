"""资金分两层（2026-10-29 用户要求）。

第一层：市场之间（加密货币 / 美股 / 现金）
- 美股目标比例（默认取美股 ETF 原来的资金比例 30%），限制在 [美股下限, 美股上限] 内；没有运行中的美股部署时为 0。
- 与借出机制兼容：美股全部空仓且看空时，美股目标的 lend_pct% 临时借给加密货币（见 etf_trend 的 lend_state）。
- 美股实际占用 = max(美股目标 − 借出部分, 美股持仓市值占比)：美股涨多了也不会让两边合计超过账户。
- 加密货币 = 100% − 现金保留 − 美股实际占用，再限制在 [加密下限, 加密上限]；剩下的是现金（不少于现金保留）。

第二层：市场内部按现有实力公式（同一个公式，见 evolve.plan_budgets）
- 加密货币：evolve.plan_budgets（长周期 年均收益/年均回撤，或训练集 收益/回撤；权重上下限）。
- 美股：同一公式。ETF 择时用美股长周期体检（2017 年起逐年）的 年均收益/年均回撤；实验室版本用训练集回测。

再平衡：美股策略的实际仓位偏离目标超过 rebalance_pp 个百分点（占账户权益，默认 5，可配置）时，
收盘后记下，下一个交易日开盘后再平衡（us_exec）。加密货币按信号逐笔开仓，额度变化在下一次开仓时生效，不主动调仓。
"""
from __future__ import annotations

import json
import logging

from . import db

log = logging.getLogger("aiquant.alloc")
DEFAULTS = {"us_equity": {"target": None, "min": 0.0, "max": 60.0}, "crypto": {"min": 0.0, "max": 100.0},
            "rebalance_pp": 5.0}
ACTIVE = ("running", "paused_entries")


def get_config() -> dict:
    c = json.loads(json.dumps(DEFAULTS))
    s = db.get_setting("capital_layers") or {}
    for k in ("us_equity", "crypto"):
        c[k].update(s.get(k) or {})
    if s.get("rebalance_pp") is not None:
        c["rebalance_pp"] = float(s["rebalance_pp"])
    if c["us_equity"]["target"] is None:
        from .paper import etf_trend
        c["us_equity"]["target"] = float(etf_trend.get_config().get("budget_pct", 30.0))
    return c


def set_config(*, actor: str | None, role: str | None, us_target=None, us_min=None, us_max=None, crypto_min=None,
               crypto_max=None, rebalance_pp=None) -> dict:
    """修改第一层上下限 / 美股目标 / 再平衡阈值（管理员、交易员），写审计并立即重新分配。"""
    from . import console
    console.require_role(role)
    old = get_config()
    s = db.get_setting("capital_layers") or {}
    us, cr = dict(s.get("us_equity") or {}), dict(s.get("crypto") or {})

    def num(v, lo, hi, name):
        try:
            x = round(float(v), 2)
        except (TypeError, ValueError):
            raise ValueError(f"{name}必须是数字")
        if not lo <= x <= hi:
            raise ValueError(f"{name}需在 {lo:g}~{hi:g} 之间")
        return x
    if us_target is not None:
        us["target"] = num(us_target, 0, 100, "美股目标比例")
    if us_min is not None:
        us["min"] = num(us_min, 0, 100, "美股下限")
    if us_max is not None:
        us["max"] = num(us_max, 0, 100, "美股上限")
    if crypto_min is not None:
        cr["min"] = num(crypto_min, 0, 100, "加密货币下限")
    if crypto_max is not None:
        cr["max"] = num(crypto_max, 0, 100, "加密货币上限")
    new = dict(s, us_equity=us, crypto=cr)
    if rebalance_pp is not None:
        new["rebalance_pp"] = num(rebalance_pp, 1, 50, "再平衡阈值")
    m_us = dict(old["us_equity"], **us)
    m_cr = dict(old["crypto"], **cr)
    if m_us["min"] > m_us["max"] or m_cr["min"] > m_cr["max"]:
        raise ValueError("下限不能大于上限")
    db.set_setting("capital_layers", new)
    if "target" in us:   # 美股 ETF 原有的资金比例与美股目标保持一致（旧界面、旧说明读它）
        cfg = dict(db.get_setting("etf_trend_config") or {})
        cfg["budget_pct"] = us["target"]
        db.set_setting("etf_trend_config", cfg)
    cur = get_config()
    txt = (f"美股 目标 {cur['us_equity']['target']:g}%（{cur['us_equity']['min']:g}~{cur['us_equity']['max']:g}%），"
           f"加密货币 {cur['crypto']['min']:g}~{cur['crypto']['max']:g}%，再平衡阈值 {cur['rebalance_pp']:g} 个百分点")
    console.audit(actor, role, "修改资金分层", "capital", "资金两层分配", None, None, None, txt, "")
    try:
        from . import evolve
        evolve.rebalance()
    except Exception:  # noqa: BLE001
        log.warning("rebalance after layer change failed", exc_info=True)
    return {"result": "已保存", "message": txt}


def us_deployments(include_paused: bool = True, include_test: bool = False) -> list[dict]:
    from .paper import runner as pr
    st = ACTIVE if include_paused else ("running",)
    rows = db.q("SELECT id FROM paper_deployments WHERE broker='alpaca_us' AND status IN "
                f"({','.join('?' * len(st))})" + ("" if include_test else " AND is_test=0") + " ORDER BY id", st)
    return [pr.get_deployment(r["id"]) for r in rows]


def _us_running(deps: list[dict]) -> bool:
    from .paper import us_exec
    return any(d["status"] == "running" and not us_exec.effective(d).get("pending_offline")
               and us_exec.effective(d).get("enabled", True) for d in deps)


def us_held_value(deps: list[dict] | None = None) -> float:
    from .paper import us_exec
    return sum(float(us_exec.dep_state(d).get("position", {}).get("market_value") or 0)
               for d in (us_deployments() if deps is None else deps))


def layer1(equity: float | None, deps: list[dict] | None = None) -> dict:
    """第一层：市场之间的比例（% of 账户权益）与计算过程。"""
    from . import evolve
    from .paper import etf_trend
    c = get_config()
    deps = us_deployments() if deps is None else deps
    reserve = float(evolve.get_config().get("reserve_pct", 0.0))
    us_c, cr_c = c["us_equity"], c["crypto"]
    running = _us_running(deps)
    raw_target = float(us_c["target"]) if running else 0.0
    us_target = min(max(raw_target, float(us_c["min"]) if running else 0.0), float(us_c["max"]))
    ls = etf_trend.lend_state()
    lend_pct = float(ls.get("lend_pct", etf_trend.get_config().get("lend_pct", 50.0)))
    lent = us_target * lend_pct / 100 if ls.get("state") == "lent" else 0.0
    held = us_held_value(deps)
    held_pct = held / equity * 100 if equity and equity > 0 else 0.0
    us_deduct = max(us_target - lent, held_pct)
    crypto_raw = 100 - reserve - us_deduct
    crypto = min(max(crypto_raw, float(cr_c["min"])), float(cr_c["max"]))
    crypto = max(0.0, min(crypto, 100 - us_deduct))
    cash = max(0.0, 100 - crypto - us_deduct)
    steps = [
        f"美股目标 {raw_target:g}%" + ("" if running else "（没有运行中的美股部署 → 0）")
        + f"，限制在 {float(us_c['min']):g}~{float(us_c['max']):g}% → {us_target:g}%",
        (f"美股空仓看空，临时借给加密货币 {lend_pct:g}% × {us_target:g}% = {lent:g}%" if lent else
         ("美股正在收回借出的额度" if ls.get("state") == "reclaim" else "没有借出额度")),
        f"美股实际占用 = max(美股目标 {us_target:g}% − 借出 {lent:g}%, 美股持仓市值占比 {held_pct:.1f}%) = {us_deduct:.1f}%",
        f"加密货币 = 100% − 现金保留 {reserve:g}% − 美股 {us_deduct:.1f}% = {crypto_raw:.1f}%，"
        f"限制在 {float(cr_c['min']):g}~{float(cr_c['max']):g}% → {crypto:.1f}%",
        f"现金 = 100% − 加密货币 {crypto:.1f}% − 美股 {us_deduct:.1f}% = {cash:.1f}%",
    ]
    return {"equity": equity, "reserve_pct": reserve, "us_target_pct": us_target, "us_raw_target_pct": raw_target,
            "us_min": float(us_c["min"]), "us_max": float(us_c["max"]), "lent_pct": lent, "lend_state": ls.get("state", "none"),
            "us_held_pct": round(held_pct, 4), "us_deduct_pct": round(us_deduct, 4), "crypto_pct": round(crypto, 4),
            "crypto_min": float(cr_c["min"]), "crypto_max": float(cr_c["max"]), "cash_pct": round(cash, 4),
            "rebalance_pp": float(c["rebalance_pp"]), "steps": steps}


def _strength(dep: dict) -> tuple[float | None, float | None, str]:
    """美股部署的实力指标：(年均收益或训练收益, 年均回撤或训练回撤, 方法)。"""
    from . import evolve, rhythm
    from .paper import us_exec
    if us_exec.kind(dep) == "etf_sma":
        m = rhythm.etf_long_metrics()
        if m:
            return float(m.get("mean_ret") or 0), float(m.get("mean_dd") or 0), "long"
        return None, None, "none"
    m = evolve._train_metrics(dep) or {}
    return m.get("return_pct"), m.get("max_dd_pct"), "train"


def plan_us(equity: float, deps: list[dict] | None = None, l1: dict | None = None) -> dict[int, dict]:
    """第二层（美股）：按现有实力公式在美股部署之间分配美股目标额度。返回 {部署: {budget, weight, share_pct, why, detail}}。
    额度不取整（与原 ETF 择时「权益 × 资金比例」一致）；暂停新开仓、等待下线的部署额度为 0（持仓照常按信号卖出）。"""
    from . import evolve
    from .paper import us_exec
    deps = us_deployments() if deps is None else deps
    l1 = l1 or layer1(equity, deps)
    c = evolve.get_config()
    wmin, wmax = float(c["weight_min"]), float(c["weight_max"])
    total = equity * l1["us_target_pct"] / 100 if equity else 0.0
    plan: dict[int, dict] = {}
    w = {}
    for d in deps:
        eff = us_exec.effective(d)
        if d["status"] != "running" or eff.get("pending_offline") or not eff.get("enabled", True):
            plan[d["id"]] = {"budget": 0.0, "weight": None, "share_pct": None,
                             "why": "暂停新开仓 / 等待下线：不分配新额度（持仓照常按信号卖出）", "detail": {"method": "paused"}}
            continue
        ret, dd, method = _strength(d)
        if ret is None:
            w[d["id"]] = (wmin, "没有实力指标，最低权重", {"method": "none", "weight": wmin})
            continue
        raw = (2 * ret / max(float(dd or 0), 1.0)) if method == "long" else (ret / max(abs(float(dd or 0)), 1.0))
        wt = wmin if ret <= 0 else min(max(raw, wmin), wmax)
        why = (f"长周期：年均收益 {ret:+.1f}% / 年均回撤 {abs(dd or 0):.1f}%" if method == "long"
               else f"训练集收益 {ret:+.1f}% / 回撤 {abs(dd or 0):.1f}%") + f" → 权重 {wt:.2f}"
        w[d["id"]] = (wt, why, {"method": method, "ret": ret, "dd": dd, "raw": round(raw, 4), "weight": wt,
                                "wmin": wmin, "wmax": wmax})
    tw = sum(x[0] for x in w.values()) or 1.0
    for did, (wt, why, det) in w.items():
        plan[did] = {"budget": total * wt / tw, "weight": round(wt, 3), "share_pct": round(wt / tw * 100, 2),
                     "why": why, "detail": det, "total": total, "total_weight": round(tw, 4)}
    return plan


def rebalance_us(equity: float) -> list[str]:
    """把第二层的额度写进美股部署配置（显示用；执行器每次下单时按当时权益重新计算）。"""
    from .paper import runner as pr
    out = []
    deps = us_deployments()
    plan = plan_us(equity, deps)
    for d in deps:
        p = plan.get(d["id"])
        if not p:
            continue
        old = float(d["config"].get("budget_usd") or 0)
        if abs(p["budget"] - old) < 100:
            continue
        cfg = d["config"]
        cfg["budget_usd"] = round(p["budget"], 2)
        cfg["budget_reason"] = p["why"]
        db.ex("UPDATE paper_deployments SET config_json=?, updated_at=? WHERE id=?",
              (json.dumps(cfg, ensure_ascii=False), db.now_iso(), d["id"]))
        pr.event(d["id"], "INFO", f"美股资金额度调整：{old:,.0f} → {p['budget']:,.0f} USD（{p['why']}）", False)
        out.append(f"#{d['id']} {old:,.0f}→{p['budget']:,.0f}")
    return out


def explain(equity: float | None) -> dict:
    """策略总台「资金两层分配」：第一层的计算过程 + 第二层每个部署的额度。"""
    from . import evolve
    from .paper import runner as pr
    from .paper import us_exec
    eq = float(equity or 0)
    l1 = layer1(eq)
    rows_us = []
    deps_us = us_deployments()
    for did, p in plan_us(eq, deps_us, l1).items():
        d = next(x for x in deps_us if x["id"] == did)
        st = us_exec.dep_state(d)
        mv = float((st.get("position") or {}).get("market_value") or 0)
        rows_us.append({"deployment": did, "label": us_exec.label(d), "budget": p["budget"], "share_pct": p["share_pct"],
                        "why": p["why"], "actual_pct": mv / eq * 100 if eq else None,
                        "target_pct": p["budget"] / eq * 100 if eq else None})
    deps_c = [pr.get_deployment(r["id"]) for r in db.q(
        "SELECT id FROM paper_deployments WHERE broker='alpaca_paper' AND is_test=0 AND status IN ('running','paused_entries') "
        "ORDER BY id")]
    rows_c = []
    if deps_c and eq:
        for did, p in evolve.plan_budgets(eq, deps_c).items():
            d = next(x for x in deps_c if x["id"] == did)
            rows_c.append({"deployment": did, "label": d["config"].get("label") or f"#{did}", "budget": p["budget"],
                           "share_pct": p.get("share_pct"), "why": p["why"]})
    return {"layer1": l1, "us": rows_us, "crypto": rows_c}
