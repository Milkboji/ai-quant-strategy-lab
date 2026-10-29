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
                f"({','.join('?' * len(st))})" + ("" if include_test else " AND is_test=0") + " ORDER BY id",
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
            stop = _stop_hit(dep, obj, e["trade_symbol"], since, today)
            if stop:
                want = False
    return {"date": last, "regime": int(want), "entry": bool(ent[i]), "exit": bool(ex[i]),
            "close": float(frame["close"].iloc[i]), "rule": "实验室版本（主引擎信号）", "stop": stop}


def _stop_hit(dep: dict, obj, sym: str, since: str | None, today) -> str | None:
    """止损 / 止盈 / 移动止损：用入场后已收盘的交易品种日线（最低价 / 最高价）检查，触发则下一交易日开盘卖出。
    与回测的差异：回测在触发当天按止损价成交，模拟盘每天只在开盘后动作一次，所以在下一个开盘成交（写在「为什么」里）。"""
    p = us_ledger.position(dep["id"], sym)
    ep = float(p["avg_cost"] or 0)
    if ep <= 0:
        return None
    b = lab_bars(sym)
    dd = pd.to_datetime(b["close_time"], utc=True).dt.tz_convert(ET).dt.date
    b = b[(dd < today) & ((dd.astype(str) > since) if since else True)]
    if not len(b):
        return None
    lo, hi = float(b["low"].min()), float(b["high"].max())
    stop_px = ep * (1 - obj.stop_loss_pct / 100) if obj.stop_loss_pct else None
    why = "止损"
    if obj.trailing and hi >= ep * (1 + obj.trailing.activate_pct / 100):
        tpx = hi * (1 - obj.trailing.trail_pct / 100)
        if stop_px is None or tpx > stop_px:
            stop_px, why = tpx, "移动止损"
    if stop_px is not None and lo <= stop_px:
        return f"{why}（触发价 {stop_px:,.2f}，入场后最低 {lo:,.2f}；模拟盘在下一交易日开盘卖出）"
    if obj.take_profit_pct and hi >= ep * (1 + obj.take_profit_pct / 100):
        return f"止盈（目标 {ep * (1 + obj.take_profit_pct / 100):,.2f}，入场后最高 {hi:,.2f}；模拟盘在下一交易日开盘卖出）"
    return None


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
        if res:
            fq = float(o["filled_qty"] or 0) if o["status"] != "internal" else 0
            _log(None if not res else _dep_for(res[0]["deployment_id"]), "INFO",
                 f"{o['symbol']} 订单 {o['client_order_id'] or '（内部对冲）'} {o['status']}：券商成交 {fq:.4f} 股；拆分到 "
                 + "；".join(f"#{r['deployment_id']} {'买' if r['side'] == 'buy' else '卖'} {r['qty']:.4f} 股"
                            f"（{'券商成交' if r['source'] == 'fill' else '内部对冲'}）" for r in res))
    return out


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
    rbp = float(ab.layer1(equity, deps)["rebalance_pp"])
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
            why = _entry_reason(d, sig) + (f"；波动率定仓 {vm:.0%}" if vm < 1 else "")
            intents.setdefault(sym, []).append({"deployment_id": d["id"], "side": "buy", "notional": target,
                                                "purpose": "entry", "reason": why, "target": target})
            res.update(action="买入", target=target, why=why)
        elif sig["regime"] == 1:
            need = 0.0
            if entry and entries_ok:
                need = etf_trend.topup_needed(entry["target"], us_ledger.entry_filled_usd(d["id"], sym, entry["signal_date"]))
            due = st.get("rebalance_due")
            mv = qty * px_now
            dev_pp = (mv - budget) / equity * 100 if equity else 0.0
            if need >= 10:
                intents.setdefault(sym, []).append({"deployment_id": d["id"], "side": "buy", "notional": need,
                                                    "purpose": "topup", "reason": f"上次买入只部分成交，补足到目标 "
                                                    f"{entry['target']:,.0f} USD"})
                res.update(action="补足", need=need)
            elif due and due.get("date") != str(today) and abs(dev_pp) > rbp and budget > 0 and entries_ok:
                if dev_pp > 0:
                    q = round((mv - budget) / px_now, 6) if px_now else 0.0
                    intents.setdefault(sym, []).append({"deployment_id": d["id"], "side": "sell", "qty": min(q, qty),
                                                        "purpose": "rebalance_sell",
                                                        "reason": f"再平衡：实际 {mv / equity:.1%}、目标 {budget / equity:.1%}，"
                                                                  f"高出 {dev_pp:.1f} 个百分点（阈值 {rbp:g}）"})
                else:
                    intents.setdefault(sym, []).append({"deployment_id": d["id"], "side": "buy", "notional": budget - mv,
                                                        "purpose": "rebalance_buy",
                                                        "reason": f"再平衡：实际 {mv / equity:.1%}、目标 {budget / equity:.1%}，"
                                                                  f"低了 {-dev_pp:.1f} 个百分点（阈值 {rbp:g}）"})
                res.update(action="再平衡", dev_pp=dev_pp)
            else:
                if due and abs(dev_pp) <= rbp:
                    res["clear_rebalance"] = True
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
        if res.get("clear_rebalance"):
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
    its = [i for i in its if i["side"] == "sell"] + buys
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
            res["entry_new"] = {"signal_date": res["signal"]["date"], "target": round(res.get("target", i["notional"]), 2)}
            res["action"] = f"买入 {sym} {i['notional']:,.0f} USD{tag}"
            set_why(d, "bought", f"看多：{i['reason']}，已下单 {res['action']}" + (f"；{short}；差额下一个交易日补足" if short else ""))
            _on_entry(d, now)
            _lend_done_if_bought(short)
        elif i["purpose"] == "topup":
            res["action"] = f"补足 买入 {sym} {i['notional']:,.0f} USD{tag}"
            set_why(d, "topup", f"上次买入只部分成交，今天补买 {i['notional']:,.0f} USD" + (f"（{short}）" if short else ""))
            _lend_done_if_bought(short)
        elif i["purpose"] in ("rebalance_buy", "rebalance_sell"):
            res["action"] = (f"再平衡 {'买入' if i['side'] == 'buy' else '卖出'} {sym} "
                             + (f"{i['notional']:,.0f} USD" if i["side"] == "buy" else f"{i['qty']:.4f} 股") + tag)
            res["clear_rebalance"] = True
            set_why(d, "rebalance", f"{i['reason']}，已下单 {res['action']}")
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
    - 每个持仓部署：实际仓位与目标的偏离超过阈值 → 记 rebalance_due，下一交易日开盘后再平衡。
    每个收盘日只确认一次。"""
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
    # 偏离目标 → 下一交易日再平衡
    acct = etf_trend._account() or {}
    equity = float(acct.get("equity") or 0)
    plan = ab.plan_us(equity, deps)
    rbp = float(ab.layer1(equity, deps)["rebalance_pp"])
    for dep in deps:
        s, held, sym = sigs[dep["id"]]
        info = {"regime": s["regime"], "held": held}
        if held and s["regime"] == 1 and equity > 0:
            q = us_ledger.position(dep["id"], sym)["qty"]
            mv = q * float(_close_of(sym, last) or 0)
            budget = float((plan.get(dep["id"]) or {}).get("budget") or 0)
            dev = (mv - budget) / equity * 100
            info.update(actual_pct=round(mv / equity * 100, 2), target_pct=round(budget / equity * 100, 2), dev_pp=round(dev, 2))
            st = dep_state(dep)
            if abs(dev) > rbp and budget > 0:
                st["rebalance_due"] = {"date": str(last), "dev_pp": round(dev, 2), "threshold": rbp}
                _save_state(dep, st)
                _log(dep, "INFO", f"{last} 收盘：实际仓位 {mv / equity:.1%}、目标 {budget / equity:.1%}，偏离 {dev:+.1f} 个百分点"
                                  f"（阈值 {rbp:g}）→ {nxt} 开盘后再平衡")
            elif st.get("rebalance_due"):
                st.pop("rebalance_due", None)
                _save_state(dep, st)
        out["deployments"][dep["id"]] = info
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
    if st.get("rebalance_due"):
        rd = st["rebalance_due"]
        items.append(("待再平衡", f"{rd['date']} 收盘偏离 {rd['dev_pp']:+.1f} 个百分点（阈值 {rd['threshold']:g}），下一交易日开盘后调整"))
    if dep["config"].get("pending_switch"):
        items.append(("待切换", "有持仓或未完成订单，空仓后切换到新版本"))
    if e.get("pending_offline"):
        heads.insert(0, "等待空仓后下线：不再开新仓")
    return {"headline": "；".join(heads) or "等待下一次美股开盘检查", "items": items, "calc": calc}
