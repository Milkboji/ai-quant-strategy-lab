"""Alpaca Paper 实时运行循环（后台线程，随服务启动）。

每 60 秒：增量更新 5 分钟行情 → 对每个运行中的 Alpaca Paper 部署执行一次 PaperRunner.step。
交易决策只来自冻结的确定性策略；DeepSeek 不参与。Live 交易永不启用。
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import pandas as pd

from .. import db
from ..data import http
from ..data import market_data as md
from . import runner as pr

log = logging.getLogger("aiquant.paper")
INTERVAL_SEC = 60
LOOKBACK_DAYS = 120
_started = False
_runners: dict[int, pr.PaperRunner] = {}
_lock = threading.Lock()


def _active() -> list[dict]:
    return db.q("SELECT * FROM paper_deployments WHERE broker='alpaca_paper' "
                "AND status IN ('running','paused_entries') ORDER BY id")


def tick(now: datetime | None = None) -> list[dict]:
    deps = _active()
    if not deps:
        return []
    now = now or datetime.now(timezone.utc)
    results = []
    try:
        md.update_from_alpaca(now - timedelta(days=LOOKBACK_DAYS), now)
    except Exception as e:  # noqa: BLE001  行情失败时 step 会因数据过期而阻止新增风险
        log.warning("行情更新失败: %s", e)
    m = md.load_master()
    df5 = m[m["ts"] >= pd.Timestamp(now - timedelta(days=LOOKBACK_DAYS))]
    # 缓存会保守地截掉最近 10 分钟；实时决策另取最近 3 小时已走完的 5 分钟K线，减少信号延迟
    try:
        from ..data import alpaca
        fresh = alpaca.fetch_bars_5m("BTC/USD", now - timedelta(hours=3), now)
        fresh = fresh[fresh["ts"] + pd.Timedelta(minutes=5) <= pd.Timestamp(now)]
        if len(fresh):
            df5 = pd.concat([df5, fresh[df5.columns]]).drop_duplicates("ts", keep="last").sort_values("ts")
    except Exception as e:  # noqa: BLE001
        log.warning("实时5分钟K线获取失败: %s", e)
    df5 = df5.reset_index(drop=True)
    if len(df5):   # 最新 BTC 价格：估算未成交买单占用的现金（下单前现金检查用）
        db.set_setting("btc_last_price", {"price": float(df5["close"].iloc[-1]), "at": db.now_iso()})
    try:   # 恢复后第一次检查：列出中断期间出现过的 BTC 入场信号（不补单）
        from .. import uptime
        uptime.catchup_signals(df5)
    except Exception:  # noqa: BLE001
        log.error("catchup signals error: %s", traceback.format_exc())
    try:   # 自动进化产生的新版本：空仓时切换
        from .. import evolve
        if evolve.apply_all_pending():
            deps = _active()
    except Exception:  # noqa: BLE001
        log.error("evolve switch error: %s", traceback.format_exc())
    try:   # 策略总台：待下线的策略空仓后停止
        from .. import console
        if console.apply_pending_offline():
            deps = _active()
    except Exception:  # noqa: BLE001
        log.error("console offline error: %s", traceback.format_exc())
    for d in deps:
        try:
            run = _runners.get(d["id"])
            if run is None or run.dep["version_id"] != d["version_id"]:
                run = _runners[d["id"]] = pr.PaperRunner(d["id"])  # 首次/重启时执行恢复与对账
            r = run.step(df5, now)
        except Exception as e:  # noqa: BLE001
            r = {"action": "error", "reason": f"{type(e).__name__}: {e}"[:300], "at": now.isoformat()}
            pr.event(d["id"], "ERROR", f"运行循环异常：{r['reason']}")
            log.error("paper tick error: %s", traceback.format_exc())
        prev = db.get_setting(f"paper_loop::{d['id']}") or {}
        if r.get("last_signal_bar") and r.get("last_signal_bar") != prev.get("last_signal_bar") \
                and r.get("action") in ("none",):
            bar_bj = (pd.Timestamp(r["last_signal_bar"]) + pd.Timedelta(hours=8)).strftime("%m-%d %H:%M")
            pr.event(d["id"], "INFO", f"K线收盘检查（{bar_bj} 北京时间）：{r.get('reason') or '无动作'}"
                     + (f"，移动止损价 {r['trailing_stop']}" if r.get("trailing_stop") else ""))
        db.set_setting(f"paper_loop::{d['id']}", r)
        results.append(r)
    try:   # 长期亏损自动暂停 / 恢复
        from .. import evolve
        evolve.health_check(float(df5["close"].iloc[-1]) if len(df5) else None)
    except Exception:  # noqa: BLE001
        log.error("health check error: %s", traceback.format_exc())
    try:   # 2 倍杠杆模拟账户：跟随核心策略成交本地记账
        from . import leverage_shadow
        leverage_shadow.update(float(df5["close"].iloc[-1]) if len(df5) else None, now)
    except Exception:  # noqa: BLE001
        log.error("leverage shadow error: %s", traceback.format_exc())
    return results


def _loop() -> None:
    while True:
        t0 = time.time()
        try:   # 中断检测（距上次心跳超过 3 分钟 → 记录中断、写明错过的检查、本轮立即补检查）+ 健康检查文件
            from .. import uptime
            uptime.beat()
        except Exception:  # noqa: BLE001
            log.error("uptime beat error: %s", traceback.format_exc())
        try:
            if http.paper_orders_unlocked():
                tick()
            try:   # 美股 ETF 趋势择时（QQQ 均线 → QLD）；下单锁未开时只记录「为什么没有动作」
                from . import etf_trend
                etf_trend.step()
            except Exception:  # noqa: BLE001
                log.error("etf trend error: %s", traceback.format_exc())
            try:   # 美股收盘后确认下一交易日信号：买入信号 → 收回借给 BTC 的额度（BTC 开盘前按借用比例减仓）
                from . import etf_trend
                etf_trend.after_close()
            except Exception:  # noqa: BLE001
                log.error("etf after-close error: %s", traceback.format_exc())
            try:   # 杠杆告警：账户总持仓市值 ÷ 权益 > 100% 或现金为负（每 5 分钟检查一次）
                from . import cash_guard
                cash_guard.maybe_exposure_check()
            except Exception:  # noqa: BLE001
                log.error("exposure check error: %s", traceback.format_exc())
            try:   # 美股交易日历（节假日 / 半日市），每周从 Alpaca 刷新一次
                from .. import market
                market.refresh_calendar()
            except Exception:  # noqa: BLE001
                log.error("calendar refresh error: %s", traceback.format_exc())
            try:   # 一次性策略升级（登记为挑战者，走系统自己的检验）
                from .. import upgrades
                if upgrades.run_pending():
                    db.set_setting("challenger_eval_hour", "")   # 立即评估新登记的挑战者
            except Exception:  # noqa: BLE001
                log.error("upgrades error: %s", traceback.format_exc())
            try:   # 挑战者每小时用最新行情评估一次，满足条件自动晋级部署
                hr = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
                if (db.get_setting("challenger_eval_hour") or "") != hr:
                    db.set_setting("challenger_eval_hour", hr)
                    from .. import challenger
                    challenger.evaluate_all()
            except Exception:  # noqa: BLE001
                log.error("challenger error: %s", traceback.format_exc())
            try:   # 交易节奏：每天一次长周期体检 → 暂停/恢复 → 重新分配额度（后台线程）
                from .. import rhythm
                rhythm.maybe_daily()
            except Exception:  # noqa: BLE001
                log.error("rhythm daily error: %s", traceback.format_exc())
            try:
                _snapshot_account()
            except Exception:  # noqa: BLE001
                log.error("snapshot error: %s", traceback.format_exc())
            _maybe_evolve()
            try:   # 通知：重试发件箱 + 每日运行摘要
                from .. import notify
                notify.flush()
                notify.maybe_daily()
            except Exception:  # noqa: BLE001
                log.error("notify error: %s", traceback.format_exc())
        except Exception:  # noqa: BLE001
            log.error("paper loop error: %s", traceback.format_exc())
        db.set_setting("paper_loop_heartbeat", db.now_iso())
        time.sleep(max(5, INTERVAL_SEC - (time.time() - t0)))


def _snapshot_account() -> None:
    """每小时记录一次 Paper 账户权益，供总览页资产曲线使用。"""
    hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
    with db.tx() as c:
        c.execute("CREATE TABLE IF NOT EXISTS account_snapshots (ts TEXT PRIMARY KEY, equity REAL, cash REAL)")
        if c.execute("SELECT 1 FROM account_snapshots WHERE ts=?", (hour,)).fetchone():
            return
    from ..data import alpaca
    code, b = alpaca.paper_get("/v2/account")
    if code == 200 and isinstance(b, dict) and b.get("equity"):
        db.ex("INSERT OR IGNORE INTO account_snapshots(ts,equity,cash) VALUES(?,?,?)",
              (hour, float(b["equity"]), float(b.get("cash") or 0)))


def _maybe_evolve() -> None:
    """按设定周期自动提交一轮策略进化（任务队列后台执行，不阻塞交易循环）。"""
    try:
        from .. import evolve
        if _active() and evolve.due():
            evolve.submit("定时")
    except Exception:  # noqa: BLE001
        log.error("evolve schedule error: %s", traceback.format_exc())


def ensure_loop() -> None:
    global _started
    import os
    if os.environ.get("AIQUANT_DISABLE_PAPER_LOOP") == "1":
        return
    with _lock:
        if _started:
            return
        threading.Thread(target=_loop, daemon=True, name="aiquant-paper-loop").start()
        _started = True
