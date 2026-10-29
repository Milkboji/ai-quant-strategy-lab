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
    """美股 ETF 实际占用的资金比例（%），BTC 策略按实力分配时先扣除它：
    运行中取「设定比例」和「当前持仓市值 / 权益」的较大者（ETF 涨多了也不会让两边合计超过账户）；
    空仓且已临时借出时，只扣除未借出的部分（BTC 最多临时使用这部分额度的 lend_pct%）；
    下线 / 等待下线时只扣除仍在持有的市值。"""
    cfg = get_config()
    st = db.get_setting("etf_trend_state") or {}
    held = float((st.get("position") or {}).get("market_value") or 0)
    held_pct = held / equity * 100 if equity and equity > 0 else 0.0
    target = float(cfg["budget_pct"]) if cfg.get("enabled") and not cfg.get("pending_offline") else 0.0
    ls = lend_state()
    if ls.get("state") == "lent":
        target *= 1 - float(ls.get("lend_pct", cfg.get("lend_pct", 50.0))) / 100
    return round(max(target, held_pct), 4)


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
    code, b = alpaca.paper_get(f"/v2/positions/{sym}")
    if code == 200 and isinstance(b, dict):
        return {"qty": float(b.get("qty") or 0), "market_value": float(b.get("market_value") or 0),
                "avg_entry_price": float(b.get("avg_entry_price") or 0), "unrealized_pl": float(b.get("unrealized_pl") or 0)}
    return {"qty": 0.0, "market_value": 0.0, "avg_entry_price": 0.0, "unrealized_pl": 0.0}


def _account() -> dict | None:
    code, b = alpaca.paper_get("/v2/account")
    return b if code == 200 and isinstance(b, dict) else None


def _submit(sym: str, side: str, *, notional: float | None = None, qty: float | None = None,
            signal_date: str, reason: str) -> dict:
    coid = f"etf-{sym.lower()}-{side}-{signal_date}-{uuid.uuid4().hex[:6]}"
    body = {"symbol": sym, "side": side, "type": "market", "time_in_force": "day", "client_order_id": coid}
    if notional is not None:
        body["notional"] = f"{notional:.2f}"
    else:
        body["qty"] = f"{qty:.9f}".rstrip("0").rstrip(".")
    db.ex("INSERT INTO etf_orders(ts,symbol,side,notional,qty,status,client_order_id,signal_date,reason) "
          "VALUES(?,?,?,?,?,?,?,?,?)", (db.now_iso(), sym, side, notional, qty, "new", coid, signal_date, reason))
    r = http.request("POST", f"{config.ALPACA_PAPER_URL}/v2/orders", headers=alpaca._paper_headers(), json_body=body)
    info = r.json() if r.content else {}
    status = info.get("status") if r.status_code in (200, 201) else f"rejected_{r.status_code}"
    db.ex("UPDATE etf_orders SET status=?, raw_json=? WHERE client_order_id=?",
          (status, json.dumps(info, ensure_ascii=False)[:2000], coid))
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
    try:   # 安全重启进行中：不下新单
        from .. import safe_restart
        if safe_restart.active():
            set_why("safe_restart", "安全重启进行中，暂停下新单")
            return {"skipped": "安全重启进行中，暂停下新单"}
    except Exception:  # noqa: BLE001
        pass
    cfg = get_config()
    if not cfg["enabled"]:
        set_why("disabled", "美股 ETF 择时已下线（未启用）")
        return {}
    if not http.paper_orders_unlocked():
        set_why("locked", "模拟下单锁未打开（双重锁），只计算信号不下单")
        return {}
    ensure_tables()
    sync_orders()
    now = now or datetime.now(timezone.utc)
    st = db.get_setting("etf_trend_state") or {}
    if not db.get_setting("etf_trend_rebalanced"):   # 首次启用：把 ETF 资金比例从 BTC 策略额度中划出
        try:
            from .. import evolve
            evolve.rebalance()
            db.set_setting("etf_trend_rebalanced", db.now_iso())
        except Exception:  # noqa: BLE001
            pass
    clk = _clock()
    if not clk:
        set_why("no_clock", "暂时连不上券商时钟，稍后重试")
        return st
    if not clk.get("is_open"):
        set_why("closed", f"美股休市中，下次开盘 {_bj(clk.get('next_open'))}（北京时间）", next_open=clk.get("next_open"))
        return st
    now_et = now.astimezone(ET)
    today = now_et.date()
    if st.get("last_action_date") == str(today):
        return st
    if now_et.hour * 60 + now_et.minute < 9 * 60 + 35:
        set_why("pre_open", "已开盘，等到美东 9:35（开盘 5 分钟后）再按前一日收盘信号决定")
        return st
    sym = cfg["trade_symbol"]
    try:
        update_daily_csv([cfg["signal_symbol"], sym], now)
    except Exception:  # noqa: BLE001
        log.warning("etf daily csv refresh failed", exc_info=True)
    try:
        sig = compute_signal(daily_bars(cfg["signal_symbol"]), today, cfg)
    except Exception as e:  # noqa: BLE001
        set_why("data_error", f"日线数据暂时取不到，稍后重试：{str(e)[:120]}")
        return st
    pos = _position(sym)
    try:
        from . import etf_versions
        if etf_versions.finish_pending_offline(float(pos["qty"] or 0)):
            set_why("disabled", "已空仓，按计划下线")
            return st
    except Exception:  # noqa: BLE001
        log.warning("pending offline check failed", exc_info=True)
    try:   # 美股影子模式：每个交易日记录一次「如果生效会怎样」并自检
        from .. import rhythm
        rhythm.shadow_record(str(today), now)
    except Exception:  # noqa: BLE001
        log.warning("shadow record failed", exc_info=True)
    acct = _account() or {}
    equity = float(acct.get("equity") or 0)
    budget = equity * float(cfg["budget_pct"]) / 100
    cash = float(acct.get("cash") or 0)
    pending = db.q1("SELECT id FROM etf_orders WHERE symbol=? AND status IN ('new','accepted','pending_new',"
                    "'partially_filled')", (sym,))
    in_band = sig["lower"] <= sig["close"] <= sig["upper"]
    band_txt = (f"（收盘 {sig['close']:.2f} 在均线缓冲带 {sig['lower']:.2f}~{sig['upper']:.2f} 内，维持原状态）"
                if in_band else "")
    entry = st.get("entry") if isinstance(st.get("entry"), dict) else None
    # 美股空仓时把一部分额度临时借给 BTC；买入信号（收盘后确认，见 after_close）时收回：
    # BTC 在下一个美股开盘前按借用比例减仓并暂停新开仓；开盘后 ETF 先等还款、再买入（ETF 买入优先）
    flat = pos["qty"] <= 0 and not pending
    ls = lend_state().get("state", "none")
    lend_pct = float(cfg.get("lend_pct", 50.0))
    if not flat and ls == "lent":
        _set_lend("none", f"已持有 {sym}，借出状态结束", cfg)
        ls = "none"
    elif flat and sig["regime"] == 0 and ls == "none" and lend_pct > 0 and not cfg.get("pending_offline"):
        _set_lend("lent", f"美股空仓（看空），BTC 最多临时使用美股额度的 {lend_pct:g}%", cfg)
        ls = "lent"
    elif flat and sig["regime"] == 1 and ls == "lent":
        _set_lend("reclaim", f"{cfg['signal_symbol']} 发出买入信号（开盘检查时确认，收盘后检查没有运行），收回借给 BTC 的额度：BTC 按"
                             "借用比例减仓并暂停新开仓", cfg, signal_date=sig["date"])
        ls = "reclaim"
    if ls == "reclaim" and sig["regime"] == 0:
        _set_lend("none", "收回结束（信号转空）", cfg)
        ls = "none"
    if ls == "reclaim" and flat and sig["regime"] == 1:
        waiting = reclaim_pending()
        mins = now_et.hour * 60 + now_et.minute
        if waiting and mins < RECLAIM_DEADLINE_MIN:
            set_why("reclaim", f"看多，先等 BTC 按借用比例减仓、归还额度（还剩部署 {', '.join('#' + w for w in waiting)}；"
                               f"BTC 已暂停新开仓）。现金 {cash:,.0f} / 需要 {budget:,.0f} USD；最晚美东 10:30 按可用现金买入")
            return st       # 不记 last_action_date：还款完成后当天就买
    reserved = 0.0
    try:
        from . import cash_guard
        reserved = cash_guard.reserved_cash(_btc_price(), exclude="etf")
    except Exception:  # noqa: BLE001
        log.warning("reserved cash failed", exc_info=True)
    action = "保持"
    side = None
    try:
        if pending:
            action = "等待未完成订单"
            set_why("pending", "上一笔订单还没完成，等它成交或过期后再动作")
        elif sig["regime"] == 1 and pos["qty"] <= 0 and cfg.get("pending_offline"):
            set_why("pending_offline", "等待下线：不再开新仓")
        elif sig["regime"] == 1 and pos["qty"] <= 0:
            block, vm = _rhythm_gate(sym, now)
            if block:
                set_why("long_pause" if block.startswith("长周期体检") else "rhythm", f"看多，但{block}，暂不买入")
            else:
                from . import cash_guard
                target = budget * vm
                notional, short = cash_guard.check_buy(target, cash, who="美股 ETF 择时", symbol=sym, reserved=reserved,
                                                       dedupe=f"cash-etf-{today}")
                if notional >= cash_guard.MIN_ORDER_USD:
                    side = "buy"
                    why = f"{cfg['signal_symbol']} 收盘 {sig['close']:.2f} 高于 {cfg['sma']} 日均线上沿 {sig['upper']:.2f}"
                    if vm < 1:
                        why += f"；波动率定仓 {vm:.0%}"
                    if short:
                        why += f"；{short}"
                    action = _buy(sym, notional, sig, why) or "保持"
                    if action != "保持":
                        entry = {"signal_date": sig["date"], "target": round(target, 2)}
                        set_why("bought", f"看多：{why}，已下单 {action}" + ("；差额下一个交易日补足" if short else ""))
                        if short:
                            _log("WARN", short)
                        _rhythm_entry(now)
                        if lend_state().get("state") != "none" and not short:
                            _set_lend("none", f"已买入 {sym}，额度归还", cfg)
                else:
                    set_why("small", (short or f"看多，但可用资金不足 10 USD（现金 {cash:,.2f}）") + "，暂不买入")
        elif sig["regime"] == 1 and pos["qty"] > 0:
            from . import cash_guard
            need = 0.0
            if entry and not cfg.get("pending_offline"):
                need = topup_needed(entry["target"], entry_filled_usd(sym, entry["signal_date"]))
            rem, short = cash_guard.check_buy(need, cash, who="美股 ETF 择时（补足）", symbol=sym, reserved=reserved,
                                              dedupe=f"cash-etf-topup-{today}") if need else (0.0, None)
            if rem >= cash_guard.MIN_ORDER_USD:
                side = "buy"
                action = _buy(sym, rem, sig, f"上次买入只部分成交，补足到目标 {entry['target']:,.0f} USD"
                              + (f"；{short}" if short else "")) or "保持"
                if action != "保持":
                    action = "补足 " + action
                    set_why("topup", f"上次买入只部分成交，今天补买 {rem:,.0f} USD" + (f"（{short}）" if short else ""))
                    if lend_state().get("state") == "reclaim" and not short:
                        _set_lend("none", f"已补足 {sym}，额度归还", cfg)
            else:
                if lend_state().get("state") == "reclaim" and not need:
                    _set_lend("none", f"已持有 {sym} 且达到目标，额度归还", cfg)
                set_why("hold", f"看多且已持有 {sym}，保持不动{band_txt}" + (f"；{short}" if short else ""))
        elif sig["regime"] == 0 and pos["qty"] > 0:
            side = "sell"
            _submit(sym, "sell", qty=pos["qty"], signal_date=sig["date"],
                    reason=f"{cfg['signal_symbol']} 收盘 {sig['close']:.2f} 低于 {cfg['sma']} 日均线下沿 {sig['lower']:.2f}")
            action = f"卖出全部 {sym}"
            entry = None
            _rhythm_exit(now, float(pos.get("unrealized_pl") or 0))
            set_why("sold", f"看空：{cfg['signal_symbol']} 跌破 {cfg['sma']} 日均线下沿 {sig['lower']:.2f}，已卖出全部 {sym}")
        else:
            set_why("flat", f"看空，保持现金，不持有 {sym}{band_txt}")
    except RuntimeError as e:
        return _reject(st, today, sym, side or "buy", e, sig)
    st = {"last_action_date": str(today), "signal": sig, "action": action, "position": pos,
          "budget": budget, "checked_at": db.now_iso(), "entry": entry}
    db.set_setting("etf_trend_state", st)
    _log("INFO", f"{sig['date']} 收盘 {cfg['signal_symbol']} {sig['close']:.2f}，{cfg['sma']} 日均线 {sig['sma']:.2f}"
                 f"（上沿 {sig['upper']:.2f} / 下沿 {sig['lower']:.2f}）→ {'看多，持有' if sig['regime'] else '看空，空仓'} {sym}；动作：{action}")
    return st


def _btc_price() -> float | None:
    """最新 BTC 价格（估算 BTC 未成交买单占用的现金）；取不到时返回 None。"""
    p = (db.get_setting("btc_last_price") or {}).get("price")
    if p:
        return float(p)
    r = db.q1("SELECT filled_price FROM paper_orders WHERE filled_price IS NOT NULL ORDER BY id DESC LIMIT 1")
    return float(r["filled_price"]) if r and r["filled_price"] else None


def after_close(now: datetime | None = None) -> dict | None:
    """美股收盘后（含周末、节假日）检查：用刚收盘的日线确认下一交易日的信号。
    美股空仓、额度已借给 BTC、且确认为买入信号 → 立即进入「收回」：BTC 在下一个美股开盘前按借用比例减仓、暂停新开仓。
    每个收盘日只确认一次（日线还没出来时稍后重试）。"""
    from .. import market as mk
    cfg = get_config()
    if not cfg["enabled"] or lend_state().get("state") != "lent":
        return None
    now = now or datetime.now(timezone.utc)
    now_et = now.astimezone(ET)
    d = now_et.date()
    last = d if (mk.is_trading_day(d) and now >= mk.session_close(d) + timedelta(minutes=15)) else None
    if last is None:
        cur = d
        for _ in range(10):
            cur -= timedelta(days=1)
            if mk.is_trading_day(cur):
                last = cur
                break
        if mk.is_trading_day(d) and now >= mk.session_open(d):
            return None          # 今天的交易时段内（含收盘后 15 分钟）：交给开盘检查
    if last is None or (db.get_setting("etf_after_close") or {}).get("date") == str(last):
        return None
    nxt = mk.trading_days_after(last, 1)[0]
    try:
        sig = compute_signal(daily_bars(cfg["signal_symbol"]), nxt, cfg)
    except Exception as e:  # noqa: BLE001
        log.info("after-close signal not ready: %s", e)
        return None
    if sig["date"] != str(last):
        return None              # 收盘日线还没出来
    pos = _position(cfg["trade_symbol"])
    out = {"date": str(last), "next": str(nxt), "regime": sig["regime"], "at": db.now_iso()}
    db.set_setting("etf_after_close", out)
    if sig["regime"] == 1 and float(pos.get("qty") or 0) <= 0:
        _set_lend("reclaim", f"{last} 收盘后确认 {cfg['signal_symbol']} 买入信号（收盘 {sig['close']:.2f} 高于均线上沿 "
                             f"{sig['upper']:.2f}），收回借给 BTC 的额度：BTC 在 {nxt} 美股开盘前按借用比例减仓、暂停新开仓",
                  cfg, signal_date=sig["date"])
        set_why("reclaim", f"{last} 收盘后确认买入信号：BTC 在 {nxt} 美股开盘前按借用比例减仓，开盘后先还款再买入 {cfg['trade_symbol']}")
    return out


def orders(limit: int = 30) -> list[dict]:
    ensure_tables()
    return db.q("SELECT * FROM etf_orders ORDER BY id DESC LIMIT ?", (limit,))
