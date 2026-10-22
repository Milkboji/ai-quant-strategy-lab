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


def get_split() -> dict:
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


def split_label(start: datetime, end: datetime) -> str:
    sp = get_split()
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


def test_set_status() -> dict:
    sp = get_split()
    touched = db.q("SELECT * FROM holdout_touches WHERE split='test_contaminated' ORDER BY id")
    finals = db.q("SELECT * FROM holdout_touches WHERE split='final_test' ORDER BY id")
    days = (md.to_utc(sp["test_end"]) - md.to_utc(sp["test_start"])).days
    if touched:
        return {"ok": False, "label": "测试集已被研究回测触及，失去独立资格", "touched": len(touched),
                "finals": len(finals), "days": days}
    if days < FINAL_TEST_MIN_DAYS:
        return {"ok": False, "label": "独立测试数据不足", "touched": 0, "finals": len(finals), "days": days}
    return {"ok": True, "label": f"独立测试集未触及（{days}天）", "touched": 0, "finals": len(finals), "days": days}


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
        end = md.to_utc(get_split()["val_start"])
    return end - timedelta(days=days), end


def cost_model(r: dict) -> CostModel:
    return CostModel(fee_bps=float(r["fee_bps"]), slippage_bps=float(r["slippage_bps"]),
                     spread_bps=float(r["spread_bps"]), spread_source="冻结代理模型（未使用历史逐笔报价）")


def effective_tf(slot: dict, schema: dict, r: dict) -> tuple[int, int | None]:
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
    split = split_label(start, end) if kind == "backtest" else "独立测试"
    if kind == "backtest" and split in ("测试集", "跨越测试集") and not p.get("allow_test_touch"):
        raise ValueError("该回测窗口覆盖独立测试集。研究回测默认禁止触及测试集；"
                         "如确需运行，请在设置中勾选『允许触及测试集（将使其失去独立资格）』。")
    if kind == "backtest" and split in ("验证集", "训练+验证"):
        n = db.q1("SELECT COUNT(*) AS n FROM holdout_touches h JOIN strategy_versions v ON v.id=h.version_id "
                  "WHERE h.split='validation' AND v.lineage_id=?", (v["lineage_id"],))["n"]
        if n >= VALIDATION_RUN_LIMIT:
            raise ValueError(f"该策略谱系已使用验证集 {n} 次，达到上限 {VALIDATION_RUN_LIMIT}，防止过拟合。")
    stage("校验数据")
    ds = md.get_dataset(start, end, source=p["source"], progress=lambda m: stage(f"校验数据：{m}"))
    df5 = md.load_dataset(ds)
    stage("运行策略引擎 / 风控 / 模拟撮合")
    r = p["research"]
    cfg = bt.BTConfig(start=start, end=end, initial_cash=float(r["initial_cash"]),
                      max_alloc_pct=float(r["max_alloc_pct"]), cost=cost_model(r))
    res = bt.run(df5, obj, p["tf"], p["aux"], cfg, should_stop=should_stop)
    stage("保存成交记录与权益曲线")
    bid = store_backtest(job_id=job["id"], version=v, slot_no=job["slot_no"], ds=ds, start=start, end=end,
                         tf=p["tf"], aux=p["aux"], split=split, cfg=cfg, res=res, kind=kind,
                         is_test=int(job["is_test"] or ds["is_test"]))
    ensure_benchmark(ds, df5, start, end, r)
    return bid


def store_backtest(*, job_id, version: dict, slot_no, ds: dict, start: datetime, end: datetime, tf: int,
                   aux: int | None, split: str, cfg, res: dict, kind: str = "backtest", is_test: int = 0) -> int:
    """持久化一次回测（成交、权益曲线、留出集触及记录）。供任务队列与自动进化共用。"""
    v = version
    assumptions = {"cost_model": cfg.cost.as_dict(), "rules": res["assumptions"],
                   "initial_cash": cfg.initial_cash, "max_alloc_pct": cfg.max_alloc_pct,
                   "warmup_start": json.loads(ds["quality_json"]).get("warmup_start"),
                   "data_source": ds["source"], "venue": ds["venue"]}
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO backtests(job_id,version_id,slot_no,dataset_id,dataset_hash,window_start,window_end,"
            "signal_tf,aux_tf,split_label,assumptions_json,metrics_json,equity_json,created_at,is_test) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, v["id"], slot_no, ds["id"], ds["sha256"], start.isoformat(), end.isoformat(),
             tf, aux, split, json.dumps(clean(assumptions), ensure_ascii=False),
             json.dumps(clean(res["metrics"])), json.dumps(res["equity"]), db.now_iso(), is_test))
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
            c.execute("INSERT INTO holdout_touches(version_id,split,window_start,window_end,backtest_id,touched_at)"
                      " VALUES(?,?,?,?,?,?)", (v["id"], "validation", start.isoformat(), end.isoformat(), bid,
                                              db.now_iso()))
        if split in ("测试集", "跨越测试集") and not is_test:
            c.execute("INSERT INTO holdout_touches(version_id,split,window_start,window_end,backtest_id,touched_at)"
                      " VALUES(?,?,?,?,?,?)", (v["id"], "test_contaminated", start.isoformat(), end.isoformat(),
                                              bid, db.now_iso()))
        if kind == "final_test" and not is_test:
            c.execute("INSERT INTO holdout_touches(version_id,split,window_start,window_end,backtest_id,touched_at)"
                      " VALUES(?,?,?,?,?,?)", (v["id"], "final_test", start.isoformat(), end.isoformat(), bid,
                                              db.now_iso()))
    return bid


def validation_runs_used(lineage_id: str) -> int:
    return db.q1("SELECT COUNT(*) AS n FROM holdout_touches h JOIN strategy_versions v ON v.id=h.version_id "
                 "WHERE h.split='validation' AND v.lineage_id=?", (lineage_id,))["n"]


def ensure_benchmark(ds: dict, df5: pd.DataFrame, start: datetime, end: datetime, r: dict) -> dict:
    ck = json.dumps(cost_model(r).as_dict() | {"cash": float(r["initial_cash"])}, sort_keys=True)
    ex = db.q1("SELECT * FROM benchmarks WHERE dataset_hash=? AND window_start=? AND window_end=? AND cost_key=?",
               (ds["sha256"], start.isoformat(), end.isoformat(), ck))
    if ex:
        return ex
    cfg = bt.BTConfig(start=start, end=end, initial_cash=float(r["initial_cash"]), cost=cost_model(r))
    b = bt.buy_and_hold(df5, cfg)
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
    k = md.resample(df5[df5["ts"] < pd.Timestamp(end)], bt_row["signal_tf"], end)
    k = k[(k["open_time"] >= pd.Timestamp(bt_row["window_start"]))]
    return k, get_trades(bt_row["id"])
