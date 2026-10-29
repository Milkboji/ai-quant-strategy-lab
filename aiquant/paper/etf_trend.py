"""美股 ETF 趋势择时（Alpaca Paper）：QQQ 150 日均线 ±2% 择时 → 持有 QLD（2 倍纳指 ETF），否则持有现金。

研究依据（2026-10-21，10.8 年日线，次日开盘成交，单边 5bp 成本）：
- 挑选期 2016-2022：年化 +28.9%，最大回撤 -34.2%（一直持有 QLD：+21.0% / -64.3%）
- 验证期 2023-2024：年化 +46.3%，回撤 -29.7%
- 最终检验 2025-01~2026-10（只看一次）：累计 +34.6%，回撤 -22.4%（持有 QLD：+63.0% / -44.0%）
  牛市里跑不赢一直持有；价值在熊市（2018、2020、2022）大幅降低回撤。
杠杆来自 2 倍 ETF 本身，没有保证金借款和强平。

执行规则（与回测一致）：用已收盘的日线算信号（带 ±2% 缓冲防止来回切换），
美股开盘 5 分钟后提交当日市价单；每个交易日最多动作一次。只做多，不做空。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from .. import config, db
from ..data import alpaca, http

ET = ZoneInfo("America/New_York")
log = logging.getLogger("aiquant.etf")
DEFAULTS = {"enabled": True, "signal_symbol": "QQQ", "trade_symbol": "QLD", "sma": 150, "band": 0.02,
            "budget_pct": 30.0, "lend_pct": 50.0}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS etf_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, side TEXT, notional REAL, qty REAL,
  status TEXT, filled_qty REAL, filled_price REAL, client_order_id TEXT UNIQUE, signal_date TEXT, reason TEXT,
  raw_json TEXT);
"""


def ensure_tables() -> None:
    with db.tx() as c:
        c.executescript(SCHEMA_SQL)


def get_config() -> dict:
    d = dict(DEFAULTS)
    d.update(db.get_setting("etf_trend_config") or {})
    return d


def lend_state() -> dict:
    """美股空仓时临时借给 BTC 的额度：{"state": none | lent | reclaim, "since", "lend_pct"}。"""
    return db.get_setting("etf_lend") or {"state": "none"}


def reclaim_active() -> bool:
    return lend_state().get("state") == "reclaim"


def capital_share(equity: float | None) -> float:
    """美股实际占用的资金比例（%），BTC 策略按实力分配时先扣除它（第一层，见 allocation.layer1）：
    max(美股目标 − 临时借出部分, 美股持仓市值占比)；没有运行中的美股部署时只算仍在持有的市值。"""
    from .. import allocation
    from . import us_exec
    if us_exec.etf_deployment() is None:     # 还没迁移（首次启动前）：原公式
        cfg = get_config()
        st = db.get_setting("etf_trend_state") or {}
        held = float((st.get("position") or {}).get("market_value") or 0)
        held_pct = held / equity * 100 if equity and equity > 0 else 0.0
        target = float(cfg["budget_pct"]) if cfg.get("enabled") and not cfg.get("pending_offline") else 0.0
        ls = lend_state()
        if ls.get("state") == "lent":
            target *= 1 - float(ls.get("lend_pct", cfg.get("lend_pct", 50.0))) / 100
        return round(max(target, held_pct), 4)
    return round(allocation.layer1(equity)["us_deduct_pct"], 4)


RECLAIM_DEADLINE_MIN = 10 * 60 + 30     # 美东 10:30 前等 BTC 还款；过了就按可用现金买入（缩量并通知）


def _btc_budgets() -> dict[str, float]:
    rows = db.q("SELECT id, config_json FROM paper_deployments WHERE broker='alpaca_paper' AND is_test=0 "
                "AND status IN ('running','paused_entries')")
    return {str(r["id"]): float((json.loads(r["config_json"] or "{}")).get("budget_usd") or 0) for r in rows}


def _set_lend(state: str, why: str, cfg: dict, **extra) -> None:
    """切换借出状态：写审计、事件，并立即重新分配 BTC 额度。
    收回（reclaim）时记下每个 BTC 部署收回前后的额度：减仓比例 = 1 − 收回后额度 / 收回前额度（即借用部分占比），
    BTC 在下一个美股开盘前按这个比例卖出借用期间开的仓位（PaperRunner 执行，见 pending_cut）。"""
    from .. import console
    old = lend_state()
    if old.get("state") == state:
        return
    pct = float(cfg.get("lend_pct", 50.0))
    before = _btc_budgets() if state == "reclaim" else {}
    rec = {"state": state, "since": db.now_iso(), "lend_pct": pct, "why": why, **extra}
    if state == "reclaim":
        rec["lent_since"] = old.get("since")
    db.set_setting("etf_lend", rec)
    action = {"lent": "临时借出额度", "reclaim": "收回临时借出的额度", "none": "额度已归还"}[state]
    console.audit("系统", "system", action, "etf", "美股 ETF 择时", None, None, None, action, why)
    _log("INFO", f"{action}：{why}")
    try:
        from .. import evolve
        evolve.rebalance()
    except Exception:  # noqa: BLE001
        log.warning("rebalance after lend change failed", exc_info=True)
    if state == "reclaim":
        after = _btc_budgets()
        cuts = {}
        for did, b0 in before.items():
            b1 = after.get(did, b0)
            frac = max(0.0, min(1.0, 1 - b1 / b0)) if b0 > 0 else 0.0
            if frac > 0.001:
                cuts[did] = {"before": round(b0, 2), "after": round(b1, 2), "frac": round(frac, 4), "done": False}
        rec["cuts"] = cuts
        db.set_setting("etf_lend", rec)
        if cuts:
            _log("INFO", "BTC 按借用比例减仓（下一个美股开盘前）：" + "；".join(
                f"#{k} 额度 {v['before']:,.0f}→{v['after']:,.0f} USD，卖出借用期间仓位的 {v['frac']:.1%}" for k, v in cuts.items()))


def pending_cut(did: int) -> dict | None:
    """收回期间该 BTC 部署还没执行的「按借用比例减仓」：{"frac", "before", "after", "lent_since"}；没有则 None。"""
    ls = lend_state()
    if ls.get("state") != "reclaim":
        return None
    c = (ls.get("cuts") or {}).get(str(did))
    if not c or c.get("done"):
        return None
    return dict(c, lent_since=ls.get("lent_since"))


def mark_cut_done(did: int, sold_qty: float, note: str) -> None:
    """BTC 部署已按借用比例减仓（或无需减仓），写审计。"""
    from .. import console
    ls = lend_state()
    cuts = dict(ls.get("cuts") or {})
    c = dict(cuts.get(str(did)) or {})
    if not c or c.get("done"):
        return
    c.update(done=True, sold_qty=round(float(sold_qty), 8), note=note, at=db.now_iso())
    cuts[str(did)] = c
    db.set_setting("etf_lend", dict(ls, cuts=cuts))
    console.audit("系统", "system", "BTC 归还借用额度", "deployment", f"#{did}", None, None, None,
                  f"卖出 {sold_qty:.6f} BTC" if sold_qty > 0 else "无需卖出", note)


def reclaim_pending() -> list[str]:
    """还没完成减仓的 BTC 部署（收回期间）。"""
    ls = lend_state()
    if ls.get("state") != "reclaim":
        return []
    return [k for k, v in (ls.get("cuts") or {}).items() if not v.get("done")]


def _log(level: str, msg: str) -> None:
    from . import runner as pr
    pr.event(None, level, f"[ETF趋势] {msg}")


# ---------------------------------------------------------------------------
# 信号（纯函数，便于测试）
# ---------------------------------------------------------------------------

from ..engine.regime_bt import regime_series  # noqa: E402,F401  回测与模拟盘共用同一个信号函数


def daily_bars(symbol: str, days: int = 420) -> pd.DataFrame:
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    params = {"symbols": symbol, "timeframe": "1Day", "start": (end - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "adjustment": "all", "feed": "sip", "limit": 10000}
    r = http.request("GET", f"{config.ALPACA_DATA_URL}/v2/stocks/bars", headers=alpaca._data_headers(),
                     params=params, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"日线获取失败 {r.status_code}: {r.text[:120]}")
    rows = (r.json().get("bars") or {}).get(symbol, []) or []
    df = pd.DataFrame(rows).rename(columns={"t": "date", "c": "close"})
    df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_convert(ET).dt.date
    return df[["date", "close"]]


def update_daily_csv(symbols: list[str] | None = None, now: datetime | None = None) -> dict:
    """每个交易日更新一次本地日线库（全部 ETF，只写已收盘的日线；复权口径变化时整只重下）。"""
    from ..data import us_daily
    syms = list(dict.fromkeys(list(symbols or []) + us_daily.UNIVERSE))
    return us_daily.refresh(syms, now)


def compute_signal(df: pd.DataFrame, today, cfg: dict) -> dict:
    """只用 today 之前已收盘的日线。"""
    d = df[df["date"] < today].reset_index(drop=True)
    if len(d) < cfg["sma"] + 5:
        raise RuntimeError("日线数量不足以计算均线")
    st = regime_series(d["close"], int(cfg["sma"]), float(cfg["band"]))
    ma = d["close"].rolling(int(cfg["sma"])).mean()
    return {"date": str(d["date"].iloc[-1]), "close": float(d["close"].iloc[-1]), "sma": float(ma.iloc[-1]),
            "regime": int(st.iloc[-1]), "upper": float(ma.iloc[-1] * (1 + cfg["band"])),
            "lower": float(ma.iloc[-1] * (1 - cfg["band"]))}


# ---------------------------------------------------------------------------
# 券商交互（Alpaca Paper）
# ---------------------------------------------------------------------------

def _clock() -> dict | None:
    code, b = alpaca.paper_get("/v2/clock")
    return b if code == 200 and isinstance(b, dict) else None


def _position(sym: str) -> dict:
    """券商持仓。404 = 没有持仓；其他错误（网络、凭证）带 error=True，调用方不能当作空仓。"""
    code, b = alpaca.paper_get(f"/v2/positions/{sym}")
    if code == 200 and isinstance(b, dict):
        return {"qty": float(b.get("qty") or 0), "market_value": float(b.get("market_value") or 0),
                "avg_entry_price": float(b.get("avg_entry_price") or 0), "unrealized_pl": float(b.get("unrealized_pl") or 0)}
    out = {"qty": 0.0, "market_value": 0.0, "avg_entry_price": 0.0, "unrealized_pl": 0.0}
    if code != 404:
        out["error"] = True
    return out


def _account() -> dict | None:
    code, b = alpaca.paper_get("/v2/account")
    return b if code == 200 and isinstance(b, dict) else None


def _submit(sym: str, side: str, *, notional: float | None = None, qty: float | None = None,
            signal_date: str | None = None, reason: str | None = None, coid: str | None = None) -> dict:
    """向 Alpaca Paper 提交当日有效（DAY）市价单。迁移后由通用美股执行器调用（订单与分账记在 us_orders / us_order_allocs）。"""
    coid = coid or f"etf-{sym.lower()}-{side}-{signal_date}-{uuid.uuid4().hex[:6]}"
    body = {"symbol": sym, "side": side, "type": "market", "time_in_force": "day", "client_order_id": coid}
    if notional is not None:
        body["notional"] = f"{notional:.2f}"
    else:
        body["qty"] = f"{qty:.9f}".rstrip("0").rstrip(".")
    r = http.request("POST", f"{config.ALPACA_PAPER_URL}/v2/orders", headers=alpaca._paper_headers(), json_body=body)
    info = r.json() if r.content else {}
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Alpaca Paper 拒绝订单 {r.status_code}: {r.text[:160]}")
    return info


def sync_orders() -> None:
    for o in db.q("SELECT * FROM etf_orders WHERE status NOT IN ('filled','canceled','expired','rejected') "
                  "AND status NOT LIKE 'rejected_%'"):
        code, b = alpaca.paper_get("/v2/orders:by_client_order_id", {"client_order_id": o["client_order_id"]})
        if code == 200 and isinstance(b, dict):
            db.ex("UPDATE etf_orders SET status=?, filled_qty=?, filled_price=? WHERE id=?",
                  (b.get("status"), float(b.get("filled_qty") or 0), float(b.get("filled_avg_price") or 0) or None, o["id"]))
            if b.get("status") == "filled" and o["status"] != "filled":
                _log("INFO", f"{'买入' if o['side'] == 'buy' else '卖出'} {o['symbol']} 成交 {float(b.get('filled_qty') or 0):.4f} 股 "
                             f"@ {float(b.get('filled_avg_price') or 0):,.2f}")


# ---------------------------------------------------------------------------
# 每轮服务循环调用
# ---------------------------------------------------------------------------

WHY_CODES = ("safe_restart", "disabled", "locked", "no_clock", "closed", "pre_open", "data_error", "pending",
             "bought", "topup", "sold", "hold", "flat", "small", "rejected", "rhythm", "long_pause", "pending_offline", "reclaim")
OPEN_STATUSES = ("new", "accepted", "pending_new", "partially_filled")
FINAL_STATUSES = ("filled", "canceled", "expired", "rejected", "done_for_day")


def _bj(iso: str | None) -> str:
    if not iso:
        return "--"
    try:
        return (datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=8)))
                .strftime("%m-%d %H:%M"))
    except ValueError:
        return str(iso)


def set_why(code: str, text: str, **extra) -> dict:
    """记录「美股 ETF 现在为什么没有动作 / 做了什么」（结构化，供策略总台和面板显示）。内容不变时不重复写库。"""
    cur = db.get_setting("etf_trend_why") or {}
    if cur.get("code") == code and cur.get("text") == text:
        return cur
    w = {"code": code, "text": text, "at": db.now_iso(), **extra}
    db.set_setting("etf_trend_why", w)
    return w


def get_why() -> dict | None:
    return db.get_setting("etf_trend_why")


def entry_filled_usd(sym: str, since_signal_date: str) -> float:
    """本轮持仓（自 since_signal_date 那次买入起）已成交的买入金额。"""
    rows = db.q("SELECT filled_qty, filled_price FROM etf_orders WHERE symbol=? AND side='buy' AND signal_date>=?",
                (sym, since_signal_date))
    return float(sum((r["filled_qty"] or 0) * (r["filled_price"] or 0) for r in rows))


def topup_needed(target: float, filled: float, min_usd: float = 10.0) -> float:
    """部分成交后需要补买的金额；差额小于 max(10 USD, 目标的 2%) 时不补。"""
    rem = float(target) - float(filled)
    return rem if rem >= max(min_usd, 0.02 * float(target)) else 0.0


def _reject(st: dict, today, sym: str, side: str, err: Exception, sig: dict) -> dict:
    """下单被拒：当天不再重试（避免每轮循环都提交一笔会被拒的订单），记录原因并通知。"""
    msg = str(err)[:200]
    st = dict(st, last_action_date=str(today), action=f"下单被拒（{'买入' if side == 'buy' else '卖出'} {sym}）：{msg}",
              signal=sig, checked_at=db.now_iso())
    db.set_setting("etf_trend_state", st)
    set_why("rejected", f"今天的{'买入' if side == 'buy' else '卖出'}订单被券商拒绝：{msg}。今天不再重试，下一个交易日重新判断")
    _log("WARN", f"{'买入' if side == 'buy' else '卖出'} {sym} 被拒绝：{msg}（今天不再重试）")
    try:
        from .. import notify
        notify.event("order_failed", f"美股 ETF 下单被拒：{sym}", f"{'买入' if side == 'buy' else '卖出'} {sym} 被 Alpaca Paper 拒绝：{msg}\n"
                     "今天不再重试，下一个交易日按信号重新判断。", dedupe=f"etf-reject-{today}-{side}")
    except Exception:  # noqa: BLE001
        pass
    return st


def _buy(sym: str, notional: float, sig: dict, why: str) -> str:
    try:
        _submit(sym, "buy", notional=notional, signal_date=sig["date"], reason=why)
        return f"买入 {sym} {notional:,.0f} USD"
    except RuntimeError as e:   # 不支持碎股时改为整股
        if "fraction" not in str(e).lower():
            raise
        px = float(daily_bars(sym, 10)["close"].iloc[-1])
        q = int(notional // px)
        if q < 1:
            return ""
        _submit(sym, "buy", qty=q, signal_date=sig["date"], reason=why)
        return f"买入 {sym} {q} 股"


def _trade_daily(sym: str) -> pd.DataFrame:
    """交易品种已收盘日线（ts = 当天美东收盘时刻），供波动率定仓使用。"""
    from .. import market as mk
    from ..data import us_daily
    d = us_daily.load(sym)
    if d is None or not len(d):
        return pd.DataFrame(columns=["ts", "close"])
    days = us_daily.et_date(d["date"])
    return pd.DataFrame({"ts": pd.to_datetime([mk.session_close(x) for x in days], utc=True), "close": d["close"].values})


def _rhythm_gate(sym: str, now: datetime) -> tuple[str | None, float]:
    """开仓前的统一纪律（与回测 regime_bt 相同的规则）：冷却 / 连亏刹车、波动率定仓。
    美股长周期体检仅作提示：不达标时只在「为什么」和日志里写明，不拦截（2026-10-29 用户决定）。
    美股影子模式：照常计算，但不阻止、不缩仓，只把「如果生效会怎样」记在 etf_rhythm_shadow 里显示。"""
    from .. import market as mk
    from .. import rhythm
    shadow = rhythm.is_shadow(mk.US_EQUITY)
    block, vm = None, 1.0
    hint = None
    lh = rhythm.etf_long_hint() if rhythm.us_long_hint_only() else None
    if lh:
        hint = (f"{rhythm.US_LONG_HINT_TEXT}：不达标（年均 {lh.get('mean_ret', 0):+.1f}%，盈利 {lh.get('pos_years', 0)}/"
                f"{lh.get('n_years', 0)} 年），不拦截")
    if rhythm.etf_long_paused(include_shadow=True):
        m = rhythm.etf_long_metrics() or {}
        block = (f"长周期体检不达标（年均 {m.get('mean_ret', 0):+.1f}%，盈利 {m.get('pos_years', 0)}/{m.get('n_years', 0)} 年），"
                 "暂停新开仓")
    else:
        rh = rhythm.configured(mk.US_EQUITY)
        if rh is not None:
            try:
                df = _trade_daily(sym) if (rh.vol_target_pct or rh.regime_sma_days) else pd.DataFrame(columns=["ts", "close"])
                b, vm, _ = rhythm.live_gate(rhythm.ETF_KEY, now, df, mk.US_EQUITY)
            except Exception as e:  # noqa: BLE001
                if not shadow:
                    raise
                rhythm.shadow_error(str(now.astimezone(ET).date()), f"开仓判断异常：{type(e).__name__}: {e}")
                b, vm = None, 1.0
            block = f"交易节奏：{b}" if b else None
    if hint:
        _log("INFO", hint)
    if shadow:
        note = (f"影子模式：如果美股节奏生效，{block}" if block else
                (f"影子模式：如果美股节奏生效，仓位会缩到 {vm:.0%}" if vm < 1 else "影子模式：节奏规则不会阻止这次买入"))
        db.set_setting("etf_rhythm_shadow", {"at": db.now_iso(), "block": block, "vol_mult": vm, "note": note, "hint": hint})
        return None, 1.0
    return block, vm


def _rhythm_entry(now: datetime) -> None:
    from .. import market as mk
    from .. import rhythm
    rhythm.live_on_entry(rhythm.ETF_KEY, now, mk.US_EQUITY)


def _rhythm_exit(now: datetime, pnl: float) -> None:
    from .. import market as mk
    from .. import rhythm
    try:
        msg = rhythm.live_on_exit(rhythm.ETF_KEY, now, "信号离场", pnl, mk.US_EQUITY)
    except Exception as e:  # noqa: BLE001
        if not rhythm.is_shadow(mk.US_EQUITY):
            raise
        rhythm.shadow_error(str(now.astimezone(ET).date()), f"离场记账异常：{type(e).__name__}: {e}")
        return
    if msg and rhythm.is_shadow(mk.US_EQUITY):
        _log("INFO", f"影子模式（不影响下单）：如果美股节奏生效，{msg}")
        return
    if msg:
        _log("WARN", f"交易节奏：{msg}")
        if "连亏" in msg:
            try:
                from .. import notify
                notify.event("loss_streak", "美股 ETF 择时：连亏刹车", msg, dedupe=f"etf-streak-{now:%Y-%m-%d}")
            except Exception:  # noqa: BLE001
                pass


def step(now: datetime | None = None) -> dict:
    """美股 ETF 择时已迁移为通用美股日线执行器上的一个普通部署（us_exec，kind=etf_sma）：
    规则不变（已收盘日线出信号、美东 9:35 后 DAY 单、被拒不重试、部分成交次日补足、写「为什么」），这里只转调。"""
    from . import us_exec
    res = us_exec.step(now)
    if isinstance(res, dict) and res.get("skipped"):
        return res
    return db.get_setting("etf_trend_state") or {}


def _btc_price() -> float | None:
    """最新 BTC 价格（估算 BTC 未成交买单占用的现金）；取不到时返回 None。"""
    p = (db.get_setting("btc_last_price") or {}).get("price")
    if p:
        return float(p)
    r = db.q1("SELECT filled_price FROM paper_orders WHERE filled_price IS NOT NULL ORDER BY id DESC LIMIT 1")
    return float(r["filled_price"]) if r and r["filled_price"] else None


def after_close(now: datetime | None = None) -> dict | None:
    """收盘后确认下一交易日信号（借出 → 收回；偏离目标 → 下一交易日再平衡），见 us_exec.after_close。"""
    from . import us_exec
    return us_exec.after_close(now)


def orders(limit: int = 30) -> list[dict]:
    """美股 ETF 择时的订单：迁移后来自通用执行器（每个策略分到的份额），迁移前的旧订单仍在 etf_orders。"""
    from . import us_exec, us_ledger
    ensure_tables()
    out = []
    dep = us_exec.etf_deployment()
    if dep:
        for a in us_ledger.allocs_for(dep["id"], limit):
            out.append({"ts": a["created_at"], "symbol": a["symbol"], "side": a["side"], "notional": a["req_notional"],
                        "qty": a["req_qty"], "status": a["order_status"] or a["status"], "filled_qty": a["filled_qty"],
                        "filled_price": a["filled_price"], "client_order_id": a["client_order_id"],
                        "signal_date": a["trade_date"], "reason": a["reason"]})
    out += db.q("SELECT * FROM etf_orders ORDER BY id DESC LIMIT ?", (limit,))
    return out[:limit]
