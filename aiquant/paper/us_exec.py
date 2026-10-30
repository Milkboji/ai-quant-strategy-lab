"""通用美股日线执行器（Alpaca Paper）：所有美股策略共用一套执行规则，规则来自原 ETF 择时执行器。

- 信号：只用已收盘的日线（美东日期早于今天）；下一交易日开盘 5 分钟后（美东 9:35）下当日有效（DAY）的市价单；
  每个部署每个交易日最多动作一次。
- 被券商拒绝：当天不再重试（写原因、发通知），下一个交易日重新判断。部分成交：次日补足到目标金额。
- 每次检查都写「为什么没有动作」（每个部署一份；ETF 择时沿用原来的 etf_trend_why）。
- 遵守交易日历和半日市（market.session_open / session_close；收盘后检查在收盘 15 分钟后）。
- 部署：paper_deployments 中 broker='alpaca_us' 的行，和 BTC 部署同一张表、同一套上线 / 下线 / 锁定 / 审计 / 通知。
  kind='etf_sma'：均线择时（参数来自 etf_trend_config，版本见 etf_versions，原 ETF 择时迁移而来）；
  kind='lab'：策略实验室的美股日线版本（strategy_versions，主引擎同一套信号函数 build_frame / group_signal）。
- 台账按策略分开（us_ledger）：同一标的的多个策略意图合并成一笔券商净额订单，成交后按比例拆回每个策略。
- 资金：allocation.plan_us（第二层）给每个部署的额度；偏离目标超过阈值时收盘后记下、下一交易日再平衡。
- 交易节奏：每个部署一个 Gate（ETF 择时沿用 rhythm_state::etf），与回测 regime_bt / 主引擎同一套规则。
- 下单前统一现金检查（cash_guard），任何情况下都不使用保证金。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pandas as pd

from .. import config, db
from ..data import alpaca, http
from . import etf_trend, us_ledger

log = logging.getLogger("aiquant.us_exec")
ET = etf_trend.ET
BROKER = "alpaca_us"
ETF_KEY = "etf"
OPEN_MIN = 9 * 60 + 35


# ---------------------------------------------------------------------------
# 部署
# ---------------------------------------------------------------------------

def kind(dep: dict) -> str:
    return (dep.get("config") or {}).get("kind", "lab")


def deployments(include_paused: bool = True, include_test: bool = False, statuses=None) -> list[dict]:
    from . import runner as pr
    st = tuple(statuses or (("running", "paused_entries") if include_paused else ("running",)))
    rows = db.q("SELECT id FROM paper_deployments WHERE broker=? AND status IN "
                f"({','.join('?' * len(st))})" + ("" if include_test else " AND is_test=0")
                + " AND COALESCE(json_extract(config_json,'$.kind'),'')<>'my_system' ORDER BY id",   # 我的交易系统有自己的执行器
                (BROKER, *st))
    return [pr.get_deployment(r["id"]) for r in rows]


def etf_deployment(any_status: bool = True) -> dict | None:
    from . import runner as pr
    r = db.q1("SELECT id FROM paper_deployments WHERE broker=? AND is_test=0 AND json_extract(config_json,'$.kind')="
              "'etf_sma' ORDER BY id DESC LIMIT 1", (BROKER,))
    if not r:
        return None
    d = pr.get_deployment(r["id"])
    return d if any_status or d["status"] in ("running", "paused_entries") else None


def effective(dep: dict) -> dict:
    """部署的有效参数。ETF 择时：信号 / 交易品种、均线、缓冲带、上下线状态仍以 etf_trend_config 为准（ETF 版本、挑战者、
    策略总台 ETF 卡片照旧写它）；实验室版本：部署配置里的 trade_symbol / signal_symbol。"""
    cfg = dict(dep.get("config") or {})
    if kind(dep) == "etf_sma":
        e = etf_trend.get_config()
        cfg.update(signal_symbol=e["signal_symbol"], trade_symbol=e["trade_symbol"], sma=int(e["sma"]),
                   band=float(e["band"]), enabled=bool(e.get("enabled")), pending_offline=bool(e.get("pending_offline")),
                   lend_pct=float(e.get("lend_pct", 50.0)))
    else:
        cfg.setdefault("enabled", True)
        cfg["pending_offline"] = bool(cfg.get("pending_offline"))
    return cfg


def label(dep: dict) -> str:
    if kind(dep) == "etf_sma":
        e = effective(dep)
        return f"美股 ETF 择时（{e['signal_symbol']}→{e['trade_symbol']}）"
    return dep["config"].get("label") or f"美股部署 #{dep['id']}"


def gate_key(dep: dict) -> str:
    return ETF_KEY if kind(dep) == "etf_sma" else f"us:{dep['id']}"


def _state_key(dep: dict) -> str:
    return "etf_trend_state" if kind(dep) == "etf_sma" else f"us_state::{dep['id']}"


def dep_state(dep: dict) -> dict:
    return db.get_setting(_state_key(dep)) or {}


def _save_state(dep: dict, st: dict) -> None:
    db.set_setting(_state_key(dep), st)


def set_why(dep: dict, code: str, text: str, **extra) -> dict:
    if kind(dep) == "etf_sma":
        return etf_trend.set_why(code, text, **extra)
    key = f"us_why::{dep['id']}"
    cur = db.get_setting(key) or {}
    if cur.get("code") == code and cur.get("text") == text:
        return cur
    w = {"code": code, "text": text, "at": db.now_iso(), **extra}
    db.set_setting(key, w)
    return w


def get_why(dep: dict) -> dict | None:
    return etf_trend.get_why() if kind(dep) == "etf_sma" else db.get_setting(f"us_why::{dep['id']}")


def _log(dep: dict | None, level: str, msg: str) -> None:
    from . import runner as pr
    if dep is None or kind(dep) == "etf_sma":
        pr.event(None if dep is None else dep["id"], level, f"[ETF趋势] {msg}")
    else:
        pr.event(dep["id"], level, f"[美股] {msg}", bool(dep.get("is_test")))


def _save_cfg(did: int, cfg: dict) -> None:
    db.ex("UPDATE paper_deployments SET config_json=?, updated_at=? WHERE id=?",
          (json.dumps(cfg, ensure_ascii=False), db.now_iso(), did))


# ---------------------------------------------------------------------------
# 迁移：原 ETF 择时 → 本执行器上的一个普通部署（不中断、不产生额外交易）
# ---------------------------------------------------------------------------

def migrate_etf(report: bool = True) -> dict | None:
    """一次性、幂等。迁移内容：
    - 新建 paper_deployments 行（broker=alpaca_us，kind=etf_sma，version_id = etf_versions 里正在运行的版本 id），
      状态按 etf_trend_config.enabled（上线 → running；已下线 → stopped）。
    - 状态（etf_trend_state：上次动作日、信号、目标、持仓）、「为什么」（etf_trend_why）、节奏状态（rhythm_state::etf）、
      借出状态（etf_lend）都原样沿用，不复制、不改写 → 当天已动作的不会再动作。
    - 台账期初：券商当前 ETF 持仓（etf_trend_state.position 的股数、成本价）记为该部署的 migrate 记录。
    - 原 etf_orders 里还没完成的订单转入 us_orders（同一个 client_order_id，不重新提交）。
    迁移前后对照写在 settings.us_exec_migration。"""
    from . import etf_versions
    from . import runner as pr
    from .. import console
    us_ledger.ensure_tables()
    done = db.get_setting("us_exec_migration")
    if done and etf_deployment():
        return done
    cfg = etf_trend.get_config()
    st = db.get_setting("etf_trend_state") or {}
    pos = st.get("position") or {}
    qty = float(pos.get("qty") or 0)
    run_v = etf_versions.running()
    before = {"etf_trend_config": {k: cfg.get(k) for k in ("enabled", "pending_offline", "signal_symbol", "trade_symbol",
                                                           "sma", "band", "budget_pct", "lend_pct")},
              "last_action_date": st.get("last_action_date"), "signal": st.get("signal"), "entry": st.get("entry"),
              "position": pos, "why": etf_trend.get_why(), "lend": etf_trend.lend_state(),
              "rhythm_state": db.get_setting("rhythm_state::etf"),
              "open_etf_orders": db.q("SELECT client_order_id, side, status FROM etf_orders WHERE status IN "
                                      "('new','accepted','pending_new','partially_filled')") if _has_table("etf_orders") else []}
    dcfg = {"kind": "etf_sma", "strategy_key": "etf", "label": "美股 ETF 择时", "market": "us_equity",
            "etf_version_id": run_v["id"], "portfolio": True, "deployed_by": "系统迁移",
            "migrated_from": "etf_trend"}
    did = db.ex("INSERT INTO paper_deployments(version_id,config_json,broker,status,created_at,updated_at,is_test) "
                "VALUES(?,?,?,?,?,?,0)", (int(run_v["id"]), json.dumps(dcfg, ensure_ascii=False), BROKER,
                                          "running" if cfg.get("enabled") else "stopped", db.now_iso(), db.now_iso()))
    dep = pr.get_deployment(did)
    if qty > 0:
        us_ledger.record_opening(did, cfg["trade_symbol"], qty, float(pos.get("avg_entry_price") or 0),
                                 "迁移期初持仓（来自 etf_trend_state，券商实际持仓）")
    moved = []
    if _has_table("etf_orders"):
        for o in db.q("SELECT * FROM etf_orders WHERE status IN ('new','accepted','pending_new','partially_filled')"):
            oid = db.ex("INSERT INTO us_orders(client_order_id,symbol,side,notional,qty,status,trade_date,ref_price,"
                        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (o["client_order_id"], o["symbol"], o["side"], o["notional"], o["qty"], o["status"],
                         str(datetime.now(timezone.utc).astimezone(ET).date()), None, o["ts"], db.now_iso()))
            db.ex("INSERT INTO us_order_allocs(order_id,deployment_id,symbol,side,req_notional,req_qty,purpose,reason,"
                  "trade_date,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (oid, did, o["symbol"], o["side"], o["notional"], o["qty"], "entry" if o["side"] == "buy" else "exit",
                   o["reason"], o["signal_date"], db.now_iso()))
            db.ex("UPDATE etf_orders SET status='migrated' WHERE id=?", (o["id"],))
            moved.append(o["client_order_id"])
    after_pos = us_ledger.position(did, cfg["trade_symbol"])
    rep = {"at": db.now_iso(), "deployment_id": did, "version": etf_versions.label(run_v), "before": before,
           "after": {"deployment": {"id": did, "broker": BROKER, "status": dep["status"], "kind": "etf_sma"},
                     "ledger_position": after_pos, "last_action_date": (db.get_setting("etf_trend_state") or {})
                     .get("last_action_date"), "why": etf_trend.get_why(), "lend": etf_trend.lend_state(),
                     "rhythm_state": db.get_setting("rhythm_state::etf"), "moved_open_orders": moved},
           "orders_submitted_by_migration": 0,
           "checks": {"position_same": abs(after_pos["qty"] - qty) < 1e-9,
                      "last_action_kept": (db.get_setting("etf_trend_state") or {}).get("last_action_date")
                      == st.get("last_action_date"),
                      "lend_kept": etf_trend.lend_state() == before["lend"]}}
    db.set_setting("us_exec_migration", rep)
    console.audit("系统", "system", "迁移到通用美股执行器", "etf", "美股 ETF 择时", did, None, run_v["id"], "已迁移",
                  f"期初持仓 {qty:.4f} 股 {cfg['trade_symbol']}；不提交任何订单；上次动作日 {st.get('last_action_date')}")
    _log(dep, "INFO", f"已迁移到通用美股日线执行器（部署 #{did}），不提交任何订单；持仓 {qty:.4f} 股 {cfg['trade_symbol']} "
                      "记为台账期初")
    return rep


def _has_table(name: str) -> bool:
    return db.q1("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)) is not None


def ensure_migrated() -> None:
    try:
        if not etf_deployment():
            etf_trend.ensure_tables()
            migrate_etf()
    except Exception:  # noqa: BLE001
        log.exception("us exec migration failed")


def sync_etf_status(dep: dict) -> dict:
    """ETF 择时的上线 / 下线仍由 etf_versions.set_online 写 etf_trend_config：部署状态跟着它。"""
    from . import runner as pr
    e = etf_trend.get_config()
    want = "running" if e.get("enabled") else "stopped"
    if e.get("enabled") and e.get("pending_offline"):
        want = "paused_entries"
    if dep["status"] != want and dep["status"] in ("running", "paused_entries", "stopped"):
        pr.set_status(dep["id"], want)
        dep = pr.get_deployment(dep["id"])
    return dep


# ---------------------------------------------------------------------------
# 实验室版本的部署 / 切换版本（策略总台 console.deploy_version 调用）
# ---------------------------------------------------------------------------

def trade_symbol_of(v: dict) -> str | None:
    """实验室美股版本的交易品种：最近一次美股日线回测用的标的。"""
    r = db.q1("SELECT d.symbol FROM backtests b JOIN datasets d ON d.id=b.dataset_id WHERE b.version_id=? "
              "AND d.venue='alpaca_stocks' ORDER BY b.id DESC LIMIT 1", (v["id"],))
    return r["symbol"] if r else None


def deploy_lab(key: str, v: dict, *, actor: str | None, label_: str, is_test: bool = False) -> dict:
    """新建实验室美股版本的部署（显式部署；已有部署时由调用方走 switch_version）。"""
    from . import runner as pr
    sym = trade_symbol_of(v)
    if not sym:
        raise ValueError("这个美股版本还没有美股日线回测，不知道交易哪只 ETF：请先在策略实验室用美股品种回测")
    sch = v["schema"]
    cfg = {"kind": "lab", "strategy_key": key, "label": label_, "market": "us_equity", "trade_symbol": sym,
           "signal_symbol": sch.get("signal_symbol") or sym, "signal_tf": 1440, "portfolio": True,
           "deployed_by": actor or "用户"}
    did = pr.save_deployment(v["id"], cfg, BROKER)
    if is_test:
        db.ex("UPDATE paper_deployments SET is_test=1 WHERE id=?", (did,))
    return {"deployment_id": did, "trade_symbol": sym}


def is_flat(did: int) -> bool:
    pos = us_ledger.position(did)
    return all(p["qty"] <= us_ledger.QTY_EPS for p in pos.values()) and not us_ledger.has_open(did)


def switch_version(dep: dict, v: dict, *, actor: str | None, reason: str) -> str:
    """空仓且没有未完成订单 → 立即切换；否则等空仓后切换（执行器每次检查时处理）。"""
    cfg = dep["config"]
    if is_flat(dep["id"]):
        cfg.pop("pending_switch", None)
        sym = trade_symbol_of(v) or cfg.get("trade_symbol")
        cfg.update(trade_symbol=sym, signal_symbol=v["schema"].get("signal_symbol") or sym)
        db.ex("UPDATE paper_deployments SET version_id=?, config_json=?, updated_at=? WHERE id=?",
              (v["id"], json.dumps(cfg, ensure_ascii=False), db.now_iso(), dep["id"]))
        _log(dep, "INFO", f"切换到版本 {v['id']}（{reason}）")
        return "已切换"
    cfg["pending_switch"] = {"version_id": v["id"], "reason": reason, "at": db.now_iso(), "trigger": "manual",
                             "by": actor}
    _save_cfg(dep["id"], cfg)
    return "等待空仓后切换"


def _apply_pending_switch(dep: dict) -> dict:
    from . import runner as pr
    from ..strategy import registry
    p = dep["config"].get("pending_switch")
    if p and is_flat(dep["id"]):
        v = registry.get_version(int(p["version_id"]))
        if v:
            switch_version(dep, v, actor=p.get("by"), reason=p.get("reason") or "空仓后切换")
            dep = pr.get_deployment(dep["id"])
    return dep


# ---------------------------------------------------------------------------
# 信号
# ---------------------------------------------------------------------------

def lab_bars(symbol: str) -> pd.DataFrame:
    """本地美股日线库 → 主引擎日线格式（ts=开盘，close_time=收盘）。"""
    from ..data import us_daily
    d = us_daily.load(symbol)
    if d is None or not len(d):
        raise RuntimeError(f"本地没有 {symbol} 的日线")
    return us_daily.to_bars(d)


def lab_signal(dep: dict, today, holding: bool) -> dict:
    """实验室版本：主引擎同一套信号函数，只用 today 之前已收盘的日线。返回 {date, regime（1=应持有）, entry, exit, close}。"""
    from ..engine.signals import build_frame, group_signal
    from ..strategy import registry
    from ..strategy.schema import validate
    v = registry.get_version(dep["version_id"])
    obj, errs = validate(v["schema"])
    if errs:
        raise RuntimeError("版本校验失败")
    e = effective(dep)
    sig_df = lab_bars(e["signal_symbol"])
    d_et = pd.to_datetime(sig_df["close_time"], utc=True).dt.tz_convert(ET).dt.date
    sig_df = sig_df[d_et < today].reset_index(drop=True)
    if len(sig_df) < 30:
        raise RuntimeError("日线数量不足以计算信号")
    end = pd.Timestamp(sig_df["close_time"].iloc[-1]) + pd.Timedelta(seconds=1)
    frame = build_frame(sig_df, obj, 1440, None, data_end=end)
    ent = group_signal(frame, obj.entry)
    ex = group_signal(frame, obj.exit)
    i = len(frame) - 1
    last = str(pd.Timestamp(frame["close_time"].iloc[i]).tz_convert(ET).date())
    want = (not bool(ex[i])) if holding else (bool(ent[i]) and not bool(ex[i]))
    stop = None
    if holding:
        st = dep_state(dep)
        since = (st.get("entry") or {}).get("signal_date")
        if since and obj.max_hold_bars:
            n = int((pd.to_datetime(frame["close_time"]).dt.tz_convert(ET).dt.date.astype(str) > since).sum())
            if n >= obj.max_hold_bars:
                want, stop = False, "最长持仓"
        if want and (obj.stop_loss_pct or obj.take_profit_pct or obj.trailing):
            sp = stop_plan(dep, today)          # 兜底：券商止损单没有成交（例如没挂上）但收盘日线已触发 → 今天开盘卖出
            if sp and sp.get("hit"):
                want, stop = False, sp["hit"] + "，券商止损单没有成交，今天开盘卖出"
    return {"date": last, "regime": int(want), "entry": bool(ent[i]), "exit": bool(ex[i]),
            "close": float(frame["close"].iloc[i]), "rule": "实验室版本（主引擎信号）", "stop": stop}


def _rules(dep: dict):
    """实验室版本的止损 / 止盈 / 移动止损规则（ETF 择时没有）。没有任何一项时返回 None。"""
    if kind(dep) != "lab":
        return None
    from ..strategy import registry
    from ..strategy.schema import validate
    v = registry.get_version(dep["version_id"])
    obj, errs = validate(v["schema"]) if v else (None, ["missing"])
    if errs or not (obj.stop_loss_pct or obj.take_profit_pct or obj.trailing):
        return None
    return obj


def _session_for(now: datetime):
    """下单所属的交易日：交易时段内 / 开盘前 = 今天；收盘后 / 休市日 = 下一个交易日。"""
    from .. import market as mk
    d = now.astimezone(ET).date()
    if mk.is_trading_day(d) and now < mk.session_close(d):
        return d
    return mk.trading_days_after(d, 1)[0]


def stop_plan(dep: dict, session=None) -> dict | None:
    """该策略在交易日 session 应挂的止损价，与回测（engine/backtest.py）同一套计算、按路径逐日推进：
    入场价 ep = 台账平均成本；第 j 天的止损 = max(ep × (1 − 止损%)，移动止损：峰值 × (1 − 回撤%)，峰值达到激活线后才启用)，
    峰值只用第 j 天之前已收盘的最高价（含入场价）；第 j 天最低价 ≤ 止损 → 触发（回测在当天按 min(开盘, 止损价) 成交）。
    返回 {stop, tp, hit, poll, why}；没有止损规则或没有持仓时返回 None。"""
    obj = _rules(dep)
    if obj is None:
        return None
    e = effective(dep)
    sym = e["trade_symbol"]
    p = us_ledger.position(dep["id"], sym)
    ep = float(p["avg_cost"] or 0)
    if p["qty"] <= us_ledger.QTY_EPS or ep <= 0:
        return None
    session = session or _session_for(datetime.now(timezone.utc))
    since = ((dep_state(dep).get("entry") or {}).get("signal_date")) or ""
    try:
        b = lab_bars(sym)
        dd = pd.to_datetime(b["close_time"], utc=True).dt.tz_convert(ET).dt.date
        b = b[(dd < session) & (dd.astype(str) > since)]
    except Exception:  # noqa: BLE001
        b = pd.DataFrame(columns=["low", "high"])
    sl = ep * (1 - obj.stop_loss_pct / 100) if obj.stop_loss_pct else None
    tp = ep * (1 + obj.take_profit_pct / 100) if obj.take_profit_pct else None

    def stop_at(peak):
        px, why = sl, "止损"
        if obj.trailing and peak >= ep * (1 + obj.trailing.activate_pct / 100):
            t = peak * (1 - obj.trailing.trail_pct / 100)
            if px is None or t > px:
                px, why = t, "移动止损"
        return px, why
    peak, hit = ep, None
    for lo, hi in zip(b["low"].astype(float), b["high"].astype(float)):
        px, why = stop_at(peak)
        if px is not None and lo <= px:
            hit = f"{why}（{px:,.2f}）已在收盘日线上触发"
            break
        if tp is not None and hi >= tp:
            hit = f"止盈（{tp:,.2f}）已在收盘日线上触发"
            break
        peak = max(peak, hi)
    px, why = stop_at(peak)
    poll = (dep.get("config") or {}).get("stop_mode") == "poll"
    txt = (f"{why} {px:,.2f}（入场均价 {ep:,.2f}" + (f"，峰值 {peak:,.2f}" if why == "移动止损" else "") + "）"
           if px is not None else "没有止损价") + (f"；止盈 {tp:,.2f}（交易时段内每 5 分钟检查最新价）" if tp else "")
    return {"stop": round(px, 2) if px is not None else None, "tp": round(tp, 2) if tp else None, "hit": hit,
            "poll": poll, "why": txt, "qty": p["qty"], "symbol": sym, "session": str(session)}


def signal_for(dep: dict, today, holding: bool) -> dict:
    e = effective(dep)
    if kind(dep) == "etf_sma":
        return etf_trend.compute_signal(etf_trend.daily_bars(e["signal_symbol"]), today, e)
    return lab_signal(dep, today, holding)


def describe_signal(dep: dict, sig: dict) -> str:
    e = effective(dep)
    if kind(dep) == "etf_sma":
        return (f"{sig['date']} 收盘 {e['signal_symbol']} {sig['close']:.2f}，{e['sma']} 日均线 {sig['sma']:.2f}"
                f"（上沿 {sig['upper']:.2f} / 下沿 {sig['lower']:.2f}）→ {'看多' if sig['regime'] else '看空'}")
    return (f"{sig['date']} 收盘 {e['signal_symbol']} {sig['close']:.2f}：入场规则{'满足' if sig['entry'] else '不满足'}、"
            f"退出规则{'满足' if sig['exit'] else '不满足'} → {'应持有' if sig['regime'] else '应空仓'}")


# ---------------------------------------------------------------------------
# 券商
# ---------------------------------------------------------------------------

def broker_positions(symbols: list[str]) -> dict[str, dict] | None:
    """券商里这些美股标的的实际持仓（逐只读取，与原 ETF 执行器相同的接口）。"""
    out = {}
    for s in symbols:
        p = etf_trend._position(s)
        if p is None or p.get("error"):
            return None
        q = float(p.get("qty") or 0)
        mv = float(p.get("market_value") or 0)
        out[s] = {"qty": q, "market_value": mv, "price": (mv / q) if q else 0.0}
    return out


def submit(symbol: str, side: str, *, notional: float | None = None, qty: float | None = None, coid: str) -> dict:
    """当日有效（DAY）市价单；碎股不支持时由调用方改整股。"""
    return etf_trend._submit(symbol, side, notional=notional, qty=qty, coid=coid) or {}


def sync_orders() -> list[dict]:
    """同步未完成的券商订单；终结后按比例拆回各策略台账。返回本次拆分结果。"""
    out = []
    for o in us_ledger.open_orders():
        if not o["client_order_id"]:
            continue
        code, b = alpaca.paper_get("/v2/orders:by_client_order_id", {"client_order_id": o["client_order_id"]})
        if code == 200 and isinstance(b, dict):
            us_ledger.update_order(o["id"], b.get("status") or o["status"], float(b.get("filled_qty") or 0),
                                   float(b.get("filled_avg_price") or 0) or None, b)
    for o in us_ledger.unallocated_final():
        res = us_ledger.allocate(o["id"])
        out.extend(res)
        for r in res:
            if r["purpose"] in ("stop", "guard") and r["qty"] > us_ledger.QTY_EPS:
                try:
                    _on_protect_fill(dict(r, symbol=o["symbol"]), o)
                except Exception:  # noqa: BLE001
                    log.exception("protect fill handling failed")
        if res:
            fq = float(o["filled_qty"] or 0) if o["status"] != "internal" else 0
            _log(None if not res else _dep_for(res[0]["deployment_id"]), "INFO",
                 f"{o['symbol']} 订单 {o['client_order_id'] or '（内部对冲）'} {o['status']}：券商成交 {fq:.4f} 股；拆分到 "
                 + "；".join(f"#{r['deployment_id']} {'买' if r['side'] == 'buy' else '卖'} {r['qty']:.4f} 股"
                            f"（{'券商成交' if r['source'] == 'fill' else '内部对冲'}）" for r in res))
    return out


def _cancel_stop(did: int) -> bool:
    """撤掉该策略挂着的止损单（卖出前必须先撤，否则券商会因为股数被占用而拒绝）。没有 → True。"""
    ex = us_ledger.open_stop(did)
    if not ex:
        return True
    ok = bool(ex.get("broker_id")) and etf_trend._cancel(ex["broker_id"])
    if not ok and ex.get("client_order_id"):        # 没有券商 id：按 client_order_id 查一次
        code, b = alpaca.paper_get("/v2/orders:by_client_order_id", {"client_order_id": ex["client_order_id"]})
        if code == 200 and isinstance(b, dict):
            if b.get("status") in us_ledger.FINAL:
                us_ledger.update_order(ex["order_id"], b["status"], float(b.get("filled_qty") or 0),
                                       float(b.get("filled_avg_price") or 0) or None, b)
                sync_orders()
                return b.get("status") != "filled" and float(b.get("filled_qty") or 0) == 0
            ok = bool(b.get("id")) and etf_trend._cancel(b["id"])
    if ok:
        us_ledger.update_order(ex["order_id"], "canceled", 0, None)
        us_ledger.allocate(ex["order_id"])
    return ok


def maintain_stops(now: datetime | None = None) -> list[str]:
    """给有止损规则的持仓挂好当日（收盘后 = 下一交易日）的 DAY 止损单；股数或止损价变了就撤掉重挂。
    券商不支持（例如碎股止损单被拒）时，该策略改为交易时段内每 5 分钟检查最新价（stop_mode=poll），并通知一次。"""
    now = now or datetime.now(timezone.utc)
    if not http.paper_orders_unlocked():
        return []
    sess = _session_for(now)
    out = []
    for dep in deployments():
        sp = stop_plan(dep, sess)
        if sp is None:
            continue
        if sp["poll"] or sp["stop"] is None or sp["hit"]:
            continue
        ex = us_ledger.open_stop(dep["id"])
        if ex and ex["trade_date"] == str(sess) and abs(float(ex["qty"] or 0) - sp["qty"]) < 1e-6 \
                and abs(float(ex["stop_price"] or 0) - sp["stop"]) < 0.005:
            continue
        if us_ledger.has_open(dep["id"]):
            continue                       # 买卖单还没完成：完成后再按新的股数挂
        if ex and not _cancel_stop(dep["id"]):
            continue
        st = dep_state(dep)
        if st.get("stop_retry_after") and now.isoformat() < st["stop_retry_after"]:
            continue
        sym = sp["symbol"]
        its = [{"deployment_id": dep["id"], "side": "sell", "qty": round(sp["qty"], 9), "purpose": "stop",
                "reason": sp["why"]}]
        coid = f"us-{sym.lower()}-stop-{sess}-{uuid.uuid4().hex[:6]}"
        oid = us_ledger.create_order(sym, {"side": "sell", "qty": round(sp["qty"], 9)}, its, str(sess), sp["stop"], coid)
        try:
            info = etf_trend._submit(sym, "sell", qty=round(sp["qty"], 9), coid=coid, order_type="stop",
                                     stop_price=sp["stop"]) or {}
            us_ledger.update_order(oid, info.get("status") or "accepted", float(info.get("filled_qty") or 0),
                                   float(info.get("filled_avg_price") or 0) or None, info)
            out.append(f"#{dep['id']} {sym} 止损单 {sp['qty']:.4f} 股 @ {sp['stop']:,.2f}（{sess}）")
            _log(dep, "INFO", f"挂止损单（{sess} 当日有效）：{sp['qty']:.4f} 股 {sym} @ {sp['stop']:,.2f}；{sp['why']}")
        except RuntimeError as e:
            us_ledger.update_order(oid, "rejected", 0, None, {"error": str(e)[:300]})
            us_ledger.allocate(oid)
            msg = str(e).lower()
            if any(k in msg for k in ("fraction", "not supported", "not allowed", "stop orders")):
                cfg = dict(dep["config"], stop_mode="poll")
                _save_cfg(dep["id"], cfg)
                _log(dep, "WARN", f"券商不接受止损单（{str(e)[:120]}），改为交易时段内每 5 分钟检查最新价、触发后立即下市价单")
                _notify_once("order_failed", f"美股止损改为轮询：{label(dep)}", f"券商拒绝止损单：{str(e)[:200]}\n"
                             "已改为交易时段内每 5 分钟检查最新价，触发后立即下市价单。", f"us-stop-poll-{dep['id']}")
            else:
                st["stop_retry_after"] = (now + timedelta(minutes=30)).isoformat()
                _save_state(dep, st)
                _log(dep, "WARN", f"止损单被拒：{str(e)[:160]}（30 分钟后重试）")
    return out


GUARD_EVERY_MIN = 5


def intraday_guard(now: datetime | None = None) -> list[str]:
    """交易时段内每 5 分钟：止盈（券商同一股数不能同时挂止损和限价）以及止损改为轮询的策略，读最新价，触发后撤止损单、立即下市价单。"""
    from .. import market as mk
    now = now or datetime.now(timezone.utc)
    d = now.astimezone(ET).date()
    if not (mk.is_trading_day(d) and mk.session_open(d) <= now < mk.session_close(d)) or not http.paper_orders_unlocked():
        return []
    last = db.get_setting("us_guard_last")
    if last and (now - datetime.fromisoformat(last)).total_seconds() < GUARD_EVERY_MIN * 60 - 5:
        return []
    db.set_setting("us_guard_last", now.isoformat())
    out = []
    for dep in deployments():
        sp = stop_plan(dep, d)
        if not sp or not (sp["tp"] or sp["poll"]) or us_ledger.has_open(dep["id"]):
            continue
        px = etf_trend._latest_price(sp["symbol"])
        if not px:
            continue
        why = None
        if sp["poll"] and sp["stop"] is not None and px <= sp["stop"]:
            why = f"最新价 {px:,.2f} ≤ 止损价 {sp['stop']:,.2f}"
        elif sp["tp"] and px >= sp["tp"]:
            why = f"最新价 {px:,.2f} ≥ 止盈价 {sp['tp']:,.2f}"
        if not why or not _cancel_stop(dep["id"]):
            continue
        sym = sp["symbol"]
        its = [{"deployment_id": dep["id"], "side": "sell", "qty": round(sp["qty"], 9), "purpose": "guard", "reason": why}]
        coid = f"us-{sym.lower()}-guard-{d}-{uuid.uuid4().hex[:6]}"
        oid = us_ledger.create_order(sym, {"side": "sell", "qty": round(sp["qty"], 9)}, its, str(d), px, coid)
        try:
            info = etf_trend._submit(sym, "sell", qty=round(sp["qty"], 9), coid=coid) or {}
            us_ledger.update_order(oid, info.get("status") or "accepted", float(info.get("filled_qty") or 0),
                                   float(info.get("filled_avg_price") or 0) or None, info)
            _log(dep, "WARN", f"交易时段内检查：{why}，立即卖出 {sp['qty']:.4f} 股 {sym}")
            out.append(f"#{dep['id']} {why}")
        except RuntimeError as e:
            us_ledger.update_order(oid, "rejected", 0, None, {"error": str(e)[:300]})
            us_ledger.allocate(oid)
            _log(dep, "WARN", f"{why}，但卖单被拒：{str(e)[:160]}")
    return out


def _on_protect_fill(r: dict, o: dict) -> None:
    """止损单 / 交易时段内检查的卖单成交后：记离场（交易节奏）、清掉入场记录、写「为什么」。"""
    dep = _dep_for(r["deployment_id"])
    if not dep:
        return
    sym = r.get("symbol") or o["symbol"]
    st = dep_state(dep)
    pos = us_ledger.position(dep["id"], sym)
    gone = pos["qty"] <= us_ledger.QTY_EPS
    pnl = 0.0
    try:
        trades = db.q("SELECT side, qty, price FROM us_ledger WHERE deployment_id=? AND symbol=? ORDER BY id", (dep["id"], sym))
        cost = qty = 0.0
        for t in trades[:-1]:
            if t["side"] == "buy":
                cost += t["qty"] * t["price"]
                qty += t["qty"]
        pnl = (r["price"] - (cost / qty if qty else r["price"])) * r["qty"]
    except Exception:  # noqa: BLE001
        pass
    what = "止损单成交" if r["purpose"] == "stop" else "交易时段内检查触发卖出"
    if gone:
        st["entry"] = None
        _save_state(dep, dict(st, action=f"{what}：卖出 {r['qty']:.4f} 股 {sym} @ {r['price']:,.2f}",
                              position=with_price(pos, r["price"])))
        _on_exit(dep, datetime.now(timezone.utc), pnl)
    set_why(dep, "stopped", f"{what}：卖出 {r['qty']:.4f} 股 {sym} @ {r['price']:,.2f}（约 {pnl:+,.2f} USD）")
    _log(dep, "WARN", f"{what}：卖出 {r['qty']:.4f} 股 {sym} @ {r['price']:,.2f}（约 {pnl:+,.2f} USD）")


def _dep_for(did: int) -> dict | None:
    from . import runner as pr
    return pr.get_deployment(did)


# ---------------------------------------------------------------------------
# 节奏
# ---------------------------------------------------------------------------

def _gate(dep: dict, sym: str, now: datetime) -> tuple[str | None, float]:
    """开仓前的统一纪律（与回测相同的规则；美股影子模式只记录）。ETF 择时沿用原来的 _rhythm_gate（含长周期体检提示）。"""
    if kind(dep) == "etf_sma":
        return etf_trend._rhythm_gate(sym, now)
    from .. import market as mk
    from .. import rhythm
    rh = rhythm.configured(mk.US_EQUITY)
    if rh is None:
        return None, 1.0
    df = etf_trend._trade_daily(sym) if (rh.vol_target_pct or rh.regime_sma_days) else pd.DataFrame(columns=["ts", "close"])
    b, vm, _ = rhythm.live_gate(gate_key(dep), now, df, mk.US_EQUITY)
    block = f"交易节奏：{b}" if b else None
    if rhythm.is_shadow(mk.US_EQUITY):
        if block:
            _log(dep, "INFO", f"影子模式（不影响下单）：如果美股节奏生效，{block}")
        return None, 1.0
    return block, vm


def _on_entry(dep: dict, now: datetime) -> None:
    from .. import market as mk
    from .. import rhythm
    rhythm.live_on_entry(gate_key(dep), now, mk.US_EQUITY)


def _on_exit(dep: dict, now: datetime, pnl: float) -> None:
    if kind(dep) == "etf_sma":
        etf_trend._rhythm_exit(now, pnl)
        return
    from .. import market as mk
    from .. import rhythm
    msg = rhythm.live_on_exit(gate_key(dep), now, "信号离场", pnl, mk.US_EQUITY)
    if msg:
        _log(dep, "INFO" if rhythm.is_shadow(mk.US_EQUITY) else "WARN",
             ("影子模式（不影响下单）：如果美股节奏生效，" if rhythm.is_shadow(mk.US_EQUITY) else "交易节奏：") + msg)


# ---------------------------------------------------------------------------
# 每轮服务循环
# ---------------------------------------------------------------------------

def _all_why(deps: list[dict], code: str, text: str, **extra) -> None:
    for d in deps:
        set_why(d, code, text, **extra)


def step(now: datetime | None = None) -> dict:
    """美股开盘后每轮循环调用。返回 {部署: 本轮动作}。"""
    ensure_migrated()
    deps_all = deployments(statuses=("running", "paused_entries", "stopped"))
    out: dict = {}
    etf_dep = next((d for d in deps_all if kind(d) == "etf_sma"), None)
    if etf_dep:
        sync_etf_status(etf_dep)
    deps = [d for d in deployments() if kind(d) != "etf_sma" or effective(d).get("enabled")]
    if etf_dep and not effective(etf_dep).get("enabled"):
        set_why(etf_dep, "disabled", "美股 ETF 择时已下线（未启用）")
    if not deps:
        return out
    try:   # 安全重启进行中：不下新单
        from .. import safe_restart
        if safe_restart.active():
            _all_why(deps, "safe_restart", "安全重启进行中，暂停下新单")
            return {"skipped": "安全重启进行中，暂停下新单"}
    except Exception:  # noqa: BLE001
        pass
    if not http.paper_orders_unlocked():
        _all_why(deps, "locked", "模拟下单锁未打开（双重锁），只计算信号不下单")
        return out
    us_ledger.ensure_tables()
    sync_orders()
    now = now or datetime.now(timezone.utc)
    clk = etf_trend._clock()
    if not clk:
        _all_why(deps, "no_clock", "暂时连不上券商时钟，稍后重试")
        return out
    if not clk.get("is_open"):
        _all_why(deps, "closed", f"美股休市中，下次开盘 {etf_trend._bj(clk.get('next_open'))}（北京时间）",
                 next_open=clk.get("next_open"))
        return out
    now_et = now.astimezone(ET)
    today = now_et.date()
    todo = [d for d in deps if dep_state(d).get("last_action_date") != str(today)]
    if not todo:
        try:   # 当天已动作完：维护止损单（新买入的仓位挂单、股数变化重挂）+ 每 5 分钟的止盈 / 轮询止损检查
            maintain_stops(now)
            intraday_guard(now)
        except Exception:  # noqa: BLE001
            log.warning("stop maintenance failed", exc_info=True)
        return out
    if now_et.hour * 60 + now_et.minute < OPEN_MIN:
        _all_why(todo, "pre_open", "已开盘，等到美东 9:35（开盘 5 分钟后）再按前一日收盘信号决定")
        return out
    syms = sorted({effective(d)["trade_symbol"] for d in deps} | {effective(d)["signal_symbol"] for d in deps})
    try:
        etf_trend.update_daily_csv(syms, now)
    except Exception:  # noqa: BLE001
        log.warning("us daily refresh failed", exc_info=True)
    try:
        from .. import rhythm
        rhythm.shadow_record(str(today), now)
    except Exception:  # noqa: BLE001
        log.warning("shadow record failed", exc_info=True)
    return _decide(todo, deps, now, today, out)


def _decide(todo: list[dict], deps: list[dict], now: datetime, today, out: dict) -> dict:
    from .. import market as mk
    from .. import allocation as ab
    from . import cash_guard
    acct = etf_trend._account() or {}
    equity = float(acct.get("equity") or 0)
    cash = float(acct.get("cash") or 0)
    bpos = broker_positions(sorted({effective(d)["trade_symbol"] for d in deps}))
    if bpos is None:
        _all_why(todo, "no_clock", "暂时读不到券商持仓，稍后重试（不下单）")
        return out
    rec = us_ledger.reconcile({k: v["qty"] for k, v in bpos.items()}, [effective(d)["trade_symbol"] for d in deps])
    plan = ab.plan_us(equity, deps)
    now_et = now.astimezone(ET)
    sigs: dict[int, dict] = {}
    holding: dict[int, float] = {}
    for d in todo:
        d = _apply_pending_switch(d)
        e = effective(d)
        sym = e["trade_symbol"]
        holding[d["id"]] = us_ledger.position(d["id"], sym)["qty"]
        try:
            sigs[d["id"]] = signal_for(d, today, holding[d["id"]] > us_ledger.QTY_EPS)
        except Exception as ex:  # noqa: BLE001
            set_why(d, "data_error", f"日线数据暂时取不到，稍后重试：{str(ex)[:120]}")
    # ---- 美股市场层面的借出 / 收回（所有美股部署空仓且看空 → 借出；任一部署发出买入信号 → 收回）----
    lend_wait = _lending(deps, sigs, holding, now, cash, equity, plan)
    intents: dict[str, list[dict]] = {}
    decided: dict[int, dict] = {}
    add_pp = float(ab.get_config()["add_pp"])
    for d in todo:
        if d["id"] not in sigs:
            continue
        sig = sigs[d["id"]]
        e = effective(d)
        sym = e["trade_symbol"]
        st = dep_state(d)
        qty = holding[d["id"]]
        pos = us_ledger.position(d["id"], sym)
        px_now = (bpos.get(sym) or {}).get("price") or sig.get("close") or 0.0
        budget = float((plan.get(d["id"]) or {}).get("budget") or 0.0)
        entry = st.get("entry") if isinstance(st.get("entry"), dict) else None
        r = rec.get(sym) or {"ok": True}
        res = {"signal": sig, "budget": budget, "entry": entry, "action": "保持", "px": px_now}
        decided[d["id"]] = res
        if kind(d) == "etf_sma":
            try:
                from . import etf_versions
                if etf_versions.finish_pending_offline(qty):
                    set_why(d, "disabled", "已空仓，按计划下线")
                    res["skip"] = True
                    continue
            except Exception:  # noqa: BLE001
                log.warning("pending offline check failed", exc_info=True)
        if us_ledger.has_open(d["id"]):
            res["action"] = "等待未完成订单"
            set_why(d, "pending", "上一笔订单还没完成，等它成交或过期后再动作")
            continue
        if not r["ok"]:
            res.update(action="对账不一致，暂停下单", nolast=True)
            set_why(d, "recon", f"{sym} 对账不一致：券商 {r['broker']:.4f} 股 / 各策略台账合计 {r['ledger']:.4f} 股，"
                                "暂停该标的下单，等待核对")
            _notify_once("order_failed", f"美股对账不一致：{sym}", f"券商 {r['broker']:.6f} 股，各策略台账合计 "
                         f"{r['ledger']:.6f} 股；该标的暂停下单。", f"us-recon-{sym}-{today}")
            continue
        if d["id"] in lend_wait:
            res.update(action="等待还款", nolast=True)
            set_why(d, "reclaim", lend_wait[d["id"]])
            continue
        entries_ok = d["status"] == "running" and not e.get("pending_offline")
        band_txt = ""
        if kind(d) == "etf_sma" and sig["lower"] <= sig["close"] <= sig["upper"]:
            band_txt = (f"（收盘 {sig['close']:.2f} 在均线缓冲带 {sig['lower']:.2f}~{sig['upper']:.2f} 内，维持原状态）")
        if sig["regime"] == 1 and qty <= us_ledger.QTY_EPS:
            if not entries_ok:
                set_why(d, "pending_offline", "等待下线：不再开新仓" if e.get("pending_offline") else "暂停新开仓")
                continue
            block, vm = _gate(d, sym, now)
            if block:
                set_why(d, "long_pause" if block.startswith("长周期体检") else "rhythm", f"看多，但{block}，暂不买入")
                continue
            target = budget * vm
            if equity and target > ab.position_cap(equity):          # 单个策略持仓上限
                target = ab.position_cap(equity)
            why = _entry_reason(d, sig) + (f"；波动率定仓 {vm:.0%}" if vm < 1 else "")
            if target < 10:
                set_why(d, "small", f"看多，但本策略额度为 0（{(plan.get(d['id']) or {}).get('why') or '等待分配'}），暂不买入")
                continue
            intents.setdefault(sym, []).append({"deployment_id": d["id"], "side": "buy", "notional": target,
                                                "purpose": "entry", "reason": why, "target": target,
                                                "target_pct": round(budget / equity * 100, 4) if equity else None})
            res.update(action="买入", target=target, why=why)
        elif sig["regime"] == 1:
            need = 0.0
            if entry and entries_ok:
                need = etf_trend.topup_needed(entry["target"], us_ledger.entry_filled_usd(d["id"], sym, entry["signal_date"]))
            mv = qty * px_now
            cap_usd = ab.position_cap(equity) if equity else 0.0
            cur_pct = budget / equity * 100 if equity else 0.0
            if entry is not None and entry.get("target_pct") is None:      # 迁移 / 旧状态：以当前目标为起点，不加仓
                entry = dict(entry, target_pct=round(cur_pct, 4))
                res["entry_new"] = entry
            add_pct = (cur_pct - float(entry["target_pct"])) if entry else 0.0
            if equity and mv > cap_usd * 1.005:
                q = round((mv - cap_usd) / px_now, 6) if px_now else 0.0
                intents.setdefault(sym, []).append({"deployment_id": d["id"], "side": "sell", "qty": min(q, qty),
                                                    "purpose": "cap",
                                                    "reason": f"风控上限：持仓 {mv / equity:.1%} 超过账户权益的 "
                                                              f"{ab.get_config()['position_cap_pct']:g}%，减到上限"})
                res.update(action="减到上限")
            elif need >= 10:
                intents.setdefault(sym, []).append({"deployment_id": d["id"], "side": "buy", "notional": need,
                                                    "purpose": "topup", "reason": f"上次买入只部分成交，补足到目标 "
                                                    f"{entry['target']:,.0f} USD"})
                res.update(action="补足", need=need)
            elif entry and entries_ok and add_pct >= add_pp and equity:
                amt = min(add_pct * equity / 100, max(0.0, cap_usd - mv))
                if amt >= 10:
                    intents.setdefault(sym, []).append({
                        "deployment_id": d["id"], "side": "buy", "notional": round(amt, 2), "purpose": "add",
                        "new_target_pct": round(cur_pct, 4),
                        "reason": f"目标比例从 {float(entry['target_pct']):.1f}% 提高到 {cur_pct:.1f}%（高出 {add_pct:.1f} 个百分点，"
                                  f"阈值 {add_pp:g}），按差额加仓；目标降低时不卖出，价格涨跌不调整"})
                    res.update(action="加仓", add=amt)
                else:
                    set_why(d, "hold", f"看多且已持有 {sym}，保持不动{band_txt}")
            else:
                set_why(d, "hold", f"看多且已持有 {sym}，保持不动{band_txt}")
        elif qty > us_ledger.QTY_EPS:
            why = _exit_reason(d, sig)
            intents.setdefault(sym, []).append({"deployment_id": d["id"], "side": "sell", "qty": round(qty, 9),
                                                "purpose": "exit", "reason": why})
            res.update(action="卖出全部", why=why, pnl=(px_now - pos["avg_cost"]) * qty)
        else:
            set_why(d, "flat", f"看空，保持现金，不持有 {sym}{band_txt}")
    # ---- 合并下单：每个标的一笔净额订单 ----
    reserved = 0.0
    try:
        reserved = cash_guard.reserved_cash(etf_trend._btc_price(), exclude="etf")
    except Exception:  # noqa: BLE001
        pass
    avail = cash
    for sym, its in intents.items():
        avail = _place(sym, its, decided, today, now, avail, reserved)
    # ---- 写状态 ----
    for d in todo:
        res = decided.get(d["id"])
        if not res or res.get("skip"):
            continue
        sym = effective(d)["trade_symbol"]
        st = dep_state(d)
        pos = with_price(us_ledger.position(d["id"], sym), res.get("px"))
        new = dict(st, signal=res["signal"], action=res["action"], position=pos, budget=res["budget"],
                   checked_at=db.now_iso(), entry=res.get("entry_new", res.get("entry")))
        new.pop("rebalance_due", None)
        if not res.get("nolast"):
            new["last_action_date"] = str(today)
        _save_state(d, new)
        if not res.get("nolast") and res["action"] != "等待还款":
            _log(d, "INFO", f"{describe_signal(d, res['signal'])}；动作：{res['action']}")
        out[d["id"]] = res["action"]
    return out


def with_price(p: dict, price: float | None) -> dict:
    x = us_ledger.with_marks(p, price)
    x.setdefault("market_value", 0.0)
    x["avg_entry_price"] = x.get("avg_cost", 0.0)
    x["unrealized_pl"] = x.get("unrealized", 0.0)
    return x


def _entry_reason(dep: dict, sig: dict) -> str:
    e = effective(dep)
    if kind(dep) == "etf_sma":
        return f"{e['signal_symbol']} 收盘 {sig['close']:.2f} 高于 {e['sma']} 日均线上沿 {sig['upper']:.2f}"
    return f"{sig['date']} 收盘入场规则满足（{e['signal_symbol']} 收盘 {sig['close']:.2f}）"


def _exit_reason(dep: dict, sig: dict) -> str:
    e = effective(dep)
    if kind(dep) == "etf_sma":
        return f"{e['signal_symbol']} 收盘 {sig['close']:.2f} 低于 {e['sma']} 日均线下沿 {sig['lower']:.2f}"
    if sig.get("stop"):
        return f"{sig['date']} 收盘后检查：{sig['stop']}"
    return f"{sig['date']} 收盘退出规则满足（{e['signal_symbol']} 收盘 {sig['close']:.2f}）"


def _place(sym: str, its: list[dict], decided: dict, today, now: datetime, avail: float, reserved: float) -> float:
    """一个标的的全部意图 → 现金检查 → 一笔净额订单（或完全内部对冲）。返回剩余可用现金。"""
    from . import cash_guard
    ref = _ref_price(sym, decided, its)
    buys = [i for i in its if i["side"] == "buy"]
    want = sum(i["notional"] for i in buys)
    sell_usd = sum(i["qty"] for i in its if i["side"] == "sell") * ref
    short = None
    if want > 0:   # 现金检查：卖出部分在同一笔净额里抵扣，只检查净买入金额
        net_need = max(want - sell_usd, 0.0)
        ok, short = cash_guard.check_buy(net_need, avail, who="美股执行器", symbol=sym, reserved=reserved,
                                         dedupe=f"cash-us-{sym}-{today}")
        if short:
            scale = (ok + sell_usd) / want if want else 0.0
            for i in buys:
                i["notional"] = round(i["notional"] * scale, 2)
        dropped = [i for i in buys if i["notional"] < cash_guard.MIN_ORDER_USD]
        buys = [i for i in buys if i["notional"] >= cash_guard.MIN_ORDER_USD]
        for i in dropped:
            res = decided[i["deployment_id"]]
            set_why(_dep_for(i["deployment_id"]), "small", (short or "可用资金不足 10 USD") + "，暂不买入")
            res["action"] = "保持"
    sells = []
    for i in [x for x in its if x["side"] == "sell"]:
        if _cancel_stop(i["deployment_id"]):
            sells.append(i)
        else:                                   # 止损单撤不掉（可能刚成交）：这次不卖，下一轮再看
            res = decided[i["deployment_id"]]
            res.update(action="等待止损单状态", nolast=True)
            set_why(_dep_for(i["deployment_id"]), "pending", "挂着的止损单还没撤掉（可能刚成交），稍后重试")
    its = sells + buys
    if not its:
        return avail
    plan = us_ledger.plan_net(its, ref)
    coid = f"us-{sym.lower()}-{plan['side']}-{today}-{uuid.uuid4().hex[:6]}" if plan["side"] else None
    oid = us_ledger.create_order(sym, plan, its, str(today), ref, coid)
    if not plan["side"]:
        us_ledger.allocate(oid)
        _after_submit(sym, its, decided, now, short, internal=True)
        return avail
    try:
        try:
            info = submit(sym, plan["side"], notional=plan.get("notional"), qty=plan.get("qty"), coid=coid)
        except RuntimeError as e:   # 不支持碎股：改整股
            if "fraction" not in str(e).lower():
                raise
            q = int((plan["notional"] // ref) if plan["side"] == "buy" else plan["qty"])
            if q < 1:
                raise
            coid2 = coid + "-w"
            db.ex("UPDATE us_orders SET client_order_id=?, qty=?, notional=NULL WHERE id=?", (coid2, q, oid))
            info = submit(sym, plan["side"], qty=q, coid=coid2)
        us_ledger.update_order(oid, info.get("status") or "accepted", float(info.get("filled_qty") or 0),
                               float(info.get("filled_avg_price") or 0) or None, info)
    except RuntimeError as e:
        us_ledger.update_order(oid, "rejected", 0, None, {"error": str(e)[:300]})
        us_ledger.allocate(oid)
        _reject(sym, its, decided, today, plan["side"], e)
        return avail
    _after_submit(sym, its, decided, now, short)
    if plan["side"] == "buy":
        avail -= float(plan.get("notional") or 0)
    return avail


def _ref_price(sym: str, decided: dict, its: list[dict]) -> float:
    for i in its:
        r = decided.get(i["deployment_id"]) or {}
        if r.get("px"):
            return float(r["px"])
    try:
        return float(etf_trend.daily_bars(sym, 10)["close"].iloc[-1])
    except Exception:  # noqa: BLE001
        return 0.0


def _after_submit(sym: str, its: list[dict], decided: dict, now: datetime, short: str | None, internal: bool = False) -> None:
    from .. import market as mk
    for i in its:
        d = _dep_for(i["deployment_id"])
        res = decided[i["deployment_id"]]
        tag = "（与其他策略内部对冲）" if internal else ""
        if i["purpose"] == "entry":
            res["entry_new"] = {"signal_date": res["signal"]["date"], "target": round(res.get("target", i["notional"]), 2),
                                "target_pct": i.get("target_pct")}
            res["action"] = f"买入 {sym} {i['notional']:,.0f} USD{tag}"
            set_why(d, "bought", f"看多：{i['reason']}，已下单 {res['action']}" + (f"；{short}；差额下一个交易日补足" if short else ""))
            _on_entry(d, now)
            _lend_done_if_bought(short)
        elif i["purpose"] == "topup":
            res["action"] = f"补足 买入 {sym} {i['notional']:,.0f} USD{tag}"
            set_why(d, "topup", f"上次买入只部分成交，今天补买 {i['notional']:,.0f} USD" + (f"（{short}）" if short else ""))
            _lend_done_if_bought(short)
        elif i["purpose"] == "add":
            e0 = dict(res.get("entry_new") or res.get("entry") or {})
            e0.update(target=round(float(e0.get("target") or 0) + i["notional"], 2), target_pct=i["new_target_pct"])
            res["entry_new"] = e0
            res["action"] = f"加仓 买入 {sym} {i['notional']:,.0f} USD{tag}"
            set_why(d, "add", f"{i['reason']}，已下单 {res['action']}" + (f"；{short}" if short else ""))
        elif i["purpose"] == "cap":
            res["action"] = f"减到上限 卖出 {sym} {i['qty']:.4f} 股{tag}"
            set_why(d, "cap", f"{i['reason']}，已下单 {res['action']}")
        else:
            res["action"] = f"卖出全部 {sym}{tag}"
            res["entry_new"] = None
            _on_exit(d, now, float(res.get("pnl") or 0))
            set_why(d, "sold", f"看空：{i['reason']}，已卖出全部 {sym}")


def _lend_done_if_bought(short: str | None) -> None:
    if etf_trend.lend_state().get("state") != "none" and not short:
        etf_trend._set_lend("none", "美股已买入，额度归还", etf_trend.get_config())


def _reject(sym: str, its: list[dict], decided: dict, today, side: str, err: Exception) -> None:
    """被拒：当天不再重试（每个相关部署都记当天已动作），写原因并通知。"""
    msg = str(err)[:200]
    for i in its:
        d = _dep_for(i["deployment_id"])
        res = decided[i["deployment_id"]]
        res["action"] = f"下单被拒（{'买入' if side == 'buy' else '卖出'} {sym}）：{msg}"
        set_why(d, "rejected", f"今天的{'买入' if side == 'buy' else '卖出'}订单被券商拒绝：{msg}。今天不再重试，下一个交易日重新判断")
    _log(_dep_for(its[0]["deployment_id"]), "WARN", f"{'买入' if side == 'buy' else '卖出'} {sym} 被拒绝：{msg}（今天不再重试）")
    _notify_once("order_failed", f"美股下单被拒：{sym}", f"{'买入' if side == 'buy' else '卖出'} {sym} 被 Alpaca Paper 拒绝：{msg}\n"
                 "今天不再重试，下一个交易日按信号重新判断。", f"us-reject-{sym}-{today}-{side}")


def _notify_once(kind_: str, title: str, body: str, dedupe: str) -> None:
    try:
        from .. import notify
        notify.event(kind_, title, body, dedupe=dedupe)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 借出 / 收回（美股市场层面；规则与原 ETF 择时相同，状态仍在 etf_lend）
# ---------------------------------------------------------------------------

def _lending(deps: list[dict], sigs: dict, holding: dict, now: datetime, cash: float, equity: float,
             plan: dict) -> dict[int, str]:
    """返回 {部署: 等待还款的原因}（这些部署今天先不买，不记已动作）。"""
    cfg = etf_trend.get_config()
    ls = etf_trend.lend_state().get("state", "none")
    lend_pct = float(cfg.get("lend_pct", 50.0))
    known = [d for d in deps if d["id"] in sigs]
    if not known:
        return {}
    any_hold = any(us_ledger.position(d["id"]).get(effective(d)["trade_symbol"], {}).get("qty", 0) > us_ledger.QTY_EPS
                   or us_ledger.has_open(d["id"]) for d in deps)
    all_bear = all(sigs[d["id"]]["regime"] == 0 for d in known) and len(known) == len(deps)
    buyers = [d for d in known if sigs[d["id"]]["regime"] == 1 and holding.get(d["id"], 0) <= us_ledger.QTY_EPS
              and d["status"] == "running" and not effective(d).get("pending_offline")]
    offline = all(effective(d).get("pending_offline") for d in deps)
    if any_hold and ls == "lent":
        etf_trend._set_lend("none", "美股已有持仓，借出状态结束", cfg)
        ls = "none"
    elif not any_hold and all_bear and ls == "none" and lend_pct > 0 and not offline:
        etf_trend._set_lend("lent", f"美股全部空仓（看空），BTC 最多临时使用美股额度的 {lend_pct:g}%", cfg)
        ls = "lent"
    elif buyers and ls == "lent":
        b0 = buyers[0]
        etf_trend._set_lend("reclaim", f"{label(b0)} 发出买入信号（开盘检查时确认，收盘后检查没有运行），收回借给 BTC 的额度："
                                       "BTC 按借用比例减仓并暂停新开仓", cfg, signal_date=sigs[b0["id"]]["date"])
        ls = "reclaim"
    if ls == "reclaim" and not buyers and not any_hold:
        etf_trend._set_lend("none", "收回结束（信号转空）", cfg)
        ls = "none"
    out = {}
    if ls == "reclaim" and buyers:
        waiting = etf_trend.reclaim_pending()
        now_et = now.astimezone(ET)
        if waiting and now_et.hour * 60 + now_et.minute < etf_trend.RECLAIM_DEADLINE_MIN:
            need = sum(float((plan.get(d["id"]) or {}).get("budget") or 0) for d in buyers)
            for d in buyers:
                out[d["id"]] = (f"看多，先等 BTC 按借用比例减仓、归还额度（还剩部署 {', '.join('#' + w for w in waiting)}；"
                                f"BTC 已暂停新开仓）。现金 {cash:,.0f} / 需要 {need:,.0f} USD；最晚美东 10:30 按可用现金买入")
    return out


def after_close(now: datetime | None = None) -> dict | None:
    """美股收盘后（收盘 15 分钟起，含周末、节假日）：用刚收盘的日线确认下一交易日信号。
    - 借出中且有空仓部署确认买入 → 进入收回（BTC 下一个美股开盘前按借用比例减仓）。
    - 有止损规则的持仓：挂好下一交易日的 DAY 止损单。
    价格涨跌造成的偏离不再再平衡（2026-10-30 决定）。每个收盘日只确认一次。"""
    from .. import market as mk
    from .. import allocation as ab
    ensure_migrated()
    now = now or datetime.now(timezone.utc)
    now_et = now.astimezone(ET)
    d = now_et.date()
    last = d if (mk.is_trading_day(d) and now >= mk.session_close(d) + timedelta(minutes=15)) else None
    if last is None:
        if mk.is_trading_day(d) and now >= mk.session_open(d):
            return None
        cur = d
        for _ in range(10):
            cur -= timedelta(days=1)
            if mk.is_trading_day(cur):
                last = cur
                break
    if last is None or (db.get_setting("us_after_close") or {}).get("date") == str(last):
        return None
    deps = deployments()
    if not deps:
        return None
    nxt = mk.trading_days_after(last, 1)[0]
    if any(kind(dep) == "lab" for dep in deps) and (db.get_setting("us_daily_after_close") or "") != str(last):
        try:   # 实验室版本用本地日线库：先把刚收盘的日线写进库（只写已收盘的交易日）
            syms = sorted({effective(x)["trade_symbol"] for x in deps} | {effective(x)["signal_symbol"] for x in deps})
            from ..data import us_daily
            us_daily.refresh(syms, now, closed_through=last)
            db.set_setting("us_daily_after_close", str(last))
        except Exception:  # noqa: BLE001
            log.warning("after-close daily refresh failed", exc_info=True)
    sigs, ready = {}, True
    for dep in deps:
        sym = effective(dep)["trade_symbol"]
        held = us_ledger.position(dep["id"], sym)["qty"] > us_ledger.QTY_EPS
        try:
            s = signal_for(dep, nxt, held)
        except Exception as e:  # noqa: BLE001
            log.info("after-close signal not ready: %s", e)
            return None
        if s["date"] != str(last):
            ready = False
        sigs[dep["id"]] = (s, held, sym)
    if not ready:
        return None              # 收盘日线还没出来
    out = {"date": str(last), "next": str(nxt), "at": db.now_iso(), "deployments": {}}
    # 借出 → 收回
    if etf_trend.lend_state().get("state") == "lent":
        buyers = [dep for dep in deps if sigs[dep["id"]][0]["regime"] == 1 and not sigs[dep["id"]][1]
                  and dep["status"] == "running" and not effective(dep).get("pending_offline")]
        if buyers:
            b0 = buyers[0]
            s0 = sigs[b0["id"]][0]
            etf_trend._set_lend("reclaim", f"{last} 收盘后确认 {label(b0)} 买入信号（{describe_signal(b0, s0)}），收回借给 BTC 的"
                                           f"额度：BTC 在 {nxt} 美股开盘前按借用比例减仓、暂停新开仓",
                                etf_trend.get_config(), signal_date=s0["date"])
            for dep in buyers:
                set_why(dep, "reclaim", f"{last} 收盘后确认买入信号：BTC 在 {nxt} 美股开盘前按借用比例减仓，开盘后先还款再买入 "
                                        f"{effective(dep)['trade_symbol']}")
    for dep in deps:
        s_, held, sym = sigs[dep["id"]]
        out["deployments"][dep["id"]] = {"regime": s_["regime"], "held": held}
    # 有止损规则的持仓：挂好下一交易日的 DAY 止损单（开盘就生效，跳空低开时按开盘价附近成交，与回测一致）
    try:
        out["stops"] = maintain_stops(now)
    except Exception:  # noqa: BLE001
        log.warning("after-close stop placement failed", exc_info=True)
    db.set_setting("us_after_close", out)
    return out


def _close_of(sym: str, day) -> float | None:
    try:
        b = lab_bars(sym)
        dd = pd.to_datetime(b["close_time"], utc=True).dt.tz_convert(ET).dt.date
        x = b[dd == day]
        if len(x):
            return float(x["close"].iloc[-1])
    except Exception:  # noqa: BLE001
        pass
    try:
        df = etf_trend.daily_bars(sym, 10)
        x = df[df["date"] == day]
        return float(x["close"].iloc[-1]) if len(x) else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# 解释（策略总台）
# ---------------------------------------------------------------------------

def explain(dep: dict, equity: float | None = None) -> dict:
    """实验室美股部署的「为什么没有动作」与计算明细（ETF 择时用 console.etf_explain）。"""
    from .. import allocation as ab
    items, calc, heads = [], [], []
    w = get_why(dep)
    if w:
        heads.append(w["text"])
        items.append(("最近一次判断", f"{etf_trend._bj(w.get('at'))}：{w['text']}"))
    st = dep_state(dep)
    sig = st.get("signal") or {}
    if sig:
        items.append(("信号", describe_signal(dep, sig)))
    e = effective(dep)
    sym = e["trade_symbol"]
    p = us_ledger.position(dep["id"], sym)
    calc.append(("交易品种", f"{sym}（信号品种 {e['signal_symbol']}）；已收盘日线出信号，下一交易日美东 9:35 后下当日有效市价单"))
    calc.append(("本策略台账", f"{p['qty']:.4f} 股，平均成本 {p['avg_cost']:,.2f}，已实现盈亏 {p['realized']:+,.2f} USD"))
    if equity:
        pl = ab.plan_us(equity, deployments()).get(dep["id"]) or {}
        calc.append(("资金额度（第二层）", f"{pl.get('budget', 0):,.0f} USD：{pl.get('why', '--')}"))
    try:
        from .. import allocation
        ts = allocation.trial_status(dep)
        items.append(("试用期", ts["text"] + ("：固定额度从现金层出，满期后参与按实力分配" if ts["trial"] else "")))
    except Exception:  # noqa: BLE001
        pass
    sp = stop_plan(dep)
    if sp:
        os_ = us_ledger.open_stop(dep["id"])
        items.append(("止损", f"{sp['why']}；" + (f"券商止损单 {os_['qty']:.4f} 股 @ {os_['stop_price']:,.2f}（{os_['trade_date']}）"
                                                if os_ else ("交易时段内每 5 分钟检查最新价" if sp.get("poll") else "下一次检查时挂单"))))
    if dep["config"].get("pending_switch"):
        items.append(("待切换", "有持仓或未完成订单，空仓后切换到新版本"))
    if e.get("pending_offline"):
        heads.insert(0, "等待空仓后下线：不再开新仓")
    return {"headline": "；".join(heads) or "等待下一次美股开盘检查", "items": items, "calc": calc}
