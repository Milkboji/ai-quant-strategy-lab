"""导出在线只读看板所需的数据快照（JSON）。

只读：读取本地 SQLite 数据库与行情缓存，不访问任何网络、不读取密钥。
用法：python scripts/export_dashboard.py --db data/aiquant.db --cache data/market_cache/alpaca_us_BTCUSD_5m.csv.gz --out <目录>
输出：account.json / strategies.json / events.json / orders.json / price.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

START_EQUITY = 100_000.0  # Alpaca Paper 账户初始资金
BJ = timedelta(hours=8)
TF_CN = {15: "15分钟", 60: "1小时", 240: "4小时", 1440: "日线"}
STATUS_CN = {"running": "运行中", "paused_entries": "暂停开仓", "stopped": "已停止",
             "risk_stopped": "风控停止", "draft": "草稿"}
REASON_CN = {"none": "观望", "buy": "买入", "sell": "卖出", "error": "异常", "blocked": "受阻"}


def bj(ts: str | None) -> str | None:
    if not ts:
        return None
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return (t.tz_convert("UTC") + BJ).strftime("%Y-%m-%d %H:%M")


def setting(c, key):
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=3)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(a.db)
    c.row_factory = sqlite3.Row

    px = pd.read_csv(a.cache, usecols=["ts", "close"])
    px["ts"] = pd.to_datetime(px["ts"], utc=True)
    last_ts = px["ts"].max()
    last_price = float(px.loc[px["ts"] == last_ts, "close"].iloc[0])
    win = px[px["ts"] >= last_ts - pd.Timedelta(days=a.days)].set_index("ts")["close"].resample("15min").last().dropna()
    price = [{"ts": (t + BJ).strftime("%Y-%m-%dT%H:%M"), "close": round(float(v), 2)} for t, v in win.items()]

    deps = c.execute("SELECT * FROM paper_deployments WHERE broker='alpaca_paper' AND is_test=0 ORDER BY id").fetchall()
    labels = {}
    strategies, orders_out = [], []
    for d in deps:
        cfg = json.loads(d["config_json"])
        v = c.execute("SELECT * FROM strategy_versions WHERE id=?", (d["version_id"],)).fetchone()
        name = cfg.get("label") or f"{v['name']} v{v['version_no']}"
        labels[d["id"]] = name
        od = c.execute("SELECT * FROM paper_orders WHERE deployment_id=? AND is_test=0 ORDER BY id", (d["id"],)).fetchall()
        filled = [o for o in od if o["status"] == "filled"]
        qty = sum(o["filled_qty"] * (1 if o["side"] == "buy" else -1) for o in filled)
        cash_flow = sum((-1 if o["side"] == "buy" else 1) * o["filled_qty"] * (o["filled_price"] or 0) for o in filled)
        qty = max(qty, 0.0)
        for o in od:
            orders_out.append({"order_id": o["id"], "time_bj": bj(o["created_at"]), "strategy": name,
                               "side": "买入" if o["side"] == "buy" else "卖出", "qty_btc": o["filled_qty"] or o["qty"],
                               "price_usd": o["filled_price"], "status": o["status"]})
        last = setting(c, f"paper_loop::{d['id']}") or {}
        bt = c.execute("SELECT split_label, metrics_json FROM backtests WHERE version_id=? AND is_test=0 "
                       "AND split_label='训练集' ORDER BY id DESC LIMIT 1", (d["version_id"],)).fetchone()
        m = json.loads(bt["metrics_json"]) if bt else {}
        tf = cfg.get("signal_tf")
        strategies.append({
            "deployment": d["id"], "strategy": name,
            "group": "实时预设" if v["source"] == "preset" else "研究策略",
            "timeframe": TF_CN.get(tf, f"{tf}分钟"), "status": STATUS_CN.get(d["status"], d["status"]),
            "budget_usd": cfg.get("budget_usd"), "position_btc": round(qty, 6),
            "position_usd": round(qty * last_price, 2), "pnl_usd": round(cash_flow + qty * last_price, 2),
            "orders": len(od), "filled": len(filled),
            "last_action": REASON_CN.get(last.get("action"), last.get("action") or "—"),
            "last_reason": ("已自动暂停新开仓：" + cfg["auto_pause"]["reason"]) if cfg.get("auto_pause")
            else (last.get("reason") or "—"), "last_check_bj": bj(last.get("at")),
            "last_bar_bj": bj(last.get("last_signal_bar")),
            "backtest_return_pct": round(m["net_return_pct"], 2) if m else None,
            "backtest_trades": m.get("trades"),
            "backtest_maxdd_pct": round(m["max_drawdown_pct"], 2) if m else None,
        })

    ev = c.execute("SELECT * FROM paper_events WHERE is_test=0 ORDER BY id DESC LIMIT 100").fetchall()
    events = [{"event_id": e["id"], "time_bj": bj(e["ts"]), "strategy": labels.get(e["deployment_id"], f"#{e['deployment_id']}"),
               "level": e["level"], "message": e["message"]} for e in ev]

    pnl = sum(s["pnl_usd"] for s in strategies)
    acct = setting(c, "paper_account")
    now = datetime.now(timezone.utc)
    account = [{
        "snapshot_bj": (now + BJ).strftime("%Y-%m-%d %H:%M"),
        "heartbeat_bj": bj(setting(c, "paper_loop_heartbeat")),
        "equity_usd": round(float(acct["equity"]), 2) if acct else round(START_EQUITY + pnl, 2),
        "equity_basis": "Alpaca 账户回报" if acct else "初始 100,000 USD + 各策略成交推算",
        "pnl_usd": round(pnl, 2),
        "btc_price_usd": round(last_price, 2), "btc_price_bj": (last_ts + BJ).strftime("%Y-%m-%d %H:%M"),
        "btc_position": round(sum(s["position_btc"] for s in strategies), 6),
        "running": sum(1 for s in strategies if s["status"] == "运行中"),
        "strategies": len(strategies),
        "orders": len(orders_out), "filled": sum(s["filled"] for s in strategies),
        "budget_total_usd": sum(s["budget_usd"] or 0 for s in strategies),
    }]

    evolution = []
    has = c.execute("SELECT name FROM sqlite_master WHERE name='evolve_results'").fetchone()
    if has:
        run = c.execute("SELECT * FROM evolve_runs WHERE is_test=0 AND status='done' ORDER BY id DESC LIMIT 1").fetchone()
        if run:
            for x in c.execute("SELECT * FROM evolve_results WHERE run_id=? ORDER BY id", (run["id"],)).fetchall():
                cur = json.loads(x["current_json"]) if x["current_json"] else {}
                best = json.loads(x["best_json"]) if x["best_json"] else {}
                nv = c.execute("SELECT name, version_no FROM strategy_versions WHERE id=?",
                               (x["new_version_id"],)).fetchone() if x["new_version_id"] else None
                evolution.append({
                    "result_id": x["id"], "run_id": run["id"], "run_bj": bj(run["started_at"]),
                    "strategy": x["label"], "decision": x["decision"], "candidates": x["candidates"],
                    "current_return_pct": (cur or {}).get("return_pct"), "current_score": (cur or {}).get("score"),
                    "best_return_pct": (best or {}).get("return_pct"), "best_score": (best or {}).get("score"),
                    "change": (best or {}).get("change") or "—",
                    "new_version": f"{nv['name']} v{nv['version_no']}" if nv else "—", "reason": x["reason"]})

    for name, rows in [("account", account), ("strategies", strategies), ("events", events),
                       ("orders", orders_out), ("price", price), ("evolution", evolution)]:
        (out / f"{name}.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(account[0], ensure_ascii=False))


if __name__ == "__main__":
    main()
