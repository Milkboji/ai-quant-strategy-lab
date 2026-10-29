"""模拟回放：现有 BTC 策略 + 美股 ETF 择时 + 实验室美股版本，在一个共用的模拟 Paper 账户里连续运行若干个交易日（含周末 / 节假日）。

- 全部走真实代码路径：console.deploy_version（实验室美股版本显式部署）、us_exec.step / after_close（通用美股日线执行器）、
  PaperRunner.step（BTC）、allocation 两层分配、cash_guard 现金检查与杠杆告警、对账。
- 券商换成本地假 Alpaca（拦截唯一出口 http.request 底下的 requests.request，不联网）：
  美股市价单按下单时刻的价格成交（开盘后 = 当天开盘价）；BTC 按最近 5 分钟收盘价成交，手续费 0.25% 从币里扣。
- 行情：本地真实日线（data/etf_daily）和 BTC 5 分钟库（data/market_cache，没有时用 data/history）。
- 时钟：每 5 分钟一步，按纽交所日历判断开盘 / 收盘（含半日市）。

用法：python scripts/replay_us_week.py <数据目录> <BTC 版本 JSON> <开始日> <结束日> <输出目录> [源数据库]
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

data_dir, ver_path, d_start, d_end, out_dir = sys.argv[1:6]
src_db = sys.argv[6] if len(sys.argv) > 6 else None
root = tempfile.mkdtemp(prefix="aiquant_replay_")
os.environ.update(AIQUANT_ROOT=root, AIQUANT_DISABLE_PAPER_LOOP="1", AIQUANT_DISABLE_LIVE_FEED="1",
                  ALPACA_PAPER_KEY_ID="PKREPLAY", ALPACA_PAPER_SECRET_KEY="x", ALPACA_API_KEY_ID="AKREPLAY",
                  ALPACA_API_SECRET_KEY="x")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
(Path(root) / "data").mkdir(parents=True, exist_ok=True)
shutil.copytree(Path(data_dir) / "etf_daily", Path(root) / "data" / "etf_daily")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402

from aiquant import config  # noqa: E402

config.PAPER_ORDER_SUBMISSION_UNLOCKED = True        # 只在这个临时目录里的回放进程中：假券商，不联网
config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
(config.CONFIG_DIR / "paper_authorization.json").write_text(json.dumps({"replay": True}))

from aiquant import actions, allocation, console, db, jobs, research, rhythm  # noqa: E402
from aiquant import market as mk  # noqa: E402
from aiquant.data import http, us_daily  # noqa: E402
from aiquant.paper import cash_guard, etf_trend, us_exec, us_ledger  # noqa: E402
from aiquant.paper import runner as pr  # noqa: E402
from aiquant.strategy import parser, registry  # noqa: E402

UTC = timezone.utc
NOW = {"t": None}
DAILY = {}
for f in (Path(root) / "data" / "etf_daily").glob("*.csv"):
    d = pd.read_csv(f)
    d["d"] = us_daily.et_date(d["date"])
    DAILY[f.stem] = d
_cache = Path(data_dir) / "market_cache" / "alpaca_us_BTCUSD_5m.csv.gz"     # 服务实时维护的 BTC 5 分钟库（最新）
BTC = pd.read_csv(_cache if _cache.exists() else Path(data_dir) / "history" / "BTCUSD_5m.csv.gz")
BTC["ts"] = pd.to_datetime(BTC["ts"], utc=True)
BTC = BTC[["ts", "open", "high", "low", "close", "volume"]].sort_values("ts").reset_index(drop=True)


def stock_px(sym: str, t: datetime) -> float:
    d = DAILY[sym]
    day = t.astimezone(mk.ET).date()
    row = d[d["d"] == day]
    if len(row) and mk.is_trading_day(day) and t >= mk.session_open(day):
        return float(row["close"].iloc[0] if t >= mk.session_close(day) else row["open"].iloc[0])
    prev = d[d["d"] < day]
    return float(prev["close"].iloc[-1])


def btc_px(t: datetime) -> float:
    x = BTC[BTC["ts"] + pd.Timedelta(minutes=5) <= pd.Timestamp(t)]
    return float(x["close"].iloc[-1])


class Broker:
    """一个账户：现金 + BTC + 美股。所有订单记下来供报告使用。"""

    def __init__(self, cash: float):
        self.cash = cash
        self.btc = 0.0
        self.stocks: dict[str, float] = {}
        self.orders: dict[str, dict] = {}
        self.log: list[dict] = []
        self.min_cash = cash
        self.max_ratio = 0.0

    def equity(self, t) -> float:
        return self.cash + self.btc * btc_px(t) + sum(q * stock_px(s, t) for s, q in self.stocks.items())

    def long_value(self, t) -> float:
        return self.btc * btc_px(t) + sum(q * stock_px(s, t) for s, q in self.stocks.items())

    def __call__(self, method, url, headers=None, params=None, json=None, timeout=None):
        t = NOW["t"]
        r = requests.models.Response()
        r.status_code, body = 200, None
        path = url.split(".markets", 1)[-1]
        if path.startswith("/v2/stocks/bars"):
            syms = (params or {}).get("symbols", "").split(",")
            today = t.astimezone(mk.ET).date()
            done = t >= mk.session_close(today) if mk.is_trading_day(today) else True
            bars = {}
            for s in syms:
                if s not in DAILY:
                    continue
                d = DAILY[s]
                d = d[(d["d"] < today) | ((d["d"] == today) & done)]
                bars[s] = [{"t": x["date"], "o": x["open"], "h": x["high"], "l": x["low"], "c": x["close"],
                            "v": x["volume"]} for _, x in d.tail(450).iterrows()]
            body = {"bars": bars, "next_page_token": None}
        elif path == "/v2/account":
            eq = self.equity(t)
            body = {"equity": eq, "cash": self.cash, "buying_power": self.cash * 2, "long_market_value": self.long_value(t),
                    "short_market_value": 0, "status": "ACTIVE", "trading_blocked": False, "crypto_status": "ACTIVE"}
        elif path == "/v2/clock":
            day = t.astimezone(mk.ET).date()
            is_open = mk.is_trading_day(day) and mk.session_open(day) <= t < mk.session_close(day)
            nd = day if (mk.is_trading_day(day) and t < mk.session_open(day)) else mk.trading_days_after(day, 1)[0]
            body = {"is_open": is_open, "next_open": mk.session_open(nd).isoformat(), "timestamp": t.isoformat()}
        elif path == "/v2/positions" and method == "GET":
            body = ([{"symbol": "BTCUSD", "qty": str(self.btc), "market_value": str(self.btc * btc_px(t)),
                      "avg_entry_price": "0", "unrealized_pl": "0", "asset_class": "crypto"}] if self.btc > 0 else [])
            body += [{"symbol": s, "qty": str(q), "market_value": str(q * stock_px(s, t)), "asset_class": "us_equity",
                      "avg_entry_price": "0", "unrealized_pl": "0"} for s, q in self.stocks.items() if q > 0]
        elif path.startswith("/v2/positions/") and method == "GET":
            s = path.rsplit("/", 1)[-1]
            q = self.stocks.get(s, 0.0)
            if q > 0:
                body = {"symbol": s, "qty": str(q), "market_value": str(q * stock_px(s, t)), "avg_entry_price": "0",
                        "unrealized_pl": "0"}
            else:
                r.status_code, body = 404, {"message": "position does not exist"}
        elif path == "/v2/orders" and method == "POST":
            body = self._order(json, t)
            if body.get("_reject"):
                r.status_code = 403
        elif path.startswith("/v2/orders:by_client_order_id"):
            o = self.orders.get((params or {}).get("client_order_id"))
            if o is None:
                r.status_code, body = 404, {"message": "not found"}
            else:
                body = o
        else:
            r.status_code, body = 404, {"message": f"replay: {method} {path} not handled"}
        r._content = __import__("json").dumps(body, default=str).encode()
        return r

    def _order(self, o, t):
        coid = o["client_order_id"]
        if coid in self.orders:
            return {"_reject": True, "message": "client_order_id must be unique"}
        sym, side = o["symbol"], o["side"]
        if sym in ("BTC/USD", "BTCUSD"):
            px = btc_px(t)
            q = float(o["qty"])
            if side == "buy":
                assert q * px <= self.cash + 1e-6, "BTC 买入超过现金（不得使用保证金）"
                self.cash -= q * px
                self.btc += q * (1 - 0.0025)
            else:
                assert q <= self.btc + 1e-9
                self.btc -= q
                self.cash += q * px * (1 - 0.0025)
        else:
            day = t.astimezone(mk.ET).date()
            assert mk.is_trading_day(day) and mk.session_open(day) <= t < mk.session_close(day), "美股休市时下单"
            px = stock_px(sym, t)
            q = float(o["notional"]) / px if o.get("notional") else float(o["qty"])
            if side == "buy":
                assert q * px <= self.cash + 1e-6, f"美股买入 {q * px:.2f} 超过现金 {self.cash:.2f}（不得使用保证金）"
                self.cash -= q * px
                self.stocks[sym] = self.stocks.get(sym, 0.0) + q
            else:
                assert q <= self.stocks.get(sym, 0.0) + 1e-9, "卖出超过持仓（不得做空）"
                self.stocks[sym] = self.stocks.get(sym, 0.0) - q
                self.cash += q * px
        self.min_cash = min(self.min_cash, self.cash)
        info = {"client_order_id": coid, "symbol": sym, "side": side, "status": "filled", "filled_qty": str(q),
                "filled_avg_price": str(px), "qty": str(q)}
        self.orders[coid] = info
        self.log.append({"t": t.isoformat(), "symbol": sym, "side": side, "qty": q, "price": px, "coid": coid,
                         "notional": o.get("notional")})
        return info


# ---------------------------------------------------------------------------
# 准备：数据库、版本、部署
# ---------------------------------------------------------------------------
broker = None
http.requests.request = lambda *a, **k: broker(*a, **k)
db.init_db()
start_t = datetime.combine(pd.Timestamp(d_start).date(), datetime.min.time(), tzinfo=mk.ET).replace(hour=8).astimezone(UTC)
end_t = datetime.combine(pd.Timestamp(d_end).date(), datetime.min.time(), tzinfo=mk.ET).replace(hour=17, minute=30).astimezone(UTC)
NOW["t"] = start_t
qld_qty, qld_cost = 330.938127274, 90.658881               # 电脑上 ETF 择时 10-21 开盘买入的 QLD（真实成交）
broker = Broker(cash=100_000.0 - qld_qty * qld_cost)
broker.stocks["QLD"] = qld_qty

# 美股 ETF 择时：电脑上的状态（10-21 已买入），迁移为通用执行器上的部署
db.set_setting("etf_trend_config", {"enabled": True, "signal_symbol": "QQQ", "trade_symbol": "QLD", "sma": 150,
                                    "band": 0.02, "budget_pct": 30.0, "lend_pct": 50.0})
db.set_setting("etf_trend_state", {"last_action_date": "2026-10-21", "signal": {"date": "2026-10-20", "regime": 1},
                                   "action": "买入", "entry": {"signal_date": "2026-10-20", "target": 30002.49},
                                   "position": {"qty": qld_qty, "market_value": qld_qty * qld_cost,
                                                "avg_entry_price": qld_cost, "unrealized_pl": 0}})
rhythm.set_config(us_mode="shadow", us_mode_at=db.now_iso(), us_mode_by="回放")
actions.init()                                              # 启动时的一次性迁移（与电脑上安全重启后相同）
mig = db.get_setting("us_exec_migration")
if os.environ.get("REPLAY_CRYPTO_MAX"):          # 腾出现金层给试用部署（新部署不摊薄现有策略）
    db.set_setting("capital_layers", {"crypto": {"max": float(os.environ["REPLAY_CRYPTO_MAX"])}})
rhythm.etf_long_metrics(compute=True)                       # 美股内部按实力分配时 ETF 用长周期结果

# 现有 BTC 策略（电脑上运行中的 6 个部署：版本与配置原样）
vers = {v["id"]: v for v in json.load(open(ver_path, encoding="utf-8"))}
btc_specs = []
if src_db:
    import sqlite3
    c = sqlite3.connect(src_db)
    c.row_factory = sqlite3.Row
    for r in c.execute("SELECT id, version_id, config_json FROM paper_deployments WHERE broker='alpaca_paper' AND is_test=0 "
                       "AND status='running' ORDER BY id"):
        btc_specs.append((r["id"], r["version_id"], json.loads(r["config_json"])))
btc_deps = []
for pc_id, vid, cfg in btc_specs:
    v = registry.freeze(None, vers[vid]["schema"], source="test", nl_text=f"电脑部署 #{pc_id}")
    cfg = {k: cfg[k] for k in ("label", "signal_tf", "aux_tf", "alloc_pct", "portfolio", "stop_mode", "budget_usd",
                               "strategy_key") if k in cfg}
    cfg["replay_of"] = pc_id
    did = db.ex("INSERT INTO paper_deployments(version_id,config_json,broker,status,created_at,updated_at,is_test) "
                "VALUES(?,?,?,?,?,?,0)", (v["id"], json.dumps(cfg, ensure_ascii=False), "alpaca_paper", "running",
                                          db.now_iso(), db.now_iso()))
    btc_deps.append(did)

# 实验室美股版本：解析中文规则 → 冻结 → 训练期回测（部署门禁）→ 策略总台显式部署
LABS = [(1, "QLD", "当QQQ收盘价高于5日均线时买入，QQQ收盘价低于5日均线时卖出", "纳指5日均线"),
        (2, "QLD", "当QQQ收盘价高于10日均线时买入，QQQ收盘价低于10日均线时卖出", "纳指10日均线"),
        (3, "SSO", "当SPY收盘价高于5日均线时买入，SPY收盘价低于5日均线时卖出", "标普5日均线")]
lab_deps = {}
r0 = research.get_research()
for slot, sym, text, name in LABS:
    research.set_research({"symbol": sym, "source": "alpaca", "window_preset": "1095天", "window_anchor": "train_end"})
    p = parser.parse(text)
    assert p.status == "ok", text
    v = registry.freeze(slot, dict(p.schema, name=name), nl_text=text)
    jid, msg = actions.submit_backtest(slot, version_id=v["id"])
    assert jid, msg
    jobs.wait_all(300)
    assert jobs.get(jid)["status"] == "done", jobs.get(jid).get("error")
    res = console.deploy_version(f"slot:{slot}", v["id"], actor="回放", role="admin")
    lab_deps[res["deployment_id"]] = {"name": name, "symbol": sym, "rule": text, "deploy": res["message"]}
research.set_research({k: r0[k] for k in ("symbol", "source", "window_preset", "window_anchor")})
etf_dep = us_exec.etf_deployment()

# ---------------------------------------------------------------------------
# 回放循环（与服务循环相同的顺序：BTC 部署 → 美股执行器 → 收盘后检查 → 杠杆检查）
# ---------------------------------------------------------------------------
runners = {did: pr.PaperRunner(did) for did in btc_deps}
BTC_STEPS: dict = {}
days: dict[str, dict] = {}
evt0 = db.q1("SELECT COALESCE(MAX(id),0) AS m FROM paper_events")["m"]
t = start_t
step = timedelta(minutes=5)
while t <= end_t:
    NOW["t"] = t
    df5 = BTC[(BTC["ts"] >= pd.Timestamp(t - timedelta(days=120))) & (BTC["ts"] + pd.Timedelta(minutes=5) <= pd.Timestamp(t))]
    df5 = df5.reset_index(drop=True)
    db.set_setting("btc_last_price", {"price": float(df5["close"].iloc[-1]), "at": t.isoformat()})
    for did, run in runners.items():
        try:
            rr = run.step(df5, t)
            k = f"{rr.get('action')}：{(rr.get('reason') or '')[:40]}"
            BTC_STEPS.setdefault(did, {}).setdefault(k, 0)
            BTC_STEPS[did][k] += 1
        except AssertionError:
            raise
        except Exception as e:  # noqa: BLE001
            pr.event(did, "ERROR", f"回放：{type(e).__name__}: {e}")
    etf_trend.step(t)
    etf_trend.after_close(t)
    acct = broker(None, "https://paper-api.alpaca.markets/v2/account").json()
    x = cash_guard.exposure(acct)
    broker.max_ratio = max(broker.max_ratio, x["ratio"])
    day = t.astimezone(mk.ET).date()
    if mk.is_trading_day(day) and mk.session_close(day) <= t < mk.session_close(day) + step:   # 每个交易日收盘时记一次
        bpos = {s: q for s, q in broker.stocks.items()}
        rec = us_ledger.reconcile(bpos)
        l1 = allocation.layer1(broker.equity(t))
        per = {}
        for d in [etf_dep["id"], *lab_deps]:
            dep = pr.get_deployment(d)
            sym = us_exec.effective(dep)["trade_symbol"]
            p = us_ledger.with_marks(us_ledger.position(d, sym), stock_px(sym, t))
            w = us_exec.get_why(dep) or {}
            per[d] = {"symbol": sym, "qty": round(p["qty"], 6), "avg_cost": round(p["avg_cost"], 4),
                      "market_value": round(p.get("market_value", 0), 2), "unrealized": round(p.get("unrealized", 0), 2),
                      "realized": round(p["realized"], 2), "why": w.get("text"), "action": us_exec.dep_state(dep).get("action")}
        btc_led = sum(r.ledger_qty() for r in runners.values())
        days[str(day)] = {"equity": round(broker.equity(t), 2), "cash": round(broker.cash, 2), "btc_qty": round(broker.btc, 8),
                          "btc_ledger": round(btc_led, 8), "stocks": {k: round(v, 6) for k, v in broker.stocks.items()},
                          "reconcile": {k: {"broker": round(v["broker"], 6), "ledger": round(v["ledger"], 6), "ok": v["ok"],
                                            "by_strategy": {str(a): round(b, 6) for a, b in v["by_strategy"].items()}}
                                        for k, v in rec.items()},
                          "layer1": {k: l1[k] for k in ("us_target_pct", "us_deduct_pct", "crypto_pct", "cash_pct", "lend_state")},
                          "us": per, "exposure": x["ratio"]}
    elif not mk.is_trading_day(day) and t.astimezone(mk.ET).hour == 12 and t.astimezone(mk.ET).minute == 0:
        days[str(day)] = {"closed": True, "why": {d: (us_exec.get_why(pr.get_deployment(d)) or {}).get("text")
                                                  for d in [etf_dep["id"], *lab_deps]},
                          "equity": round(broker.equity(t), 2), "btc_qty": round(broker.btc, 8)}
    t += step

events = db.q("SELECT ts, deployment_id, level, message FROM paper_events WHERE id>? ORDER BY id", (evt0,))
allocs = db.q("SELECT a.deployment_id, a.symbol, a.side, a.req_notional, a.req_qty, a.purpose, a.filled_qty, a.filled_price, "
              "a.status, a.trade_date, o.client_order_id, o.side AS order_side, o.notional AS order_notional, o.qty AS order_qty "
              "FROM us_order_allocs a JOIN us_orders o ON o.id=a.order_id ORDER BY a.id")
out = {"window": [d_start, d_end], "migration_checks": mig["checks"], "etf_deployment": etf_dep["id"], "labs": lab_deps,
       "btc_deployments": {d: pr.get_deployment(d)["config"].get("label") for d in btc_deps},
       "broker_orders": broker.log, "us_allocs": allocs, "days": days, "min_cash": broker.min_cash,
       "max_exposure": broker.max_ratio, "final_equity": broker.equity(end_t), "btc_steps": BTC_STEPS,
       "btc_budgets": {d: pr.get_deployment(d)["config"].get("budget_usd") for d in btc_deps},
       "alloc_explain": allocation.explain(broker.equity(end_t)),
       "trial": {d: allocation.trial_status(pr.get_deployment(d))["text"] for d in [etf_dep["id"], *lab_deps, *btc_deps]},
       "events": [e for e in events if any(k in e["message"] for k in ("[美股]", "[ETF趋势]", "买入", "卖出", "成交", "额度",
                                                                      "现金", "杠杆", "节奏", "对账", "借"))]}
Path(out_dir).mkdir(parents=True, exist_ok=True)
Path(out_dir, "replay.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
print(json.dumps({"orders": len(broker.log), "us_orders": [x for x in broker.log if x["symbol"] != "BTC/USD"],
                  "min_cash": broker.min_cash, "max_exposure": broker.max_ratio,
                  "recon_ok": all(v["ok"] for d in days.values() for v in d.get("reconcile", {}).values())},
                 ensure_ascii=False, indent=1, default=str))
