"""交易节奏总控：统一纪律（冷却 / 连亏刹车 / 波动率定仓）、长周期体检与按长期实力分配资金。

规则来源：2021-03 → 2026-10 的 BTC 与 ETH 5 分钟历史，对当时全部 8 个模拟盘策略逐年回测（每个资产 6 个年度），
比较 25 种节奏方案后选定（见 docs/trading_rhythm.md）。

- 回测（evolve / challenger）与模拟盘（runner）使用同一套 Rhythm 规则，保证“进化时看到的”就是“实际执行的”。
- 长周期体检：每天用 2021 年起的历史，对每个部署当前版本做 6 个年度的回测（含节奏规则）。
  平均年收益为负且盈利年份少于 3 个 → 暂停新开仓（长期没有优势的策略不再交易）；换上长期达标的新版本后自动恢复。
- 资金分配优先使用长周期结果（平均年收益 / 平均年度回撤），没有长周期结果时才用一年训练集。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timezone

import pandas as pd

from . import db
from .engine.rhythm import STOP_REASONS, Gate, Rhythm, daily_context, lookup

log = logging.getLogger("aiquant.rhythm")

# 选定方案（研究代号 A2）：止损后冷却 72 小时；连亏 3 笔暂停 72 小时；年化波动率目标 40%，最低 30% 仓位。
RULES_V1 = Rhythm(cooldown_hours_after_stop=72, loss_streak_n=3, loss_streak_pause_hours=72,
                  vol_target_pct=40, vol_lookback_days=20, vol_min_mult=0.3)
# 美股（时长单位：交易日；年化 √252）：连亏 2 笔暂停开仓 20 个交易日。波动率定仓在 QQQ→QLD 上降低收益/回撤比，
# 不采用（研究见 docs/trading_rhythm.md「美股」一节）。代码与加密货币完全相同，只是参数不同。
RULES_US_V1 = Rhythm(loss_streak_n=2, loss_streak_pause_hours=20)
DEFAULTS = {
    "enabled": True,
    "rules": RULES_V1.to_dict(),
    "rules_us": RULES_US_V1.to_dict(),   # 美股节奏规则（时长单位：交易日；年化 √252），见 docs/trading_rhythm.md
    "min_signal_tf": 60,          # 自动进化不再生成 1 小时以下的版本（0.25% 单边手续费下，短周期长期被成本吃掉）
    "long_veto": True,            # 长周期体检不达标 → 暂停新开仓（加密货币）
    "long_us": "hint",            # 美股长周期体检：hint = 仅作提示、不拦截（2026-10-29 用户决定；GLD→UGL 上会长期误拦）
    "long_min_pos_years": 3,
    "long_years": 6,
}


def get_config() -> dict:
    c = dict(DEFAULTS)
    c.update(db.get_setting("rhythm_config") or {})
    return c


def set_config(**kw) -> dict:
    c = get_config()
    c.update(kw)
    db.set_setting("rhythm_config", c)
    return c


def _rules_key(market=None) -> str:
    """加密货币用 rules（时长单位：小时）；美股用 rules_us（时长单位：交易日）。同一套 Rhythm 代码。"""
    return "rules" if market is None or getattr(market, "key", "crypto") == "crypto" else "rules_us"


def _is_us(market) -> bool:
    return market is not None and getattr(market, "key", "crypto") != "crypto"


US_MODES = {"shadow": "影子模式（只计算显示，不影响下单和回测）", "active": "生效"}


def us_mode() -> str:
    """美股节奏的生效方式。2026-10-29 起默认「影子」：训练期重做研究的结论确认前，只计算和显示，不影响下单。"""
    m = get_config().get("us_mode") or "shadow"
    return m if m in US_MODES else "shadow"


def is_shadow(market=None) -> bool:
    return _is_us(market) and us_mode() == "shadow"


def configured(market=None) -> Rhythm | None:
    """设置里的规则（不论影子与否）；影子模式下用它计算「如果生效会怎样」。"""
    c = get_config()
    if not c.get("enabled"):
        return None
    rules = c.get(_rules_key(market))
    if not rules:
        return None
    rh = Rhythm.from_dict(rules)
    return rh if rh.active else None


def current(market=None) -> Rhythm | None:
    """实际执行的规则：回测、进化、模拟盘都用它。美股影子模式下返回 None（不影响任何下单和回测）。"""
    if is_shadow(market):
        return None
    return configured(market)


def rules_hash(market=None) -> str:
    c = get_config()
    parts = [c.get("enabled"), c.get(_rules_key(market))]
    if _is_us(market):
        parts.append(us_mode())
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:10]


def set_us_mode(mode: str, *, actor: str | None, role: str | None) -> dict:
    """切换美股节奏 影子 / 生效（管理员、交易员），写审计。
    转为生效的前提（2026-10-29 用户决定）：影子模式已记录满 SHADOW_MIN_DAYS 个交易日、影子日志没有计算错误或异常；
    生效时只启用连亏刹车（RULES_US_V1），波动率定仓不启用，长周期体检仍只作提示。"""
    from . import console
    console.require_role(role)
    if mode not in US_MODES:
        raise ValueError("模式只能是 shadow / active")
    old = us_mode()
    if old == mode:
        return {"result": "无变化", "message": f"美股节奏已是{US_MODES[mode]}"}
    extra = ""
    if mode == "active":
        ps = us_promotion_status()
        if not ps["ready"]:
            raise ValueError(f"还不能转为生效：{ps['text']}")
        set_config(rules_us=RULES_US_V1.to_dict(), long_us="hint")
        extra = f"；只启用连亏刹车（{'；'.join(RULES_US_V1.describe('个交易日'))}），波动率定仓不启用，长周期体检仅作提示；{ps['text']}"
    set_config(us_mode=mode, us_mode_by=actor or "用户", us_mode_at=db.now_iso())
    console.audit(actor, role, "美股节奏模式", "rhythm_us", "美股交易节奏", None, None, None, US_MODES[mode],
                  f"{US_MODES[old]} → {US_MODES[mode]}{extra}")
    from .paper import runner as pr
    pr.event(None, "INFO", f"[ETF趋势] {actor or '用户'} 把美股交易节奏改为：{US_MODES[mode]}")
    return {"result": "已切换", "message": f"美股交易节奏：{US_MODES[mode]}"}


def ensure_us_shadow_default() -> None:
    """一次性：把美股节奏设为影子模式并写审计（重做研究前不影响下单）。"""
    c = get_config()
    if c.get("us_mode_at"):
        return
    set_config(us_mode="shadow", us_mode_by="系统", us_mode_at=db.now_iso())
    try:
        from . import console
        console.audit("系统", "system", "美股节奏模式", "rhythm_us", "美股交易节奏", None, None, None, US_MODES["shadow"],
                      "训练期重做研究前，美股节奏改为影子模式：只计算显示，不影响下单和回测")
    except Exception:  # noqa: BLE001
        log.exception("us shadow audit failed")


# ---------------------------------------------------------------------------
# 美股影子模式：每个交易日记录一次计算结果并自检；满 SHADOW_MIN_DAYS 个交易日且无异常 → 提醒管理员确认转正
# ---------------------------------------------------------------------------

SHADOW_MIN_DAYS = 20
SHADOW_LOG_KEY = "us_shadow_log"


def shadow_count_from() -> str:
    """转正计数起点（美东日期）：2026-10-29 核对节奏单位之后首次运行的那天，一次性写入。"""
    d = db.get_setting("us_shadow_count_from")
    if not d:
        from . import market as mk
        d = str(datetime.now(timezone.utc).astimezone(mk.ET).date())
        db.set_setting("us_shadow_count_from", d)
    return d


def shadow_log() -> list[dict]:
    return list(db.get_setting(SHADOW_LOG_KEY) or [])


def _save_shadow(rec: dict) -> None:
    rows = [r for r in shadow_log() if r.get("date") != rec["date"]] + [rec]
    db.set_setting(SHADOW_LOG_KEY, sorted(rows, key=lambda r: r["date"])[-400:])


def shadow_error(day: str, msg: str) -> None:
    """影子模式计算中出现的错误（离场记账、开仓判断异常等），记到当天的影子日志里，阻止转正。"""
    rec = next((r for r in shadow_log() if r.get("date") == day), None) or {"date": day, "at": db.now_iso(), "errors": []}
    rec["errors"] = list(rec.get("errors") or []) + [msg[:300]]
    _save_shadow(rec)
    log.error("us shadow error %s: %s", day, msg)


def shadow_record(day: str, now) -> dict | None:
    """每个交易日一次：用设置里的美股规则计算「如果生效会怎样」，并自检（连亏计数范围、冷却结束时刻必须是交易日开盘）。
    只在美股影子模式下记录。"""
    from . import market as mk
    if not is_shadow(mk.US_EQUITY):
        return None
    old = next((r for r in shadow_log() if r.get("date") == day), None)
    if old and ("streak" in old or old.get("note")):
        return old
    rec = {"date": day, "at": db.now_iso(), "errors": list((old or {}).get("errors") or [])}
    try:
        rh = configured(mk.US_EQUITY)
        if rh is None:
            rec["note"] = "交易节奏总开关关闭，本日不计算"
        else:
            g = load_gate(ETF_KEY, rh, mk.US_EQUITY)
            t = pd.Timestamp(now)
            t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
            rec.update(block=g.check(t) or "", streak=int(g.streak),
                       until=g.block_until.isoformat() if g.block_until is not None else None)
            if g.streak < 0 or (rh.loss_streak_n and g.streak >= rh.loss_streak_n):
                rec["errors"].append(f"连亏计数越界：{g.streak}（规则 {rh.loss_streak_n} 笔）")
            if g.block_until is not None:
                bu = pd.Timestamp(g.block_until)
                bu = bu.tz_localize("UTC") if bu.tzinfo is None else bu.tz_convert("UTC")
                d = bu.tz_convert(mk.ET).date()
                if not mk.is_trading_day(d) or pd.Timestamp(mk.session_open(d)) != bu:
                    rec["errors"].append(f"冷却结束时刻 {bu.isoformat()} 不是某个交易日的开盘时刻（单位或日历有误）")
    except Exception as e:  # noqa: BLE001
        rec["errors"].append(f"计算异常：{type(e).__name__}: {str(e)[:200]}")
    _save_shadow(rec)
    try:
        _maybe_review_notice()
    except Exception:  # noqa: BLE001
        log.exception("shadow review notice failed")
    return rec


def us_promotion_status() -> dict:
    """美股节奏能否从影子模式转为生效（只转连亏刹车）：影子日志满 SHADOW_MIN_DAYS 个交易日，且没有计算错误或异常。"""
    from . import market as mk
    from datetime import date as _date

    from . import uptime
    start = shadow_count_from()
    recs = [r for r in shadow_log() if r.get("date", "") >= start]
    errs = [(r["date"], e) for r in recs for e in (r.get("errors") or [])]
    out_rows = uptime.outages(f"{start}T00:00:00+00:00")
    done = [r for r in recs if "streak" in r or r.get("note")]
    broken = [r["date"] for r in done if not uptime.full_run_day(_date.fromisoformat(r["date"]), out_rows)]
    days = len(done) - len(broken)     # 只计服务完整运行的交易日（开盘前 10 分钟到收盘后 30 分钟没有中断）
    today = datetime.now(timezone.utc).astimezone(mk.ET).date()
    try:
        from datetime import date as _d
        expected = [str(x) for x in mk.trading_days(_d.fromisoformat(start), today)]
    except Exception:  # noqa: BLE001
        expected = []
    seen = {r["date"] for r in recs}
    missing = [x for x in expected[:-1] if x not in seen]     # 今天可能还没运行，不算缺
    shadow = us_mode() == "shadow"
    ready = shadow and days >= SHADOW_MIN_DAYS and not errs
    if not shadow:
        text = "美股节奏已生效（只启用连亏刹车）"
    elif errs:
        text = f"影子日志有 {len(errs)} 处计算错误或异常（最近：{errs[-1][0]} {errs[-1][1]}），需要先查明"
    elif days < SHADOW_MIN_DAYS:
        text = f"影子模式已记录 {days}/{SHADOW_MIN_DAYS} 个交易日（{start} 起），没有异常"
    else:
        text = f"影子模式已记录 {days} 个交易日（{start} 起），没有计算错误或异常：系统会自动把连亏刹车切为生效"
    if missing and shadow:
        text += f"；服务未运行、没有记录的交易日 {len(missing)} 个（不计入天数）"
    if broken and shadow:
        text += f"；交易时段内服务中断过的交易日 {len(broken)} 个（{', '.join(broken[-5:])}，不计入天数）"
    return {"ready": ready, "days": days, "need": SHADOW_MIN_DAYS, "errors": errs, "missing": missing, "start": start,
            "text": text, "shadow": shadow, "interrupted": broken}


def _maybe_review_notice() -> None:
    ps = us_promotion_status()
    if not ps["ready"] or db.get_setting("us_shadow_review_notified"):
        return
    db.set_setting("us_shadow_review_notified", db.now_iso())
    from .paper import runner as pr
    pr.event(None, "INFO", f"[ETF趋势] {ps['text']}")
    from . import notify
    notify.event("rhythm_review", "美股交易节奏：影子模式已满 20 个交易日，待确认转正",
                 ps["text"] + "\n请到「自动进化 → 交易节奏」确认是否把连亏刹车切为生效（波动率定仓不启用，长周期体检仅作提示）。",
                 dedupe="us-shadow-review")


# ---------------------------------------------------------------------------
# 模拟盘：节奏状态（每个部署一份，存 settings，重启不丢）
# ---------------------------------------------------------------------------

def _key(did: int) -> str:
    return f"rhythm_state::{did}"


def load_gate(did, rh: Rhythm, market=None) -> Gate:
    g = Gate(rh, market)
    s = db.get_setting(_key(did)) or {}
    if s.get("block_until"):
        g.block_until = pd.Timestamp(s["block_until"])
        g.block_why = s.get("block_why", "")
    g.streak = int(s.get("streak", 0))
    g.entries_day = dict(s.get("entries_day") or {})
    return g


def save_gate(did: int, g: Gate) -> None:
    days = sorted(g.entries_day)[-3:]
    db.set_setting(_key(did), {
        "block_until": g.block_until.isoformat() if g.block_until is not None else None,
        "block_why": g.block_why, "streak": g.streak,
        "entries_day": {k: g.entries_day[k] for k in days}, "updated_at": db.now_iso()})


def live_gate(did, now: datetime, df5: pd.DataFrame, market=None) -> tuple[str | None, float, dict]:
    """返回 (阻止原因 或 None, 仓位系数, 说明)。规则关闭时 (None, 1.0, {})。
    did：部署 id（BTC）或 "etf"（美股 ETF 择时）；market 决定时长单位与年化口径。
    美股影子模式下也照常计算（调用方决定是否真的阻止），所以这里用 configured()。"""
    rh = configured(market)
    if rh is None:
        return None, 1.0, {}
    t = pd.Timestamp(now)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    ok, vm = True, 1.0
    if rh.regime_sma_days or rh.vol_target_pct:
        o, v = lookup(daily_context(df5, rh, market), [t])
        ok, vm = bool(o[0]), float(v[0])
    g = load_gate(did, rh, market)
    why = g.check(t, ok)
    return why, vm, {"vol_mult": round(vm, 3), "streak": g.streak}


def live_on_entry(did, now: datetime, market=None) -> None:
    rh = configured(market)
    if rh is None:
        return
    g = load_gate(did, rh, market)
    t = pd.Timestamp(now)
    g.on_entry(t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC"))
    save_gate(did, g)


def live_on_exit(did, now: datetime, reason: str, pnl: float, market=None) -> str | None:
    """记录一次离场；返回新产生的冷却说明（用于日志）。影子模式下照常记录，便于显示「如果生效会怎样」。"""
    rh = configured(market)
    if rh is None:
        return None
    g = load_gate(did, rh, market)
    before = g.block_until
    t = pd.Timestamp(now)
    g.on_exit(t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC"), reason, pnl)
    save_gate(did, g)
    if g.block_until is not None and g.block_until != before:
        return g.check(g.block_until - pd.Timedelta(seconds=1))
    return None


def reset_gate(did: int) -> None:
    db.set_setting(_key(did), {})


# ---------------------------------------------------------------------------
# 长周期体检（2021 年起逐年回测，含节奏规则）
# ---------------------------------------------------------------------------

def _hist():
    from . import challenger
    return challenger._history("BTC/USD")


def _long_key(vid: int, tf: int, aux) -> str:
    return f"rhythm_long::{vid}::{tf}::{aux or 0}"


def long_metrics(dep: dict, compute: bool = False) -> dict | None:
    """部署当前版本的长周期结果；compute=False 时只读缓存（health_check 每分钟调用，不做重计算）。"""
    from . import challenger, evolve, research
    from .strategy import registry
    vid = dep["version_id"]
    v = registry.get_version(vid)
    if not v:
        return None
    sch = dict(v["schema"])
    tf = int(dep["config"].get("signal_tf") or sch.get("signal_tf") or 15)
    aux = dep["config"].get("aux_tf") or sch.get("aux_tf")
    sch["signal_tf"], sch["aux_tf"] = tf, aux
    key = _long_key(vid, tf, aux)
    df = _hist() if compute else None
    sig = rules_hash()
    cached = db.get_setting(key)
    if cached and cached.get("rules") == sig and (not compute or cached.get("hist_end") == (
            str(df["ts"].iloc[-1]) if df is not None and len(df) else None)):
        return cached
    if not compute:
        return cached if cached and cached.get("rules") == sig else None
    if df is None or len(df) < 288 * 400:
        return None
    end = df["ts"].iloc[-1].to_pydatetime().replace(hour=0, minute=0, second=0, microsecond=0)
    folds = challenger._folds(df, end, warmup_days=60, years=int(get_config()["long_years"]))
    r = research.get_research()
    years = []
    for a, b in folds:
        try:
            m = evolve.evaluate(df, sch, a, b, r)["metrics"]
        except Exception as e:  # noqa: BLE001
            log.info("long fold failed: %s", e)
            continue
        years.append({"start": a.strftime("%Y-%m-%d"), "end": b.strftime("%Y-%m-%d"),
                      "ret": round(m["net_return_pct"], 2), "dd": round(m["max_drawdown_pct"], 2),
                      "trades": m["trades"]})
    if not years:
        return None
    mr = sum(y["ret"] for y in years) / len(years)
    md_ = sum(abs(y["dd"]) for y in years) / len(years)
    out = {"rules": sig, "hist_end": str(df["ts"].iloc[-1]), "at": db.now_iso(), "years": years,
           "mean_ret": round(mr, 2), "mean_dd": round(md_, 2), "pos_years": sum(1 for y in years if y["ret"] > 0),
           "n_years": len(years)}
    out["ok"] = not (mr < 0 and out["pos_years"] < int(get_config()["long_min_pos_years"]))
    db.set_setting(key, out)
    return out


ETF_KEY = "etf"


def etf_long_metrics(compute: bool = False) -> dict | None:
    """美股 ETF 择时的长周期体检：2017 年起逐个自然年（含美股节奏规则，√252），口径与 BTC 相同：
    平均年收益为负且盈利年份少于 long_min_pos_years → 不达标（暂停新开仓）。"""
    from . import challenger
    from . import market as mk
    from .engine import regime_bt
    from .paper import etf_trend
    cfg = etf_trend.get_config()
    sig = rules_hash(mk.US_EQUITY)
    key = "rhythm_long::etf"
    cached = db.get_setting(key)
    p = {"sma": int(cfg["sma"]), "band": float(cfg["band"]), "sig": cfg["signal_symbol"], "trade": cfg["trade_symbol"]}
    if not compute:
        return cached if cached and cached.get("rules") == sig and cached.get("params") == p else None
    df = challenger._etf_data()
    if df is None or len(df) < 600:
        return None
    end = str(df.index[-1].date())
    if cached and cached.get("rules") == sig and cached.get("params") == p and cached.get("hist_end") == end:
        return cached
    from . import allocation
    from . import spread_model
    years = regime_bt.yearly(df, p["sma"], p["band"], max(2017, df.index[0].year + 1), df.index[-1].year,
                             market=mk.US_EQUITY, rhythm=configured(mk.US_EQUITY),
                             cap_frac=allocation.backtest_cap_frac(allocation.get_config()["us_equity"]["target"]),
                             spread_bps=spread_model.open_spread(p["trade"]))
    if not years:
        return None
    mr = sum(y["ret"] for y in years) / len(years)
    md_ = sum(abs(y["dd"]) for y in years) / len(years)
    out = {"rules": sig, "params": p, "hist_end": end, "at": db.now_iso(),
           "years": [dict(y, start=f"{y['year']}-01-01", end=f"{y['year']}-12-31") for y in years],
           "mean_ret": round(mr, 2), "mean_dd": round(md_, 2), "pos_years": sum(1 for y in years if y["ret"] > 0),
           "n_years": len(years)}
    out["ok"] = not (mr < 0 and out["pos_years"] < int(get_config()["long_min_pos_years"]))
    db.set_setting(key, out)
    return out


def us_long_hint_only() -> bool:
    """美股长周期体检只作提示（不拦截回测和模拟盘）。"""
    return (get_config().get("long_us") or "hint") == "hint"


US_LONG_HINT_TEXT = "美股长周期体检仅作提示"


def etf_long_hint() -> dict | None:
    """美股 ETF 长周期体检不达标时返回体检结果（只用于提示，不拦截）；达标或尚未计算时返回 None。"""
    m = etf_long_metrics()
    return m if (m and not m.get("ok")) else None


def etf_long_paused(include_shadow: bool = False) -> bool:
    """长周期体检不达标且开启了「体检不达标暂停」→ 美股 ETF 不开新仓（持仓照常按信号离场）。
    美股长周期体检只作提示时（默认）恒为 False；影子模式下返回 False（不影响下单）；
    include_shadow=True 时返回「如果生效是否会暂停」。"""
    from . import market as mk
    c = get_config()
    if not c.get("enabled") or not c.get("long_veto") or us_long_hint_only():
        return False
    if is_shadow(mk.US_EQUITY) and not include_shadow:
        return False
    m = etf_long_metrics()
    return bool(m and not m.get("ok"))


def _etf_long_refresh() -> str | None:
    before = bool((db.get_setting("etf_long_state") or {}).get("paused"))
    m = etf_long_metrics(compute=True)
    if not m:
        return None
    paused = etf_long_paused()
    hint_before = bool((db.get_setting("etf_long_state") or {}).get("hint"))
    hint = not m.get("ok") and us_long_hint_only()
    db.set_setting("etf_long_state", {"paused": paused, "would_pause": etf_long_paused(include_shadow=True),
                                      "hint": hint, "at": db.now_iso()})
    from .paper import runner as pr
    txt = f"年均 {m['mean_ret']:+.1f}% 回撤 {m['mean_dd']:.1f}% 盈利 {m['pos_years']}/{m['n_years']} 年"
    if hint and not hint_before:
        pr.event(None, "INFO", f"[ETF趋势] {US_LONG_HINT_TEXT}：不达标（{txt}），不拦截开仓")
        try:
            from . import notify
            notify.event("long_hint", "美股 ETF 择时：长周期体检不达标（仅作提示）", f"{txt}\n{US_LONG_HINT_TEXT}，不拦截开仓。",
                         dedupe=f"etf-long-hint-{m['hist_end']}")
        except Exception:  # noqa: BLE001
            pass
    if paused and not before:
        pr.event(None, "WARN", f"[ETF趋势] 长周期体检不达标，暂停新开仓（{txt}）")
        try:
            from . import notify
            notify.event("auto_pause", "美股 ETF 择时：长周期体检不达标，暂停新开仓", txt, dedupe=f"etf-long-{m['hist_end']}")
        except Exception:  # noqa: BLE001
            pass
    elif before and not paused:
        pr.event(None, "INFO", f"[ETF趋势] 长周期体检恢复达标，恢复开仓（{txt}）")
    return f"美股 ETF {txt}"


def refresh_long_all() -> list[str]:
    from .paper import runner as pr
    out = []
    for d in db.q("SELECT id FROM paper_deployments WHERE broker='alpaca_paper' AND is_test=0 "
                  "AND status IN ('running','paused_entries') ORDER BY id"):
        dep = pr.get_deployment(d["id"])
        try:
            m = long_metrics(dep, compute=True)
        except Exception as e:  # noqa: BLE001
            log.error("long metrics #%s: %s", d["id"], e)
            continue
        if m:
            out.append(f"#{d['id']} 年均 {m['mean_ret']:+.1f}% 回撤 {m['mean_dd']:.1f}% 盈利 {m['pos_years']}/{m['n_years']} 年")
    try:
        e = _etf_long_refresh()
        if e:
            out.append(e)
    except Exception as e:  # noqa: BLE001
        log.error("etf long metrics: %s", e)
    db.set_setting("rhythm_long_refreshed", {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                                             "at": db.now_iso(), "rows": out})
    return out


_daily_lock = threading.Lock()


def maybe_daily(background: bool = True) -> bool:
    """每天（UTC）一次：刷新长周期体检 → 体检暂停/恢复 → 重新分配额度。后台线程执行，不阻塞交易循环。"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last = (db.get_setting("rhythm_long_refreshed") or {}).get("date")
    if last == today or not get_config().get("enabled") or _daily_lock.locked():
        return False

    def work():
        with _daily_lock:
            try:
                refresh_long_all()
                from . import evolve
                evolve.health_check()
                evolve.rebalance()
            except Exception:  # noqa: BLE001
                log.exception("rhythm daily failed")
                db.set_setting("rhythm_long_refreshed", {"date": today, "at": db.now_iso(), "error": True})
    if background:
        threading.Thread(target=work, name="rhythm-daily", daemon=True).start()
    else:
        work()
    return True


def status_rows() -> list[dict]:
    from .paper import runner as pr
    rows = []
    now = pd.Timestamp.now(tz="UTC")
    for d in db.q("SELECT id FROM paper_deployments WHERE broker='alpaca_paper' AND is_test=0 "
                  "AND status IN ('running','paused_entries') ORDER BY id"):
        dep = pr.get_deployment(d["id"])
        s = db.get_setting(_key(d["id"])) or {}
        bu = pd.Timestamp(s["block_until"]) if s.get("block_until") else None
        lm = long_metrics(dep) or {}
        rows.append({"id": d["id"], "label": dep["config"].get("label") or f"#{d['id']}", "status": dep["status"],
                     "blocked": bool(bu is not None and bu > now),
                     "block_until": bu.isoformat() if bu is not None and bu > now else None,
                     "block_why": s.get("block_why") if bu is not None and bu > now else "",
                     "streak": int(s.get("streak") or 0), "long": lm,
                     "budget": dep["config"].get("budget_usd")})
    return rows

