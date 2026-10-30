"""「我的交易系统 v1」模拟盘执行器：跟着回测引擎走（同一个 engine.my_system.run）。

每 5 分钟（加密货币全天；美股只在交易时段内，开盘 5 分钟后开始）：
1. 同步未完成订单（通用执行器的 us_orders → us_ledger 按策略分账）；有未完成订单就等。
2. 读日线：已走完的日线 + 今天到现在的部分日线（开盘价 = 今天第一次看到的价格，最高 / 最低 = 到现在为止看到的价格）。
3. 从部署开始运行的时刻重放引擎（live=True）：昨天收盘决定的订单在今天开盘成交，盘中按今天的最高 / 最低检查止损和目标。
4. 和上一次的引擎持仓比较，按比例照做：引擎某品种加仓 → 买入「引擎买入金额 × 当前额度 / 引擎资金」；
   引擎减仓 → 卖出同样比例的实际持仓（引擎清仓 → 全部卖出）。额度变化只影响之后的买入，不强制卖出。
所有对外请求走唯一出口；下单受模拟盘双重锁约束（锁没开时只记录「为什么没有动作」）。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, time, timedelta, timezone

import pandas as pd

from .. import db
from ..data import alpaca, http
from . import etf_trend, us_ledger

log = logging.getLogger("aiquant.my_system_exec")
EVERY_MIN = 5
ENGINE_CASH = 10_000.0
MIN_USD = 10.0
CRYPTO_FEE = 0.0025     # Alpaca 加密货币买入手续费从所得币里扣（与 BTC 执行器相同的处理）：台账按到手数量记


def _sys():
    from .. import my_system
    return my_system


def deployments() -> list[dict]:
    return _sys().deployments()


def is_flat(did: int) -> bool:
    pos = us_ledger.position(did)
    return all(abs(p["qty"]) <= us_ledger.QTY_EPS * 10 for p in pos.values()) and not us_ledger.has_open(did)


def _log(dep: dict, level: str, msg: str) -> None:
    from . import runner as pr
    pr.event(dep["id"], level, f"[我的交易系统] {msg}")


def _why(dep: dict, text: str, **extra) -> None:
    key = f"ms_why::{dep['id']}"
    cur = db.get_setting(key) or {}
    if cur.get("text") == text:
        return
    db.set_setting(key, {"text": text, "at": db.now_iso(), **extra})


def get_why(did: int) -> dict | None:
    return db.get_setting(f"ms_why::{did}")


def _save_cfg(dep: dict) -> None:
    db.ex("UPDATE paper_deployments SET config_json=?, updated_at=? WHERE id=?",
          (json.dumps(dep["config"], ensure_ascii=False, default=str), db.now_iso(), dep["id"]))


def _session(mkey: str, now: datetime) -> tuple[bool, str, pd.Timestamp]:
    """(现在能不能交易, 原因, 今天这根日线的开始时间 UTC)。"""
    from .. import market as mk
    if mkey == "crypto":
        return True, "", pd.Timestamp(now).tz_convert("UTC").normalize()
    d = now.astimezone(mk.ET).date()
    if not mk.is_trading_day(d):
        return False, "美股休市", None
    op, cl = mk.session_open(d), mk.session_close(d)
    if now < op + timedelta(minutes=5) or now >= cl:
        return False, "不在交易时段（开盘 5 分钟后到收盘）", pd.Timestamp(op)
    return True, "", pd.Timestamp(op)


def _latest(mkey: str, sym: str) -> float | None:
    if mkey == "crypto":
        q = alpaca.latest_quote(sym)
        if not q or not q.get("bid") or not q.get("ask"):
            return None
        return (float(q["bid"]) + float(q["ask"])) / 2
    return etf_trend._latest_price(sym)


def _today_bar(dep: dict, mkey: str, sym: str, day_start: pd.Timestamp, px: float) -> dict:
    """今天到现在为止的部分日线（存在部署配置里，重启不丢）。"""
    st = dep["config"].setdefault("today", {})
    k = day_start.isoformat()
    if st.get("day") != k:
        st.clear()
        st["day"] = k
    b = st.get(sym) or {"open": px, "high": px, "low": px}
    b["high"], b["low"], b["close"] = max(b["high"], px), min(b["low"], px), px
    st[sym] = b
    return b


def engine_state(dep: dict, now: datetime, prices: dict[str, float] | None = None, fetch: bool = True) -> dict:
    """重放引擎（live=True）。prices：今天的最新价（None = 只用已走完的日线，用于页面显示）。"""
    from .. import my_system as S
    from .. import research, rhythm
    from ..engine import my_system as ms
    cfg = dep["config"]
    mkey = cfg["market"]
    m = S.market_obj(mkey)
    v = S.get_version(dep["version_id"])
    p = ms.Params.from_dict(v["params"])
    ok, why, day_start = _session(mkey, now)
    bars = {}
    for sym in cfg.get("symbols") or S.universe(mkey):
        try:
            d = S.daily(mkey, sym, now, fetch=fetch)
        except Exception:  # noqa: BLE001
            log.warning("daily bars failed for %s", sym, exc_info=True)
            continue
        if len(d) < 250:
            continue
        if prices and sym in prices and day_start is not None and (not len(d) or d["ts"].iloc[-1] < day_start):
            b = _today_bar(dep, mkey, sym, day_start, prices[sym])
            d = pd.concat([d, pd.DataFrame([{"ts": day_start, "open": b["open"], "high": b["high"], "low": b["low"],
                                               "close": b["close"], "volume": float("nan")}])], ignore_index=True)
        bars[sym] = d
    bench = bars.get(S.BENCH[mkey])
    if bench is None:
        bench = S.daily(mkey, S.BENCH[mkey], now, fetch=fetch)
    bench = bench[bench["ts"] < (day_start if day_start is not None else pd.Timestamp(now))]
    base = research.base_cost_model(research.get_research(), m) if mkey == "crypto" else m.cost()
    start = pd.Timestamp(cfg["live_since"]).tz_convert("UTC").normalize()
    if mkey == "crypto":
        start = start + pd.Timedelta(days=1)             # 部署之后的第一根完整日线起
    end = (day_start + pd.Timedelta(days=1)) if (prices and day_start is not None) else pd.Timestamp(now)
    return ms.run(bars, bench, p, cost=base, start=start, end=end, initial_cash=ENGINE_CASH, market_key=mkey,
                  rhythm=rhythm.current(m), market=m, live=True)


def _budget(dep: dict, equity: float) -> float:
    from .. import allocation
    if allocation.is_trial(dep):
        tp = allocation.trial_plan(equity).get(dep["id"])
        b = float(tp["budget"]) if tp else 0.0
    else:
        b = float(dep["config"].get("budget_usd") or 0)        # 转正后保持最后一次的额度（按实力分配待接入）
    if abs(b - float(dep["config"].get("budget_usd") or 0)) > 0.5:
        dep["config"]["budget_usd"] = round(b, 2)
    return b


def _fee_adjust(dep: dict) -> None:
    """加密货币买入的手续费从所得币里扣：分账后给每笔买入补一行「手续费」卖出，台账 = 实际到手（每笔只处理一次）。"""
    done = set(dep["config"].get("fee_done") or [])
    rows = db.q("SELECT id, symbol, qty, price, alloc_id FROM us_ledger WHERE deployment_id=? AND side='buy' "
                "AND symbol LIKE '%/%' AND source<>'fee'", (dep["id"],))
    new = [r for r in rows if r["id"] not in done]
    for r in new:
        db.ex("INSERT INTO us_ledger(deployment_id,symbol,ts,side,qty,price,source,order_id,alloc_id,note) "
              "VALUES(?,?,?,?,?,?,?,?,?,?)", (dep["id"], r["symbol"], db.now_iso(), "sell", float(r["qty"]) * CRYPTO_FEE,
                                              float(r["price"]), "fee", None, r["alloc_id"], "加密货币买入手续费（从币里扣）"))
        done.add(r["id"])
    if new:
        dep["config"]["fee_done"] = sorted(done)[-500:]


def step(now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    out = []
    for dep in deployments():
        try:
            r = _step_one(dep, now)
            if r:
                out.append(f"#{dep['id']} {r}")
        except Exception as e:  # noqa: BLE001
            log.exception("my_system step failed for #%s", dep["id"])
            _why(dep, f"检查出错：{type(e).__name__}: {str(e)[:120]}（下一轮重试，不下单）")
    return out


def _step_one(dep: dict, now: datetime) -> str | None:
    from . import cash_guard, us_exec
    cfg = dep["config"]
    mkey = cfg["market"]
    last = db.get_setting(f"ms_last::{dep['id']}")
    if last and (now - datetime.fromisoformat(last)).total_seconds() < EVERY_MIN * 60 - 5:
        return None
    ok, why, day_start = _session(mkey, now)
    if not ok:
        _why(dep, f"{why}，等待下一个交易时段")
        return None
    db.set_setting(f"ms_last::{dep['id']}", now.isoformat())
    if not http.paper_orders_unlocked():
        _why(dep, "模拟盘下单锁未开启：只计算，不下单")
    us_exec.sync_orders()
    _fee_adjust(dep)
    if us_ledger.has_open(dep["id"]):
        _why(dep, "有未完成的订单，等成交后再检查")
        _save_cfg(dep)
        return None
    # 待切换 / 待下线
    if cfg.get("pending_switch") and is_flat(dep["id"]):
        vid = cfg.pop("pending_switch")["version_id"]
        cfg["live_since"], dep["version_id"] = now.isoformat(), vid
        cfg.pop("engine_state", None)
        db.ex("UPDATE paper_deployments SET version_id=? WHERE id=?", (vid, dep["id"]))
        _log(dep, "INFO", f"空仓，切换到版本 {vid}")
    if cfg.get("pending_offline") and is_flat(dep["id"]):
        from . import runner as pr
        _save_cfg(dep)
        pr.set_status(dep["id"], "stopped")
        _log(dep, "INFO", "空仓，已下线")
        return "已下线"
    syms = cfg.get("symbols") or _sys().universe(mkey)
    prices = {}
    for s in syms:
        try:
            px = _latest(mkey, s)
        except Exception:  # noqa: BLE001
            px = None
        if px:
            prices[s] = px
    res = engine_state(dep, now, prices)
    eng = {s: float(v["qty"]) for s, v in (res.get("state") or {}).items()}
    prev = {s: float(q) for s, q in (cfg.get("engine_state") or {}).items()}
    cfg["engine_view"] = {s: {"qty": v["qty"], "tranches": v["tranches"], "half_sold": v["half_sold"]}
                          for s, v in (res.get("state") or {}).items()}
    acct = etf_trend._account() or {}
    equity, cash = float(acct.get("equity") or 0), float(acct.get("cash") or 0)
    budget = _budget(dep, equity)
    scale = budget / ENGINE_CASH if ENGINE_CASH else 0.0
    acts = []
    for s in sorted(set(eng) | set(prev)):
        e_new, e_old = eng.get(s, 0.0), prev.get(s, 0.0)
        if abs(e_new - e_old) <= 1e-12:
            continue
        have = us_ledger.position(dep["id"], s)["qty"] if s in us_ledger.position(dep["id"]) else 0.0
        px = prices.get(s)
        if not px:
            continue
        if e_new > e_old:
            if dep["status"] != "running" or cfg.get("pending_offline"):
                continue
            usd = (e_new - e_old) * px * scale
            if usd < MIN_USD:
                acts.append((s, "skip", f"额度太小（{usd:.2f} USD < {MIN_USD:g}）"))
                prev[s] = e_new
                continue
            usd, short = cash_guard.check_buy(usd, cash, who=cfg.get("label") or "我的交易系统", symbol=s,
                                              dedupe=f"cash-sys-{dep['id']}-{now:%Y-%m-%d}")
            if usd < MIN_USD:
                acts.append((s, "skip", short or "现金不足"))
                continue
            acts.append((s, "buy", round(usd, 2)))
        else:
            q = have if e_new <= 1e-12 else have * (e_old - e_new) / e_old
            if q * px < 1.0 and e_new <= 1e-12:
                prev[s] = e_new
                continue
            acts.append((s, "sell", q))
        prev[s] = e_new
    done = []
    for s, side, amt in acts:
        if side == "skip":
            done.append(f"{s} 不买：{amt}")
            continue
        r = _order(dep, s, side, amt, prices[s], mkey, now)
        done.append(r)
        if r.startswith("被拒") or r.startswith("未提交"):
            prev[s] = (cfg.get("engine_state") or {}).get(s, 0.0)      # 下次再试
    cfg["engine_state"] = prev
    _save_cfg(dep)
    held = "、".join(f"{s} {v['qty'] * scale:.4g}" for s, v in (res.get("state") or {}).items()) or "空仓"
    _why(dep, ("；".join(done) + "；" if done else "") + f"引擎持仓（按当前额度 {budget:,.0f} USD 折算）：{held}")
    return "；".join(done) if done else None


def _order(dep: dict, sym: str, side: str, amt: float, px: float, mkey: str, now: datetime) -> str:
    """下单（经过通用执行器的订单表和分账）。买入按金额，卖出按数量。"""
    did = dep["id"]
    coid = f"sys{did}-{sym.replace('/', '').lower()}-{side}-{uuid.uuid4().hex[:8]}"
    intent = {"deployment_id": did, "side": side, "purpose": "system", "reason": "我的交易系统"}
    plan = {"side": side}
    if side == "buy":
        intent["notional"], plan["notional"] = amt, round(amt, 2)
    else:
        q = float(f"{amt:.9f}") if mkey == "crypto" else round(amt, 6)
        intent["qty"], plan["qty"] = q, q
    oid = us_ledger.create_order(sym, plan, [intent], str(now.date()), px, coid)
    if not http.paper_orders_unlocked():
        us_ledger.update_order(oid, "rejected", 0, None, {"error": "模拟盘下单锁未开启"})
        us_ledger.allocate(oid)
        return f"未提交 {sym} {side}（下单锁未开启）"
    try:
        info = etf_trend._submit(sym, side, notional=plan.get("notional"), qty=plan.get("qty"), coid=coid,
                                 tif="gtc" if mkey == "crypto" else "day") or {}
        us_ledger.update_order(oid, info.get("status") or "accepted", float(info.get("filled_qty") or 0),
                               float(info.get("filled_avg_price") or 0) or None, info)
        txt = f"{'买入' if side == 'buy' else '卖出'} {sym} " + (f"{amt:,.2f} USD" if side == "buy" else f"{amt:.6g}")
        _log(dep, "INFO", txt + f"（跟随引擎，参考价 {px:,.4g}）")
        return txt
    except RuntimeError as e:
        us_ledger.update_order(oid, "rejected", 0, None, {"error": str(e)[:300]})
        us_ledger.allocate(oid)
        _log(dep, "WARN", f"{sym} {side} 被拒：{str(e)[:160]}")
        return f"被拒 {sym} {side}：{str(e)[:80]}"


def snapshot(dep: dict) -> dict:
    """策略总台卡片：各品种的引擎分批状态（止损）、台账持仓、为什么。"""
    pos = us_ledger.position(dep["id"])
    return {"positions": {s: p["qty"] for s, p in pos.items() if abs(p["qty"]) > us_ledger.QTY_EPS},
            "engine": dep["config"].get("engine_view") or {}, "why": get_why(dep["id"])}
