"""Paper Strategy Runner / Order State Ledger / Position Reconciliation / Restart Recovery / Risk Stop Controls。

实时交易决策只来自冻结的确定性 Strategy Schema；DeepSeek 不参与下单。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd

from .. import db, research
from ..data import http
from ..engine.signals import build_frame, group_signal
from ..strategy import registry
from ..strategy.schema import validate
from .brokers import AlpacaPaperAdapter, MockBroker, OrderPermissionLocked

ACTIVE_STATES = ("running", "paused_entries")
ENTRY_MAX_DELAY = pd.Timedelta(minutes=15)   # 入场信号最长执行延迟（与回测 MAX_FILL_DELAY 一致）
STATE_CN = {"configured": "已配置（未运行）", "running": "运行中", "paused_entries": "暂停新开仓",
            "stopped": "已停止", "emergency_stopped": "紧急停止", "blocked": "风险阻断"}


def dep_label(dep_id: int | None) -> str:
    if not dep_id:
        return "系统"
    d = db.q1("SELECT d.config_json, v.name, v.version_no FROM paper_deployments d "
              "LEFT JOIN strategy_versions v ON v.id=d.version_id WHERE d.id=?", (dep_id,))
    if not d:
        return f"部署#{dep_id}"
    cfg = json.loads(d["config_json"] or "{}")
    return cfg.get("label") or f"{d['name']} v{d['version_no']}"


def event(dep_id: int | None, level: str, msg: str, is_test: bool = False) -> None:
    db.ex("INSERT INTO paper_events(deployment_id,ts,level,message,is_test) VALUES(?,?,?,?,?)",
          (dep_id, db.now_iso(), level, msg, 1 if is_test else 0))
    # 同时写入按天分割的交易日志文件（北京时间），便于事后审计
    try:
        from .. import config as _cfg
        now_bj = datetime.now(timezone(timedelta(hours=8)))
        f = _cfg.LOG_DIR / f"trading_{now_bj:%Y%m%d}.log"
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(f"{now_bj:%Y-%m-%d %H:%M:%S} [{level}] #{dep_id or '-'} {dep_label(dep_id)}"
                     f"{' [测试]' if is_test else ''} | {msg}\n")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 部署配置与门禁
# ---------------------------------------------------------------------------

def deploy_gate(version_id: int) -> tuple[bool, list[tuple[str, bool, str]]]:
    v = registry.get_version(version_id)
    checks: list[tuple[str, bool, str]] = []
    checks.append(("策略已冻结", bool(v and v["frozen"]), f"哈希 {v['schema_hash'][:12]}" if v else "版本不存在"))
    # 门禁以训练集回测为准（验证集/测试集样本短、可能无成交，仅作参考）
    b = (research.latest_backtest_for_version(version_id, kind_split="训练集")
         or research.latest_backtest_for_version(version_id)) if v else None
    checks.append(("已完成真实数据历史回测", b is not None,
                   f"{b['split_label']} 净收益 {b['metrics']['net_return_pct']:.2f}%" if b else "无（测试数据不计入）"))
    checks.append(("回测有成交", bool(b and b["metrics"]["trades"] > 0),
                   f"{b['metrics']['trades']} 笔" if b else "--"))
    val = db.q1("SELECT COUNT(*) AS n FROM holdout_touches WHERE version_id=? AND split='validation'", (version_id,))
    checks.append(("验证集检验（建议）", True, f"已运行 {val['n']} 次" if val and val["n"] else "未运行（仅提示，不阻断）"))
    ok = all(c[1] for c in checks)
    return ok, checks


def save_deployment(version_id: int, cfg: dict, broker: str) -> int:
    ok, checks = deploy_gate(version_id)
    if broker == "alpaca_paper" and not ok:
        raise ValueError("部署门禁未通过：" + "；".join(f"{n}({d})" for n, p, d in checks if not p))
    is_test = broker == "mock"
    cfg = dict(cfg)
    cfg["gate"] = [list(c) for c in checks]
    did = db.ex("INSERT INTO paper_deployments(version_id,config_json,broker,status,created_at,updated_at,is_test) "
                "VALUES(?,?,?,?,?,?,?)", (version_id, json.dumps(cfg, ensure_ascii=False), broker, "configured",
                                          db.now_iso(), db.now_iso(), 1 if is_test else 0))
    event(did, "INFO", f"保存部署配置（{broker}），版本 {version_id}", is_test)
    return did


def get_deployment(did: int) -> dict | None:
    d = db.q1("SELECT * FROM paper_deployments WHERE id=?", (did,))
    if d:
        d["config"] = json.loads(d["config_json"])
    return d


def make_broker(dep: dict):
    if dep["broker"] == "mock":
        return MockBroker(dep["id"], initial_cash=float(dep["config"].get("mock_cash", 10000)),
                          fee_bps=float(dep["config"].get("fee_bps", 25)))
    return AlpacaPaperAdapter()


def set_status(did: int, status: str) -> None:
    db.ex("UPDATE paper_deployments SET status=?, updated_at=? WHERE id=?", (status, db.now_iso(), did))


def start(did: int) -> str:
    dep = get_deployment(did)
    if dep["broker"] == "alpaca_paper" or (dep["broker"] == "alpaca_us" and not dep["is_test"]):
        if not http.paper_orders_unlocked():
            event(did, "WARN", "启动被拒绝：PAPER_ORDERS=0，Paper 订单权限锁定", False)
            raise OrderPermissionLocked("Alpaca Paper 自动交易启动门禁锁定：用户尚未授权提交任何 Paper 订单。")
    # 多个策略共用同一个 Paper 账户：每个策略有独立资金额度（budget_usd），额度之和不得超过账户权益
    if dep["broker"] == "alpaca_paper":
        others = db.q("SELECT config_json FROM paper_deployments WHERE broker='alpaca_paper' "
                      "AND status IN ('running','paused_entries') AND id<>?", (did,))
        used = sum(float(json.loads(o["config_json"]).get("budget_usd") or 0) for o in others)
        mine = float(dep["config"].get("budget_usd") or 0)
        if not mine and others and not dep["config"].get("trial"):
            raise ValueError("多个策略共用账户时，每个部署必须设置资金额度 budget_usd")
        acct = make_broker(dep).account()
        if acct is not None and mine and used + mine > acct.equity * 1.0001:
            raise ValueError(f"资金额度不足：已分配 {used:,.0f} + 本策略 {mine:,.0f} > 账户权益 {acct.equity:,.0f}")
    set_status(did, "running")
    event(did, "INFO", "启动模拟交易", bool(dep["is_test"]))
    return "running"


def pause_entries(did: int) -> None:
    if get_deployment(did)["status"] != "running":
        raise ValueError("只有运行中的部署可以暂停新开仓")
    set_status(did, "paused_entries")
    event(did, "INFO", "暂停新开仓（已有持仓按规则管理）", _is_test(did))


def stop(did: int) -> None:
    set_status(did, "stopped")
    event(did, "INFO", "停止策略：不再产生新信号；持仓保持不变（未自动平仓）", _is_test(did))


def _not_us(dep: dict, what: str) -> None:
    """美股部署和 BTC 共用一个券商账户：这些按账户 / BTC 持仓操作的按钮不能用于美股部署（会动到其他策略）。"""
    if dep["broker"] == "alpaca_us":
        raise ValueError(f"美股部署不支持「{what}」：多个策略共用账户和标的，请在策略总台下线该策略（按信号卖出后停止）")


def cancel_orders(did: int, confirmed: bool) -> str:
    if not confirmed:
        raise ValueError("撤单需要确认")
    dep = get_deployment(did)
    _not_us(dep, "撤销未成交订单")
    make_broker(dep).cancel_open_orders()
    event(did, "INFO", "撤销未成交订单", bool(dep["is_test"]))
    return "ok"


def flatten(did: int, confirmed: bool, price: float | None = None) -> str:
    if not confirmed:
        raise ValueError("平仓需要用户确认")
    dep = get_deployment(did)
    _not_us(dep, "平仓")
    br = make_broker(dep)
    if isinstance(br, MockBroker) and price:
        br.mark(price)
    br.close_position()
    event(did, "INFO", "平掉模拟持仓（用户确认）", bool(dep["is_test"]))
    return "ok"


def emergency_stop(did: int) -> None:
    dep = get_deployment(did)
    set_status(did, "emergency_stopped")
    if dep["broker"] == "alpaca_us":
        event(did, "ERROR", "紧急停止：美股策略已停止，不再下新单；持仓未自动平仓，未成交订单当天收盘自动失效（DAY 单）",
              bool(dep["is_test"]))
        return
    try:
        make_broker(dep).cancel_open_orders()
        msg = "紧急停止：已停止策略并撤销未成交订单；持仓未自动平仓"
    except OrderPermissionLocked as e:
        msg = f"紧急停止：策略已停止；{e}"
    event(did, "ERROR", msg, bool(dep["is_test"]))


def _is_test(did: int) -> bool:
    d = db.q1("SELECT is_test FROM paper_deployments WHERE id=?", (did,))
    return bool(d and d["is_test"])


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class PaperRunner:
    """确定性执行一步：读取已完成K线 → 计算信号 → 对账 → 风控 → 通过订单台账下单。"""

    def __init__(self, did: int, max_data_age_min: float = 20.0):
        self.dep = get_deployment(did)
        self.did = did
        self.broker = make_broker(self.dep)
        v = registry.get_version(self.dep["version_id"])
        self.version = v
        self.schema, errs = validate(v["schema"])
        if errs:
            raise ValueError("部署版本校验失败")
        self.tf = int(self.dep["config"].get("signal_tf") or self.schema.signal_tf or 15)
        self.aux = self.dep["config"].get("aux_tf") or self.schema.aux_tf
        # 实盘模拟：只看最后一根 5 分钟K线的新鲜度；回放测试时用相同规则
        self.max_age = timedelta(minutes=max_data_age_min)
        self.is_test = bool(self.dep["is_test"])
        self._last_stale = None
        self.recover()

    # 重启恢复：根据台账与券商状态对齐
    def recover(self) -> None:
        pending = db.q("SELECT * FROM paper_orders WHERE deployment_id=? AND status IN ('new','submitted')", (self.did,))
        for o in pending:
            db.ex("UPDATE paper_orders SET status='unknown_after_restart', updated_at=? WHERE id=?",
                  (db.now_iso(), o["id"]))
            event(self.did, "WARN", f"重启恢复：订单 {o['client_order_id']} 状态未知，已阻止新增风险直到对账", self.is_test)

    def ledger_qty(self) -> float:
        rows = db.q("SELECT side, filled_qty FROM paper_orders WHERE deployment_id=? AND status='filled'", (self.did,))
        q = 0.0
        for r in rows:
            q += r["filled_qty"] if r["side"] == "buy" else -r["filled_qty"]
        return max(q, 0.0)

    def ledger_qty_all(self) -> float:
        """同一券商账户下所有部署（含已停止）的台账净持仓合计，用于与券商实际持仓对账。"""
        if self.broker.name != "alpaca_paper":
            return self.ledger_qty()  # MockBroker 每个部署独立账户
        rows = db.q("SELECT o.side, o.filled_qty FROM paper_orders o JOIN paper_deployments d ON d.id=o.deployment_id "
                    "WHERE d.broker=? AND o.status='filled'", (self.broker.name,))
        q = sum(r["filled_qty"] if r["side"] == "buy" else -r["filled_qty"] for r in rows)
        try:   # 「我的交易系统」也可能持有 BTC（记在通用台账 us_ledger，品种 BTC/USD）
            from . import us_ledger
            q += us_ledger.symbol_totals().get("BTC/USD", 0.0)
        except Exception:  # noqa: BLE001
            pass
        return max(q, 0.0)

    def last_entry(self) -> dict | None:
        return db.q1("SELECT filled_price, created_at FROM paper_orders WHERE deployment_id=? AND side='buy' "
                     "AND status='filled' ORDER BY id DESC LIMIT 1", (self.did,))

    def last_entry_price(self) -> float | None:
        r = self.last_entry()
        return r["filled_price"] if r else None

    def _note(self, level: str, msg: str) -> None:
        """同一提示不重复刷屏：只在状态变化时写日志。"""
        key = f"paper_lastnote::{self.did}"
        if db.get_setting(key) != msg:
            db.set_setting(key, msg)
            event(self.did, level, msg, self.is_test)

    def sync_orders(self) -> int:
        """同步未终结订单的真实状态（Alpaca 以 client_order_id 查询）。返回仍未终结的订单数。"""
        pend = db.q("SELECT * FROM paper_orders WHERE deployment_id=? AND status IN "
                    "('new','submitted','unknown_after_restart')", (self.did,))
        if not pend or not hasattr(self.broker, "get_order"):
            return 0
        open_n = 0
        for o in pend:
            info = self.broker.get_order(o["client_order_id"])
            if info is None:
                if o["status"] == "new":
                    db.ex("UPDATE paper_orders SET status='not_submitted', updated_at=? WHERE id=?",
                          (db.now_iso(), o["id"]))
                    self._note("WARN", f"订单 {o['client_order_id']} 未到达券商，已标记为未提交")
                else:
                    open_n += 1
                continue
            st = info.get("status")
            if st == "filled":
                db.ex("UPDATE paper_orders SET status='filled', filled_qty=?, filled_price=?, raw_json=?, updated_at=? "
                      "WHERE id=?", (float(info.get("filled_qty") or 0), float(info.get("filled_avg_price") or 0),
                                     json.dumps(info, default=str)[:4000], db.now_iso(), o["id"]))
                event(self.did, "INFO", f"订单 {o['client_order_id']} 已成交 {info.get('filled_qty')} @ "
                      f"{info.get('filled_avg_price')}", self.is_test)
            elif st in ("canceled", "expired", "rejected"):
                db.ex("UPDATE paper_orders SET status=?, raw_json=?, updated_at=? WHERE id=?",
                      (st, json.dumps(info, default=str)[:4000], db.now_iso(), o["id"]))
                event(self.did, "WARN", f"订单 {o['client_order_id']} 状态 {st}", self.is_test)
            else:
                db.ex("UPDATE paper_orders SET status='submitted', updated_at=? WHERE id=?", (db.now_iso(), o["id"]))
                open_n += 1
        return open_n

    def _order(self, side: str, qty: float, signal_ts: str, ref_price: float) -> dict | None:
        coid = f"aq{self.did}-{side}-{pd.Timestamp(signal_ts).strftime('%Y%m%d%H%M')}"
        prev = db.q("SELECT status FROM paper_orders WHERE deployment_id=? AND client_order_id LIKE ?",
                    (self.did, coid + "%"))
        if prev and all(p["status"] in ("rejected", "blocked_locked", "not_submitted", "canceled", "expired")
                        for p in prev) and side == "sell":
            coid = f"{coid}-r{len(prev)}"  # 未成交的卖出（如止损）允许重试，成交/未知状态仍受重复保护
        try:
            oid = db.ex("INSERT INTO paper_orders(deployment_id,client_order_id,broker,side,qty,status,signal_ts,"
                        "created_at,updated_at,is_test) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (self.did, coid, self.broker.name, side, qty, "new", signal_ts, db.now_iso(), db.now_iso(),
                         1 if self.is_test else 0))
        except Exception:
            self._note("WARN", f"重复订单保护：{coid} 已存在，跳过")
            return None
        try:
            pos_before = (self.broker.position() or {}).get("qty", 0.0) if self.broker.name == "alpaca_paper" else None
            res = self.broker.submit_order(coid, side, qty, ref_price)
            if res.get("status") == "filled":
                fq = float(res.get("filled_qty") or qty)
                if pos_before is not None:
                    after = (self.broker.position() or {}).get("qty")
                    if after is not None:
                        delta = after - pos_before if side == "buy" else pos_before - after
                        if delta > 0 and abs(delta - fq) <= fq * 0.01:
                            fq = delta  # 加密货币手续费从所得币中扣除：记录实际到手/实际减少的数量
                fp = float(res.get("filled_avg_price") or res.get("price") or ref_price)
                db.ex("UPDATE paper_orders SET status='filled', filled_qty=?, filled_price=?, raw_json=?, updated_at=? "
                      "WHERE id=?", (fq, fp, json.dumps(res, default=str)[:4000], db.now_iso(), oid))
                event(self.did, "INFO", f"{'买入' if side == 'buy' else '卖出'} {fq:.6f} BTC @ {fp:,.2f}"
                      f"（{self.broker.name}，{coid}）", self.is_test)
            else:
                db.ex("UPDATE paper_orders SET status='submitted', raw_json=?, updated_at=? WHERE id=?",
                      (json.dumps(res, default=str)[:4000], db.now_iso(), oid))
                event(self.did, "INFO", f"已提交{'买入' if side == 'buy' else '卖出'}订单 {coid}，等待成交", self.is_test)
            return res
        except OrderPermissionLocked as e:
            db.ex("UPDATE paper_orders SET status='blocked_locked', updated_at=? WHERE id=?", (db.now_iso(), oid))
            event(self.did, "WARN", str(e), self.is_test)
        except Exception as e:  # noqa: BLE001
            db.ex("UPDATE paper_orders SET status='rejected', raw_json=?, updated_at=? WHERE id=?",
                  (str(e)[:300], db.now_iso(), oid))
            event(self.did, "ERROR", f"订单被拒绝：{e}", self.is_test)
        return None

    def _reclaim_cut(self, my_qty: float, price: float, now: datetime) -> dict | None:
        """美股 ETF 收回临时借出的额度：卖出借用期间所开仓位的「借用比例」（1 − 收回后额度 / 收回前额度）。
        借出之前就开的仓没有用到借来的额度，不卖；没有持仓或卖出额不足 10 USD 时直接记为完成。返回本步动作或 None。"""
        try:
            from . import etf_trend
            cut = etf_trend.pending_cut(self.did)
        except Exception:  # noqa: BLE001
            return None
        if not cut:
            return None
        if my_qty <= 1e-9:
            etf_trend.mark_cut_done(self.did, 0.0, "没有持仓，无需减仓")
            return None
        le = self.last_entry()
        since = cut.get("lent_since")
        if since and le and pd.Timestamp(le["created_at"]) < pd.Timestamp(since):
            etf_trend.mark_cut_done(self.did, 0.0, "持仓在借出额度之前开的，没有用到借来的额度，无需减仓")
            return None
        qty = float(f"{my_qty * float(cut['frac']):.6f}")
        if qty * price < 10:
            etf_trend.mark_cut_done(self.did, 0.0, f"按借用比例 {cut['frac']:.1%} 应卖 {qty:.6f} BTC，不足 10 USD，不卖")
            return None
        ts = pd.Timestamp(now).floor("min").isoformat()
        res = self._order("sell", qty, ts, price)
        why = (f"美股 ETF 买入优先：额度 {cut['before']:,.0f}→{cut['after']:,.0f} USD，按借用比例 {cut['frac']:.1%} "
               f"卖出 {qty:.6f} BTC 归还额度")
        if res is None:
            return {"action": "blocked", "reason": why + "（卖单未成功，下一轮重试）"}
        etf_trend.mark_cut_done(self.did, qty, why)
        event(self.did, "INFO", why, self.is_test)
        return {"action": "sell", "reason": why}

    def _position_cap(self, my_qty: float, price: float, equity: float, now: datetime) -> dict | None:
        """风控上限：本策略持仓市值超过账户权益的 position_cap_pct（默认 50%）→ 卖出超出部分。额度调整本身不强制卖出。"""
        if my_qty <= 1e-9 or not equity:
            return None
        from .. import allocation
        cap = allocation.position_cap(equity)
        val = my_qty * price
        if val <= cap * 1.005:
            return None
        qty = float(f"{(val - cap) / price:.6f}")
        if qty * price < 10:
            return None
        why = (f"风控上限：持仓 {val:,.0f} USD 超过账户权益 {equity:,.0f} 的 "
               f"{allocation.get_config()['position_cap_pct']:g}%，卖出 {qty:.6f} BTC 减到上限")
        res = self._order("sell", qty, pd.Timestamp(now).floor("min").isoformat(), price)
        if res is None:
            return {"action": "blocked", "reason": why + "（卖单未成功，下一轮重试）"}
        event(self.did, "WARN", why, self.is_test)
        return {"action": "sell", "reason": why}

    def _rhythm_exit(self, now: datetime, reason: str, pnl_est: float) -> None:
        try:
            from .. import rhythm
            msg = rhythm.live_on_exit(self.did, now, reason, pnl_est)
            if msg:
                event(self.did, "INFO", f"交易节奏：本笔{'盈利' if pnl_est > 0 else '亏损'}约 {pnl_est:,.0f} USD → {msg}",
                      self.is_test)
                if "连亏" in msg and not self.is_test:
                    from .. import notify
                    notify.event("loss_streak", f"连亏刹车：{dep_label(self.did)}",
                                 f"{msg}\n期间该策略不开新仓，已有持仓不受影响。")
        except Exception:  # noqa: BLE001
            pass

    def step(self, df5: pd.DataFrame, now: datetime) -> dict:
        dep = get_deployment(self.did)
        status = dep["status"]
        out = {"status": status, "action": "none", "reason": "", "at": pd.Timestamp(now).isoformat()}
        if status not in ACTIVE_STATES:
            out["reason"] = "部署未运行"
            return out
        # 1) 行情新鲜度：最后一根 5 分钟K线必须足够新
        if df5.empty or (pd.Timestamp(now) - df5["ts"].max()) > self.max_age:
            out.update(action="blocked", reason="行情过期/缺失，阻止新增风险敞口")
            self._note("WARN", out["reason"])
            return out
        # 2) 未终结订单同步
        if self.sync_orders() > 0:
            out.update(action="blocked", reason="存在未终结订单，等待成交/撤销后再决策")
            self._note("INFO", out["reason"])
            return out
        # 3) 账户状态与持仓对账（Alpaca 加密货币手续费从所得币中扣除，允许 0.6% 差异）
        acct = self.broker.account()
        pos = self.broker.position()
        if acct is None or pos is None:
            out.update(action="blocked", reason="账户状态未知（网络或接口异常），阻止新增风险敞口")
            self._note("WARN", out["reason"])
            return out
        lq_all = self.ledger_qty_all()
        tol = max(1e-6, lq_all * 0.006)
        if abs(pos["qty"] - lq_all) > tol:
            out.update(action="blocked", reason=f"持仓对账不一致（券商 {pos['qty']:.8f} / 全部策略台账 {lq_all:.8f}），暂停新开仓")
            self._note("ERROR", out["reason"])
            if status == "running":
                set_status(self.did, "paused_entries")
            return out
        db.ex("UPDATE paper_orders SET status='reconciled' WHERE deployment_id=? AND status='unknown_after_restart'",
              (self.did,))

        frame = build_frame(df5, self.schema, self.tf, self.aux, data_end=pd.Timestamp(now))
        if len(frame) < 3:
            out["reason"] = "已完成K线不足"
            return out
        ent = group_signal(frame, self.schema.entry)
        ex = group_signal(frame, self.schema.exit)
        i = len(frame) - 1
        sig_ts = frame["close_time"].iloc[i].isoformat()
        last = df5.iloc[-1]
        price = float(last["close"])
        out["last_signal_bar"] = sig_ts
        if isinstance(self.broker, MockBroker):
            self.broker.mark(price)
        my_qty = min(self.ledger_qty(), pos["qty"])
        out["position_qty"] = my_qty
        if self.broker.name == "alpaca_paper":   # 美股 ETF 买入优先：收回期间按借用比例减仓（下一个美股开盘前）
            cut = self._reclaim_cut(my_qty, price, now)
            if cut:
                out.update(cut)
                return out
            capped = self._position_cap(my_qty, price, acct.equity, now)
            if capped:
                out.update(capped)
                return out
        if my_qty > 1e-9:
            le = self.last_entry()
            ep = (le or {}).get("filled_price") or price
            reason, stop_px = None, None
            if self.schema.stop_loss_pct:
                stop_px = ep * (1 - self.schema.stop_loss_pct / 100)
            if self.schema.trailing and le:
                since = pd.Timestamp(le["created_at"])
                prior = df5[(df5["ts"] >= since) & (df5["ts"] < df5["ts"].max())]
                peak = max([ep] + ([float(prior["high"].max())] if len(prior) else []))
                if peak >= ep * (1 + self.schema.trailing.activate_pct / 100):
                    tpx = peak * (1 - self.schema.trailing.trail_pct / 100)
                    stop_px = max(stop_px or 0, tpx)
                    out["trailing_stop"] = round(tpx, 2)
            if ex[i]:
                reason = "退出规则"
            elif stop_px and float(last["low"]) <= stop_px:
                reason = "止损/移动止损"
            elif self.schema.take_profit_pct and float(last["high"]) >= ep * (1 + self.schema.take_profit_pct / 100):
                reason = "止盈"
            if reason:
                bar_ts = sig_ts if reason == "退出规则" else pd.Timestamp(last["ts"]).isoformat()
                res = self._order("sell", my_qty, bar_ts, price)
                out.update(action="sell", reason=reason)
                if res is not None:
                    self._rhythm_exit(now, reason, (price - ep) * my_qty - (price + ep) * my_qty * 0.0025)
            else:
                out["reason"] = "持仓中，未触发退出"
            return out
        if status == "paused_entries":
            out["reason"] = "暂停新开仓"
            return out
        if ent[i] and not ex[i]:
            # 与回测一致：信号K线收盘后 15 分钟内没能执行（服务中断/重启等）则放弃该入场信号，
            # 不在远离信号的时间和价格追单。退出信号不受此限制（降低风险永远优先）。
            age = pd.Timestamp(now) - pd.Timestamp(frame["close_time"].iloc[i])
            if age > ENTRY_MAX_DELAY:
                out["reason"] = (f"入场信号已过期（K线收盘已 {int(age.total_seconds() // 60)} 分钟，超过 "
                                 f"{int(ENTRY_MAX_DELAY.total_seconds() // 60)} 分钟），放弃本次入场")
                if self._last_stale != sig_ts:
                    self._note("WARN", out["reason"])
                    self._last_stale = sig_ts
                    # 记下最近一次“信号过期未追单”，策略总台 / 模拟盘页面展示
                    db.set_setting(f"stale_signal::{self.did}", {"signal_bar": sig_ts, "age_min": int(age.total_seconds() // 60),
                                                                 "limit_min": int(ENTRY_MAX_DELAY.total_seconds() // 60),
                                                                 "at": pd.Timestamp(now).isoformat(), "reason": out["reason"]})
                return out
            # 交易节奏：冷却 / 连亏刹车 / 每日上限 → 跳过本次入场；波动率定仓 → 缩小仓位
            try:
                from .. import rhythm
                block, vmult, _info = rhythm.live_gate(self.did, now, df5)
            except Exception as e:  # noqa: BLE001  节奏模块异常时保守处理：不开新仓
                block, vmult = f"交易节奏检查异常：{type(e).__name__}", 1.0
            if block:
                out.update(action="none", reason=f"交易节奏：{block}，跳过本次入场信号")
                self._note("INFO", out["reason"])
                return out
            if self.broker.name == "alpaca_paper":
                try:   # 美股 ETF 正在收回临时借给 BTC 的额度：BTC 暂停新开仓，直到额度归还
                    from . import etf_trend
                    if etf_trend.reclaim_active():
                        out.update(action="blocked", reason="美股 ETF 发出买入信号，正在收回临时借给 BTC 的额度：暂停新开仓，"
                                                            "直到额度归还（持仓照常按规则卖出）")
                        self._note("INFO", out["reason"])
                        return out
                except Exception:  # noqa: BLE001
                    pass
            if self.broker.name == "alpaca_paper" and "budget_usd" in dep["config"] \
                    and float(dep["config"]["budget_usd"] or 0) <= 0:
                out.update(action="blocked", reason="资金额度为 0（等待重新分配），不开新仓")
                self._note("WARN", out["reason"])
                return out
            budget_pct = float(dep["config"].get("alloc_pct", 100)) * self.schema.alloc_pct / 100
            usd = acct.cash * budget_pct / 100 * 0.99
            if dep["config"].get("budget_usd"):
                usd = float(dep["config"]["budget_usd"]) * 0.99
            cap = dep["config"].get("max_position_usd")
            if cap:
                usd = min(usd, float(cap))
            if vmult < 0.999:
                usd *= vmult
                out["rhythm_size"] = round(vmult, 3)
            # 下单前现金检查：不超过（现金 − 其他未成交买单占用）× 98%，不使用券商保证金；不足时缩量并通知
            from . import cash_guard
            reserved = cash_guard.reserved_cash(None, exclude="btc") if self.broker.name == "alpaca_paper" else 0.0
            usd, short = cash_guard.check_buy(usd, acct.cash, who=dep_label(self.did), symbol="BTC/USD", reserved=reserved,
                                              dedupe=f"cash-btc-{self.did}-{pd.Timestamp(now):%Y-%m-%d}"
                                              if not self.is_test else f"cash-test-{self.did}-{sig_ts}")
            if short:
                out["cash_note"] = short
                self._note("WARN", short)
            qty = float(f"{usd / price:.6f}")
            if qty * price < 10:
                out.update(action="blocked", reason=short or "可用资金不足最小下单额")
                return out
            res = self._order("buy", qty, sig_ts, price)
            out.update(action="buy", reason="入场规则" + (f"（波动率定仓 {vmult:.0%}）" if vmult < 0.999 else ""))
            if res is not None:
                try:
                    from .. import rhythm
                    rhythm.live_on_entry(self.did, now)
                except Exception:  # noqa: BLE001
                    pass
        else:
            out["reason"] = "空仓，等待入场信号"
        return out


def mock_replay(did: int, df5: pd.DataFrame, n_steps: int = 300, restart_at: int | None = None) -> dict:
    """使用历史（测试）数据逐根回放，验证 Mock Broker 的启动/订单/停止/恢复。所有记录 is_test=1。"""
    dep = get_deployment(did)
    if dep["broker"] != "mock":
        raise ValueError("回放测试只允许 MockBroker")
    runner = PaperRunner(did)
    ts = df5["ts"]
    start_i = max(len(df5) - n_steps, 300)
    actions = {"buy": 0, "sell": 0, "blocked": 0, "none": 0, "restarts": 0}
    for k, i in enumerate(range(start_i, len(df5))):
        if restart_at is not None and k == restart_at:
            runner = PaperRunner(did)  # 模拟服务重启
            actions["restarts"] += 1
            event(did, "INFO", "模拟服务重启，Runner 从数据库恢复", True)
        now = ts.iloc[i] + pd.Timedelta(minutes=5)
        r = runner.step(df5.iloc[max(0, i + 1 - 15000): i + 1], now.to_pydatetime())
        actions[r["action"]] = actions.get(r["action"], 0) + 1
    return actions


def readiness(version_id: int | None) -> list[tuple[str, bool, str]]:
    """Alpaca Paper 只读连接检查（11.3）。不会调用任何写接口。"""
    from ..data import alpaca as _al
    ad = AlpacaPaperAdapter()
    out: list[tuple[str, bool, str]] = []
    acct = ad.account()
    out.append(("账户可读", acct is not None, f"状态 {acct.status}" if acct else "无法读取（检查 .env 凭证/网络）"))
    out.append(("模拟余额可读", acct is not None, f"权益 {acct.equity:,.2f} USD" if acct else "--"))
    rules = ad.asset_rules() if acct else None
    out.append(("BTC/USD 订单规则", bool(rules and rules.get("tradable")),
                f"最小下单 {rules.get('min_order_size')}，步长 {rules.get('min_trade_increment')}" if rules else "--"))
    perm = bool(acct and not acct.trading_blocked and (acct.crypto_status in (None, "ACTIVE")))
    out.append(("交易权限完整（账户侧）", perm,
                f"trading_blocked={acct.trading_blocked}, crypto_status={acct.crypto_status}" if acct else "--"))
    q = None
    try:
        q = _al.latest_quote()
    except Exception as e:  # noqa: BLE001
        q = None
    fresh = False
    if q and q.get("ts"):
        age = (datetime.now(timezone.utc) - pd.Timestamp(q["ts"]).to_pydatetime()).total_seconds()
        fresh = age < 300
        out.append(("行情及时", fresh, f"最新报价 {age:.0f} 秒前，bid {q['bid']} / ask {q['ask']}"))
    else:
        out.append(("行情及时", False, "无法获取最新报价"))
    if version_id:
        ok, checks = deploy_gate(version_id)
        out.append(("策略通过部署门禁", ok, "；".join(f"{n}:{'✓' if p else '✗'}" for n, p, _ in checks)))
    out.append(("本地订单权限", False, "PAPER_ORDERS=0 锁定（需未来单独授权），LIVE_ORDERS=0 永久禁用"))
    return out
