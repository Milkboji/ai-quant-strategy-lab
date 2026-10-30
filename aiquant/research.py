"""研究服务层：全局研究设置、数据分区（训练/验证/独立测试）、回测执行与结果持久化。"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from . import config, db
from .data import market_data as md
from .engine import backtest as bt
from .engine.execution import CostModel
from .strategy import registry
from .strategy.schema import StrategySchema, validate

VALIDATION_RUN_LIMIT = 3      # 每条策略谱系允许触及验证集的次数
FINAL_TEST_MIN_DAYS = 90


def clean(o):
    """把 numpy / inf / nan 转为可 JSON 序列化的 Python 值。"""
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (np.floating,)):
        o = float(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, float):
        if math.isinf(o):
            return "inf"
        if math.isnan(o):
            return None
    return o


# ---------------------------------------------------------------------------
# 全局研究设置
# ---------------------------------------------------------------------------

def _utc_today() -> datetime:
    n = datetime.now(timezone.utc)
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


def _is_us(market) -> bool:
    return market is not None and getattr(market, "key", "crypto") != "crypto"


# 美股唯一的一套数据分区（实验室、ETF 进化、ETF 挑战者、快速检验共用）。边界按日期冻结、带版本号，不随时间滚动；
# 需要调整时新增一个版本并切换 settings.us_split_version，旧结果按记录里的分区版本解读。日期均为美东交易日，结束日不含。
US_SPLITS = {
    1: {"version": 1, "train_start": "2017-01-01", "val_start": "2025-07-31", "test_start": "2026-06-23",
        "test_end": "2026-10-29", "frozen_at": "2026-10-29",
        "note": "训练期 2017-01-01~2025-07-30；验证期 2025-07-31~2026-06-22（224 个交易日）；"
                "测试期 2026-06-23~2026-10-28（最近 90 个交易日）"},
}


def us_split(version: int | None = None) -> dict:
    """美股分区（固定日期，带版本号）。返回与加密货币分区相同的键（val_start / test_start / test_end，UTC ISO）。"""
    v = int(version or db.get_setting("us_split_version") or max(US_SPLITS))
    d = US_SPLITS[v]
    iso = lambda x: f"{x}T00:00:00+00:00"   # noqa: E731
    return {"version": v, "market": "us_equity", "train_start": iso(d["train_start"]), "val_start": iso(d["val_start"]),
            "test_start": iso(d["test_start"]), "test_end": iso(d["test_end"]), "frozen_at": d["frozen_at"],
            "note": d["note"]}


def get_split(market=None) -> dict:
    """数据分区。加密货币：首次启动时冻结，测试集 = 最近 90 天，验证集 = 其前 60 天。
    美股：固定日期、带版本号的唯一分区（us_split），按交易日历。"""
    if _is_us(market):
        return us_split()
    sp = db.get_setting("data_split")
    if sp is None:
        test_end = _utc_today()
        test_start = test_end - timedelta(days=90)
        val_start = test_start - timedelta(days=60)
        sp = {"val_start": val_start.isoformat(), "test_start": test_start.isoformat(),
              "test_end": test_end.isoformat(), "created_at": db.now_iso(),
              "note": "首次启动时冻结：测试集=最近90天，验证集=其前60天，更早为训练集"}
        db.set_setting("data_split", sp)
    return sp


def split_label(start: datetime, end: datetime, market=None) -> str:
    sp = get_split(market)
    vs, ts_, te = (md.to_utc(sp[k]) for k in ("val_start", "test_start", "test_end"))
    if end <= vs:
        return "训练集"
    if start >= vs and end <= ts_:
        return "验证集"
    if start >= ts_ and end <= te:
        return "测试集"
    if end <= ts_:
        return "训练+验证"
    if start >= te:
        return "前向新数据"   # 数据分区冻结之后才产生的新行情，从未参与任何研究
    return "跨越测试集"


def _touch_name(base: str, market=None) -> str:
    """留出集触及记录按市场分开（测试集 / 最终测试各市场独立；验证集按谱系计数，谱系本身只属于一个市场）。"""
    return f"{base}_us" if _is_us(market) and base in ("test_contaminated", "final_test") else base


def test_set_status(market=None) -> dict:
    sp = get_split(market)
    touched = db.q("SELECT * FROM holdout_touches WHERE split=? ORDER BY id", (_touch_name("test_contaminated", market),))
    finals = db.q("SELECT * FROM holdout_touches WHERE split=? ORDER BY id", (_touch_name("final_test", market),))
    days = (md.to_utc(sp["test_end"]) - md.to_utc(sp["test_start"])).days
    if touched:
        return {"ok": False, "label": "测试集已被研究回测触及，失去独立资格", "touched": len(touched),
                "finals": len(finals), "days": days}
    if days < FINAL_TEST_MIN_DAYS:
        return {"ok": False, "label": "独立测试数据不足", "touched": 0, "finals": len(finals), "days": days}
    return {"ok": True, "label": f"独立测试集未触及（{days}天）", "touched": 0, "finals": len(finals), "days": days}


def version_market(v: dict):
    """策略版本属于哪个市场：用另一只 ETF 出信号，或最近一次回测用的是美股日线快照 → 美股；否则加密货币。"""
    from . import market as mk
    if (v.get("schema") or {}).get("signal_symbol"):
        return mk.US_EQUITY
    b = db.q1("SELECT d.venue FROM backtests b JOIN datasets d ON d.id=b.dataset_id WHERE b.version_id=? "
              "ORDER BY b.id DESC LIMIT 1", (v["id"],))
    if b and b["venue"] == "alpaca_stocks":
        return mk.US_EQUITY
    return mk.CRYPTO


def market_of(r: dict | None = None):
    from . import market as mk
    return mk.of_symbol((r or get_research()).get("symbol"))


DEFAULT_RESEARCH = {
    "symbol": "BTC/USD", "source": "alpaca", "timeframe_min": 15,
    "window_preset": "90天", "window_anchor": "train_end",
    "custom_start": None, "custom_end": None,
    "initial_cash": 10000.0, "max_alloc_pct": 100.0, "fee_bps": 25.0, "slippage_bps": 2.0,
    "spread_bps": 1.0,
}


def get_research() -> dict:
    r = dict(DEFAULT_RESEARCH)
    r.update(db.get_setting("research", {}) or {})
    return r


def set_research(r: dict) -> None:
    cur = get_research()
    cur.update(r)
    db.set_setting("research", cur)


def resolve_window(r: dict) -> tuple[datetime, datetime]:
    if r["window_preset"] == "自定义" and r.get("custom_start") and r.get("custom_end"):
        s, e = md.to_utc(r["custom_start"]), md.to_utc(r["custom_end"])
        return s, e
    days = int(str(r["window_preset"]).replace("天", ""))
    if r.get("window_anchor") == "latest":
        end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    else:
        end = md.to_utc(get_split(market_of(r))["val_start"])
    return end - timedelta(days=days), end


def base_cost_model(r: dict, market=None) -> CostModel:
    """固定代理值（旧口径）：研究设置里的成本（加密货币）；美股使用市场配置里的成本（佣金 0、滑点、点差、卖出监管费）。"""
    from . import market as mk
    m = market or mk.of_symbol(r.get("symbol"))
    if m.key != "crypto":
        return m.cost()
    return CostModel(fee_bps=float(r["fee_bps"]), slippage_bps=float(r["slippage_bps"]),
                     spread_bps=float(r["spread_bps"]), spread_source="冻结代理模型（未使用历史逐笔报价）")


def cost_model(r: dict, market=None) -> CostModel:
    """当前口径的成本模型：管理员确认切换到实测点差后，点差按交易品种的分时段估计（spread_model）；否则为固定代理值。"""
    from . import market as mk
    from . import spread_model
    m = market or mk.of_symbol(r.get("symbol"))
    sym = r.get("symbol") if m.key != "crypto" else "BTC/USD"
    return spread_model.apply(base_cost_model(r, m), sym)


def bt_config(start: datetime, end: datetime, r: dict, market=None, account_share_pct: float | None = None) -> bt.BTConfig:
    """唯一的回测配置入口：策略实验室、自动进化、挑战者检验、长周期体检都从这里构造，
    成本按市场、交易节奏按当前规则（与模拟盘一致）。
    单个策略持仓上限按账户口径换算：上限% ÷ 该策略额度占账户的比例（account_share_pct；为空按新部署的试用额度）。"""
    from . import market as mk
    from . import rhythm
    m = market or mk.of_symbol(r.get("symbol"))
    from . import allocation
    return bt.BTConfig(start=start, end=end, initial_cash=float(r["initial_cash"]),
                       max_alloc_pct=float(r["max_alloc_pct"]), cost=cost_model(r, m),
                       rhythm=rhythm.current(m), market=m,
                       position_cap_frac=allocation.backtest_cap_frac(account_share_pct))


def effective_tf(slot: dict, schema: dict, r: dict) -> tuple[int, int | None]:
    if _is_us(market_of(r)):            # 美股：只做日线（按美东交易时段），不支持辅助周期
        if schema.get("aux_tf") or slot.get("aux_timeframe_min"):
            raise ValueError("美股日线策略暂不支持辅助周期，请去掉辅助周期条件")
        return 1440, None
    mode = slot.get("timeframe_mode") or "follow"
    if mode != "follow":
        tf = int(mode)
    elif schema.get("signal_tf"):
        tf = int(schema["signal_tf"])
    else:
        tf = int(r["timeframe_min"])
    aux = schema.get("aux_tf")
    if slot.get("aux_timeframe_min"):
        aux = int(slot["aux_timeframe_min"])
    if aux is not None and aux <= tf:
        raise ValueError(f"辅助周期({aux}分钟)必须大于主周期({tf}分钟)")
    return tf, aux


def params_key(version_id: int, tf: int, aux: int | None, start: datetime, end: datetime, r: dict,
               source: str, kind: str = "backtest") -> str:
    d = {"v": version_id, "tf": tf, "aux": aux, "s": start.isoformat(), "e": end.isoformat(),
         "cash": float(r["initial_cash"]), "alloc": float(r["max_alloc_pct"]), "fee": float(r["fee_bps"]),
         "slip": float(r["slippage_bps"]), "spr": float(r["spread_bps"]), "src": source, "k": kind}
    from . import market as mk
    from . import rhythm
    m = mk.of_symbol(r.get("symbol"))
    if rhythm.current(m) is not None:   # 交易节奏规则变化 → 之前的回测结果不再复用
        d["rh"] = rhythm.rules_hash(m)
    from . import spread_model
    if spread_model.current_name() != spread_model.FIXED:   # 成本口径变化 → 旧口径的结果不复用（保留，不覆盖）
        d["basis"] = spread_model.current_name()
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# 回测执行（由任务队列调用）
# ---------------------------------------------------------------------------

def execute_backtest(job: dict, stage, should_stop) -> int:
    p = json.loads(job["params_json"])
    v = registry.get_version(job["version_id"])
    if v is None:
        raise ValueError("策略版本不存在")
    stage("校验策略")
    obj, errs = validate(v["schema"])
    if errs:
        raise ValueError("策略校验失败：" + "；".join(errs))
    start, end = md.to_utc(p["start"]), md.to_utc(p["end"])
    kind = job["kind"]
    mkt = market_of(p["research"])
    split = split_label(start, end, mkt) if kind == "backtest" else "独立测试"
    if kind == "backtest" and split in ("测试集", "跨越测试集") and not p.get("allow_test_touch"):
        raise ValueError("该回测窗口覆盖独立测试集。研究回测默认禁止触及测试集；"
                         "如确需运行，请在设置中勾选『允许触及测试集（将使其失去独立资格）』。")
    if kind == "backtest" and split in ("验证集", "训练+验证"):
        from . import holdout
        n = holdout.used(v["lineage_id"])
        if n >= VALIDATION_RUN_LIMIT:
            raise ValueError(f"该策略谱系已使用验证集 {n} 次，达到上限 {VALIDATION_RUN_LIMIT}，防止过拟合。")
    stage("校验数据")
    r = p["research"]
    df_sig = None
    if _is_us(mkt):
        from .data import us_daily
        sym = r["symbol"]
        ds = us_daily.get_dataset(sym, start, end)
        df5 = md.load_dataset(ds)
        if obj.signal_symbol and obj.signal_symbol != sym:
            df_sig = md.load_dataset(us_daily.get_dataset(obj.signal_symbol, start, end))
    else:
        if obj.signal_symbol:
            raise ValueError("「用另一只 ETF 出信号」只适用于美股，请把交易品种切换为美股 ETF")
        ds = md.get_dataset(start, end, source=p["source"], progress=lambda m: stage(f"校验数据：{m}"))
        df5 = md.load_dataset(ds)
    stage("运行策略引擎 / 风控 / 模拟撮合")
    cfg = bt_config(start, end, r)
    res = bt.run(df5, obj, p["tf"], p["aux"], cfg, should_stop=should_stop, df_signal=df_sig)
    stage("保存成交记录与权益曲线")
    bid = store_backtest(job_id=job["id"], version=v, slot_no=job["slot_no"], ds=ds, start=start, end=end,
                         tf=p["tf"], aux=p["aux"], split=split, cfg=cfg, res=res, kind=kind,
                         is_test=int(job["is_test"] or ds["is_test"]), market=mkt)
    ensure_benchmark(ds, df5, start, end, r)
    return bid


def store_backtest(*, job_id, version: dict, slot_no, ds: dict, start: datetime, end: datetime, tf: int,
                   aux: int | None, split: str, cfg, res: dict, kind: str = "backtest", is_test: int = 0,
                   market=None) -> int:
    """持久化一次回测（成交、权益曲线、留出集触及记录）。供任务队列与自动进化共用。"""
    v = version
    assumptions = {"cost_model": cfg.cost.as_dict(), "cost_basis": cfg.cost.basis, "rules": res["assumptions"],
                   "initial_cash": cfg.initial_cash, "max_alloc_pct": cfg.max_alloc_pct,
                   "warmup_start": json.loads(ds["quality_json"]).get("warmup_start"),
                   "data_source": ds["source"], "venue": ds["venue"]}
    if _is_us(market):
        assumptions["split_version"] = get_split(market)["version"]
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO backtests(job_id,version_id,slot_no,dataset_id,dataset_hash,window_start,window_end,"
            "signal_tf,aux_tf,split_label,assumptions_json,metrics_json,equity_json,created_at,is_test,cost_basis) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, v["id"], slot_no, ds["id"], ds["sha256"], start.isoformat(), end.isoformat(),
             tf, aux, split, json.dumps(clean(assumptions), ensure_ascii=False),
             json.dumps(clean(res["metrics"])), json.dumps(res["equity"]), db.now_iso(), is_test, cfg.cost.basis))
        bid = cur.lastrowid
        for t in res["trades"]:
            t = clean(t)
            c.execute(
                "INSERT INTO trades(backtest_id,entry_signal_ts,entry_fill_ts,entry_price,exit_signal_ts,exit_fill_ts,"
                "exit_price,qty,fee,spread_cost,slippage_cost,exit_reason,pnl,equity_after) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (bid, t["entry_signal_ts"], t["entry_fill_ts"], t["entry_price"], t["exit_signal_ts"],
                 t["exit_fill_ts"], t["exit_price"], t["qty"], t["fee"], t["spread_cost"], t["slippage_cost"],
                 t["exit_reason"], t["pnl"], t["equity_after"]))
        if split in ("验证集", "训练+验证"):
            from . import holdout
            holdout.ensure()
            holdout.record(v["lineage_id"], v["id"], start.isoformat(), end.isoformat(), bid, conn=c)
        if split in ("测试集", "跨越测试集") and not is_test:
            c.execute("INSERT INTO holdout_touches(version_id,split,window_start,window_end,backtest_id,touched_at)"
                      " VALUES(?,?,?,?,?,?)", (v["id"], _touch_name("test_contaminated", market), start.isoformat(),
                                              end.isoformat(), bid, db.now_iso()))
        if kind == "final_test" and not is_test:
            c.execute("INSERT INTO holdout_touches(version_id,split,window_start,window_end,backtest_id,touched_at)"
                      " VALUES(?,?,?,?,?,?)", (v["id"], _touch_name("final_test", market), start.isoformat(),
                                              end.isoformat(), bid, db.now_iso()))
    return bid


def validation_runs_used(lineage_id: str) -> int:
    from . import holdout
    return holdout.used(lineage_id)


def ensure_benchmark(ds: dict, df5: pd.DataFrame, start: datetime, end: datetime, r: dict) -> dict:
    ck = json.dumps(cost_model(r).as_dict() | {"cash": float(r["initial_cash"])}, sort_keys=True)
    ex = db.q1("SELECT * FROM benchmarks WHERE dataset_hash=? AND window_start=? AND window_end=? AND cost_key=?",
               (ds["sha256"], start.isoformat(), end.isoformat(), ck))
    if ex:
        return ex
    cfg = bt.BTConfig(start=start, end=end, initial_cash=float(r["initial_cash"]), cost=cost_model(r))
    b = bt.buy_and_hold(df5, cfg)   # 加密货币：BTC 买入持有；美股：所选 ETF 买入持有
    db.ex("INSERT INTO benchmarks(dataset_id,dataset_hash,window_start,window_end,cost_key,metrics_json,equity_json,"
          "created_at,is_test) VALUES(?,?,?,?,?,?,?,?,?)",
          (ds["id"], ds["sha256"], start.isoformat(), end.isoformat(), ck, json.dumps(clean(b["metrics"])),
           json.dumps(b["equity"]), db.now_iso(), ds["is_test"]))
    return db.q1("SELECT * FROM benchmarks WHERE dataset_hash=? AND window_start=? AND window_end=? AND cost_key=?",
                  (ds["sha256"], start.isoformat(), end.isoformat(), ck))


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def get_backtest(bid: int | None) -> dict | None:
    if not bid:
        return None
    b = db.q1("SELECT * FROM backtests WHERE id=?", (bid,))
    if b:
        b["metrics"] = json.loads(b["metrics_json"])
        b["equity"] = json.loads(b["equity_json"])
        b["assumptions"] = json.loads(b["assumptions_json"])
    return b


def get_trades(bid: int) -> pd.DataFrame:
    return pd.DataFrame(db.q("SELECT * FROM trades WHERE backtest_id=? ORDER BY id", (bid,)))


def latest_backtest_for_version(vid: int, include_test: bool = False, kind_split: str | None = None) -> dict | None:
    sql = "SELECT id FROM backtests WHERE version_id=?"
    args: list = [vid]
    if not include_test:
        sql += " AND is_test=0"
    if kind_split:
        sql += " AND split_label=?"
        args.append(kind_split)
    r = db.q1(sql + " ORDER BY id DESC LIMIT 1", args)
    return get_backtest(r["id"]) if r else None


def benchmark_for(bt_row: dict) -> dict | None:
    b = db.q1("SELECT * FROM benchmarks WHERE dataset_hash=? AND window_start=? AND window_end=? ORDER BY id DESC "
              "LIMIT 1", (bt_row["dataset_hash"], bt_row["window_start"], bt_row["window_end"]))
    if b:
        b["metrics"] = json.loads(b["metrics_json"])
        b["equity"] = json.loads(b["equity_json"])
    return b


def chart_data(bt_row: dict, max_bars: int = 400) -> tuple[pd.DataFrame, pd.DataFrame]:
    """返回该回测所用数据版本的主周期K线（窗口内）与成交记录，用于画图（数据版本与回测一致）。"""
    ds = db.q1("SELECT * FROM datasets WHERE id=?", (bt_row["dataset_id"],))
    df5 = md.load_dataset(ds)
    end = md.to_utc(bt_row["window_end"])
    d = df5[df5["ts"] < pd.Timestamp(end)]
    if "close_time" in d.columns:        # 美股日线快照
        from .engine.signals import daily_bars
        k = daily_bars(d, end)
    else:
        k = md.resample(d, bt_row["signal_tf"], end)
    k = k[(k["open_time"] >= pd.Timestamp(bt_row["window_start"]))]
    return k, get_trades(bt_row["id"])
