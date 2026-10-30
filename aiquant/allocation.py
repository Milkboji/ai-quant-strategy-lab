"""资金分两层（2026-10-29），试用期与风控上限（2026-10-30）。

第一层：市场之间（加密货币 / 美股 / 现金）
- 美股目标比例（默认取美股 ETF 原来的资金比例 30%），限制在 [美股下限, 美股上限] 内；没有运行中的正式美股部署时为 0。
- 与借出机制兼容：美股全部空仓且看空时，美股目标的 lend_pct% 临时借给加密货币（见 etf_trend 的 lend_state）。
- 美股实际占用 = max(美股目标 − 借出部分, 正式美股部署持仓市值占比)。
- 加密货币 = 100% − 现金保留 − 美股实际占用，再限制在 [加密下限, 加密上限]；剩下的是现金（不少于现金保留）。

第二层：市场内部按现有实力公式（同一个公式，见 evolve.plan_budgets），只在「正式」部署之间分配。

试用期（新部署的策略；运行中的策略换版本不算）：
- 每个试用部署固定额度 = 权益 × trial_pct（默认 5%），所有试用部署合计不超过 trial_total_pct（默认 15%）；
  额度从现金层出（现金层超出现金保留的部分），不从正式策略的额度里扣；现金层不够时试用额度为 0 并提示。
- 试用满 trial_days（默认 60）个交易日（美股）/ 天（加密货币）后转为正式，参与按实力分配。

额度调整不强制卖出：任何额度变化都不卖出已有持仓，新的目标比例在该策略下一次开仓时生效。
例外（只加不减）：美股持仓中、目标比例比开仓时高出 add_pp 个百分点（默认 1）→ 下一个交易日按差额加仓；价格涨跌不调整。

风控上限：单个策略持仓市值超过账户权益的 position_cap_pct（默认 50%，美股和 BTC 都适用）→ 减到上限。
回测按账户口径换算：上限 ÷ 该策略额度占账户的比例 = 占策略自身资金的上限（≥ 100% 时不会触发）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from . import db

log = logging.getLogger("aiquant.alloc")
DEFAULTS = {"us_equity": {"target": None, "min": 0.0, "max": 60.0}, "crypto": {"min": 0.0, "max": 100.0},
            "add_pp": 1.0, "trial_pct": 5.0, "trial_total_pct": 15.0, "trial_days": 60, "position_cap_pct": 50.0}
ACTIVE = ("running", "paused_entries")


def get_config() -> dict:
    c = json.loads(json.dumps(DEFAULTS))
    s = db.get_setting("capital_layers") or {}
    for k in ("us_equity", "crypto"):
        c[k].update(s.get(k) or {})
    for k in ("add_pp", "trial_pct", "trial_total_pct", "trial_days", "position_cap_pct"):
        if s.get(k) is not None:
            c[k] = float(s[k]) if k != "trial_days" else int(s[k])
    if c["us_equity"]["target"] is None:
        from .paper import etf_trend
        c["us_equity"]["target"] = float(etf_trend.get_config().get("budget_pct", 30.0))
    return c


def set_config(*, actor: str | None, role: str | None, us_target=None, us_min=None, us_max=None, crypto_min=None,
               crypto_max=None, add_pp=None, trial_pct=None, trial_total_pct=None, trial_days=None,
               position_cap_pct=None) -> dict:
    """修改第一层上下限 / 美股目标 / 加仓阈值 / 试用期 / 风控上限（管理员、交易员），写审计并立即重新分配额度（不强制卖出）。"""
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
    if add_pp is not None:
        new["add_pp"] = num(add_pp, 0.5, 50, "加仓阈值")
    if trial_pct is not None:
        new["trial_pct"] = num(trial_pct, 0, 20, "试用额度")
    if trial_total_pct is not None:
        new["trial_total_pct"] = num(trial_total_pct, 0, 50, "试用额度合计上限")
    if trial_days is not None:
        new["trial_days"] = int(num(trial_days, 1, 365, "试用期天数"))
    if position_cap_pct is not None:
        new["position_cap_pct"] = num(position_cap_pct, 5, 100, "单个策略持仓上限")
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
           f"加密货币 {cur['crypto']['min']:g}~{cur['crypto']['max']:g}%，目标比例增加 {cur['add_pp']:g} 个百分点以上才加仓；"
           f"试用 每个 {cur['trial_pct']:g}%、合计 ≤ {cur['trial_total_pct']:g}%、{cur['trial_days']} 个交易日 / 天；"
           f"单个策略持仓上限 {cur['position_cap_pct']:g}%")
    console.audit(actor, role, "修改资金分层", "capital", "资金两层分配", None, None, None, txt, "")
    try:
        from . import evolve
        evolve.rebalance()
    except Exception:  # noqa: BLE001
        log.warning("rebalance after layer change failed", exc_info=True)
    return {"result": "已保存", "message": txt}


# ---------------------------------------------------------------------------
# 试用期
# ---------------------------------------------------------------------------

def start_trial(cfg: dict, market_key: str, now: datetime | None = None) -> dict:
    """新部署（之前没在运行的策略）进入试用期：写入部署配置。返回更新后的 cfg。"""
    now = now or datetime.now(timezone.utc)
    cfg = dict(cfg)
    cfg["trial"] = {"since": now.replace(microsecond=0).isoformat(), "market": market_key,
                    "unit": "trading_days" if market_key != "crypto" else "days"}
    return cfg


def trial_status(dep: dict, now: datetime | None = None) -> dict:
    """{"trial": bool, "done": x, "need": 60, "unit", "text": "试用中（x/60）" | "正式"}。没有 trial 记录的部署 = 正式。"""
    from . import market as mk
    t = (dep.get("config") or {}).get("trial")
    need = int(get_config()["trial_days"])
    if not t or t.get("graduated_at"):
        return {"trial": False, "done": None, "need": need, "unit": None, "text": "正式"}
    now = now or datetime.now(timezone.utc)
    since = datetime.fromisoformat(t["since"])
    if t.get("unit") == "trading_days":
        d0, d1 = since.astimezone(mk.ET).date(), now.astimezone(mk.ET).date()
        days = mk.trading_days(d0, d1)
        done = sum(1 for d in days if d > d0 and now >= mk.session_close(d)) + (
            1 if mk.is_trading_day(d0) and since <= mk.session_open(d0) and now >= mk.session_close(d0) else 0)
        unit = "个交易日"
    else:
        done = int((now - since).total_seconds() // 86400)
        unit = "天"
    if done >= need:
        return {"trial": False, "done": done, "need": need, "unit": unit, "text": "正式", "due": True}
    return {"trial": True, "done": done, "need": need, "unit": unit, "text": f"试用中（{done}/{need}）"}


def graduate_trials(now: datetime | None = None) -> list[str]:
    """试用期满的部署转为正式：写配置、事件、审计。由 evolve.rebalance 调用（每天至少一次）。"""
    from . import console
    from .paper import runner as pr
    out = []
    for r in db.q("SELECT id FROM paper_deployments WHERE status IN ('running','paused_entries') AND is_test=0 "
                  "AND json_extract(config_json,'$.trial') IS NOT NULL"):
        dep = pr.get_deployment(r["id"])
        st = trial_status(dep, now)
        if st.get("due") and not dep["config"]["trial"].get("graduated_at"):
            cfg = dep["config"]
            cfg["trial"] = dict(cfg["trial"], graduated_at=db.now_iso())
            db.ex("UPDATE paper_deployments SET config_json=?, updated_at=? WHERE id=?",
                  (json.dumps(cfg, ensure_ascii=False), db.now_iso(), dep["id"]))
            msg = f"试用期满 {st['done']} {st['unit']}，转为正式：参与按实力分配（新额度在下一次开仓时生效，不强制卖出）"
            pr.event(dep["id"], "INFO", msg, bool(dep["is_test"]))
            console.audit("系统", "system", "试用转正式", cfg.get("strategy_key"), cfg.get("label"), dep["id"],
                          dep["version_id"], None, "正式", msg)
            out.append(f"#{dep['id']} 转为正式")
    return out


def is_trial(dep: dict, now: datetime | None = None) -> bool:
    return trial_status(dep, now)["trial"]


def trial_plan(equity: float, l1: dict | None = None, now: datetime | None = None) -> dict[int, dict]:
    """试用部署的固定额度（两个市场一起排队，先部署的先分）：每个 min(trial_pct, 剩余)，合计 ≤ min(trial_total_pct, 现金层空余)。"""
    from .paper import runner as pr
    c = get_config()
    l1 = l1 or layer1(equity)
    free = max(0.0, l1["cash_pct"] - l1["reserve_pct"])
    pool = min(float(c["trial_total_pct"]), free)
    out = {}
    for r in db.q("SELECT id FROM paper_deployments WHERE broker IN ('alpaca_paper','alpaca_us') AND is_test=0 "
                  "AND status IN ('running','paused_entries') ORDER BY id"):
        dep = pr.get_deployment(r["id"])
        st = trial_status(dep, now)
        if not st["trial"]:
            continue
        if dep["status"] != "running" or dep["config"].get("pending_offline"):
            out[dep["id"]] = {"budget": 0.0, "pct": 0.0, "status": st, "why": f"{st['text']}；暂停中，不分配额度"}
            continue
        pct = max(0.0, min(float(c["trial_pct"]), pool))
        pool -= pct
        why = (f"{st['text']}：固定额度 {pct:g}% × 权益 {equity:,.0f} = {equity * pct / 100:,.0f} USD（从现金层出）"
               if pct > 0 else f"{st['text']}：{trial_zero_hint(l1, c)}")
        out[dep["id"]] = {"budget": equity * pct / 100 if equity else 0.0, "pct": pct, "status": st, "why": why}
    return out


def trial_zero_hint(l1: dict, c: dict | None = None) -> str:
    """现金层没有空余时的提示：写明要调哪个参数、调到多少。"""
    c = c or get_config()
    free = max(0.0, l1["cash_pct"] - l1["reserve_pct"])
    want = float(c["trial_total_pct"])
    cr_max = float(c["crypto"]["max"])
    to = max(0.0, min(cr_max, float(l1["crypto_pct"])) - (want - free))
    return (f"现金层没有空余（现金 {l1['cash_pct']:.1f}% 只够现金保留 {l1['reserve_pct']:g}%），试用额度为 0。"
            f"要给试用留出空间，请调「策略总台 → 资金两层分配 → 加密货币上限 %」（当前设置 {cr_max:g}%，实际 {float(l1['crypto_pct']):.0f}%）："
            f"调到 {to:.0f}% 可腾出 {want:g}%（{int(want // float(c['trial_pct']) or 1)} 个试用额度，每个 {float(c['trial_pct']):g}%）。"
            "调低后不会强制卖出 BTC 持仓，新额度在各策略下一次开仓时生效")


def position_cap(equity: float) -> float:
    """单个策略持仓上限（USD）。"""
    return float(equity) * float(get_config()["position_cap_pct"]) / 100


def backtest_cap_frac(share_pct: float | None = None) -> float | None:
    """回测里「单个策略持仓上限」按账户口径换算为占策略自身资金的比例：上限 ÷ 该策略额度占账户的比例。
    share_pct 为空时按新部署的试用额度计算。结果 ≥ 1（自身资金的 100%，无杠杆时不可能超过）→ None（不触发）。"""
    c = get_config()
    share = float(share_pct) if share_pct else float(c["trial_pct"] or 5.0)
    frac = float(c["position_cap_pct"]) / max(share, 1e-9)
    return None if frac >= 1.0 else frac


def us_deployments(include_paused: bool = True, include_test: bool = False) -> list[dict]:
    from .paper import runner as pr
    st = ACTIVE if include_paused else ("running",)
    rows = db.q("SELECT id FROM paper_deployments WHERE broker='alpaca_us' AND status IN "
                f"({','.join('?' * len(st))})" + ("" if include_test else " AND is_test=0")
                + " AND COALESCE(json_extract(config_json,'$.kind'),'')<>'my_system' ORDER BY id", st)   # 我的交易系统：额度见 trial_plan
    return [pr.get_deployment(r["id"]) for r in rows]


def _us_running(deps: list[dict]) -> bool:
    from .paper import us_exec
    return any(d["status"] == "running" and not us_exec.effective(d).get("pending_offline")
               and us_exec.effective(d).get("enabled", True) and not is_trial(d) for d in deps)


def us_held_value(deps: list[dict] | None = None) -> float:
    """正式美股部署的持仓市值（试用部署的持仓由现金层的试用额度承担）。"""
    from .paper import us_exec
    return sum(float(us_exec.dep_state(d).get("position", {}).get("market_value") or 0)
               for d in (us_deployments() if deps is None else deps) if not is_trial(d))


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
            "add_pp": float(c["add_pp"]), "position_cap_pct": float(c["position_cap_pct"]), "steps": steps}


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
    trials = None
    for d in deps:
        eff = us_exec.effective(d)
        if is_trial(d):
            trials = trials if trials is not None else trial_plan(equity, l1)
            t = trials.get(d["id"]) or {"budget": 0.0, "why": "试用中"}
            plan[d["id"]] = {"budget": t["budget"], "weight": None, "share_pct": None, "why": t["why"],
                             "detail": {"method": "trial", "pct": t.get("pct")}, "trial": True}
            continue
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
                        "target_pct": p["budget"] / eq * 100 if eq else None, "status": trial_status(d)["text"]})
    deps_c = [pr.get_deployment(r["id"]) for r in db.q(
        "SELECT id FROM paper_deployments WHERE broker='alpaca_paper' AND is_test=0 AND status IN ('running','paused_entries') "
        "ORDER BY id")]
    rows_c = []
    if deps_c and eq:
        for did, p in evolve.plan_budgets(eq, deps_c).items():
            d = next(x for x in deps_c if x["id"] == did)
            rows_c.append({"deployment": did, "label": d["config"].get("label") or f"#{did}", "budget": p["budget"],
                           "share_pct": p.get("share_pct"), "why": p["why"], "status": trial_status(d)["text"]})
    trials = trial_plan(eq, l1) if eq else {}
    l1 = dict(l1, trial_pct=round(sum(t["pct"] for t in trials.values()), 4),
              steps=l1["steps"] + [f"试用：{len(trials)} 个试用部署，从现金层（超出现金保留的部分 "
                                   f"{max(0.0, l1['cash_pct'] - l1['reserve_pct']):.1f}%）分配 "
                                   f"{sum(t['pct'] for t in trials.values()):g}%（每个最多 {get_config()['trial_pct']:g}%，"
                                   f"合计最多 {get_config()['trial_total_pct']:g}%）"])
    free = max(0.0, l1["cash_pct"] - l1["reserve_pct"])
    l1["trial_hint"] = ("新部署的策略将进入试用：" + trial_zero_hint(l1)) if free < 0.01 else None
    return {"layer1": l1, "us": rows_us, "crypto": rows_c}
