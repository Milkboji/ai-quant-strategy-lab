"""策略自动进化：定期为每个模拟盘策略搜索更稳健的版本，通过检验后自动替换。

流程（每个运行中的模拟盘策略）：
1. 生成候选：在当前版本附近做参数变异（指标周期、阈值、止损/止盈/移动止损、K线周期），
   两轮爬山搜索；可选再加一个 DeepSeek 提出的候选。
2. 稳健性评分（只用训练集）：把训练窗口切成 4 个季度，
   稳健分 = 季度收益平均值 − 0.5×季度收益波动 − 0.25×最大回撤（单位 %）。成交太少的候选直接淘汰。
3. 留出检验（候选从没在这些数据上被挑选过）：
   优先用“前向新数据”（数据分区冻结之后的新行情，天数足够时）；
   否则用验证集（每条谱系最多 3 次，与研究流程共用限额）；都不满足则本轮不替换。
   候选在留出数据上的收益不得低于当前版本，回撤不得明显更深。
4. 晋级：冻结为新版本（来源 auto_evolve），保存训练集与留出回测；
   模拟盘部署在空仓时切换到新版本，有持仓时等平仓后再切换。

独立测试集永远不参与进化。所有决定写入 evolve_results 与模拟盘日志。
不保证新版本未来一定更赚钱：它只是在没见过的数据上不比旧版本差、且分段表现更稳。
"""
from __future__ import annotations

import copy
import json
import logging
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from . import db, research
from .data import market_data as md
from .engine import backtest as bt
from .strategy import registry
from .strategy.schema import INDICATOR_TYPES, schema_hash, validate

log = logging.getLogger("aiquant.evolve")

DEFAULTS = {"enabled": True, "interval_days": 7, "auto_apply": True, "use_deepseek": True,
            "max_candidates": 120, "margin": 1.0,
            # 长期亏损自动暂停：训练集一年回测亏损达到阈值，或模拟盘实际亏损达到额度的一定比例
            "pause_losers": True, "pause_train_loss_pct": -10.0, "pause_live_loss_pct": -10.0,
            # 按实力分配资金：每轮进化后按训练集 收益/回撤 重新分配额度
            "rebalance": True, "reserve_pct": 4.0, "paused_floor_pct": 6.0, "weight_min": 0.25, "weight_max": 2.0}
SEARCH_TFS = [15, 30, 60, 240, 1440]
MIN_TRADES = {15: 20, 30: 10, 60: 8, 240: 4, 1440: 3}
FORWARD_MIN_DAYS = {15: 7, 30: 10, 60: 14, 240: 21, 1440: 30}
DEFAULT_STOP = {15: 1.5, 30: 2.0, 60: 3.0, 240: 5.0, 1440: 8.0}
DEFAULT_TP = {15: 3.0, 30: 4.0, 60: 6.0, 240: 10.0, 1440: 20.0}
N_SEGMENTS = 4
HOLDOUT_TOLERANCE = 0.5      # 留出收益允许比当前版本低的百分点（容忍噪声）
HOLDOUT_DD_TOLERANCE = 3.0   # 留出最大回撤允许更深的百分点

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS evolve_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, trigger TEXT, status TEXT, started_at TEXT, finished_at TEXT,
  summary_json TEXT, is_test INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS evolve_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, deployment_id INTEGER, label TEXT, lineage_id TEXT,
  current_version_id INTEGER, candidates INTEGER, current_json TEXT, best_json TEXT, holdout_json TEXT,
  decision TEXT, reason TEXT, new_version_id INTEGER, created_at TEXT);
"""


def ensure_tables() -> None:
    with db.tx() as c:
        c.executescript(SCHEMA_SQL)


def get_config() -> dict:
    d = dict(DEFAULTS)
    d.update(db.get_setting("evolve_config") or {})
    return d


def set_config(**kw) -> dict:
    c = get_config()
    c.update(kw)
    db.set_setting("evolve_config", c)
    return c


# ---------------------------------------------------------------------------
# 候选生成
# ---------------------------------------------------------------------------

def _round_param(kind: str, v: float) -> float:
    return float(round(v * 4) / 4) if kind == "std" else float(max(1, int(round(v))))


def mutations(sch: dict) -> list[tuple[dict, str]]:
    """当前 schema 的单步邻居。返回 (schema, 变化说明)。"""
    out: list[tuple[dict, str]] = []

    def add(d: dict, why: str) -> None:
        obj, errs = validate(d)
        if not errs:
            out.append((obj.model_dump(), why))

    tf = int(sch.get("signal_tf") or 15)
    # 1) 指标参数
    for i, ind in enumerate(sch.get("indicators", [])):
        spec = INDICATOR_TYPES.get(ind["type"], {})
        for k, v in ind["params"].items():
            lo, hi = spec.get(k, (None, None))
            for m in (0.6, 0.8, 1.25, 1.6):
                nv = _round_param(k, v * m)
                if lo is not None and not (lo <= nv <= hi) or nv == v:
                    continue
                d = copy.deepcopy(sch)
                d["indicators"][i]["params"][k] = nv
                add(d, f"{ind['id']}.{k} {v:g}→{nv:g}")
    # 2) 条件中的数值阈值
    for g in ("entry", "exit"):
        grp = sch.get(g)
        if not grp:
            continue
        for ci, cond in enumerate(grp["conditions"]):
            for side in ("left", "right"):
                val = cond[side].get("value")
                if val is None:
                    continue
                for m in (0.8, 0.9, 1.1, 1.2):
                    nv = round(val * m, 2)
                    if nv == val:
                        continue
                    d = copy.deepcopy(sch)
                    d[g]["conditions"][ci][side]["value"] = nv
                    add(d, f"{'入场' if g == 'entry' else '离场'}阈值 {val:g}→{nv:g}")
    # 3) 风控
    sl, tp = sch.get("stop_loss_pct"), sch.get("take_profit_pct")
    if sl:
        for m in (0.6, 1.5):
            d = copy.deepcopy(sch); d["stop_loss_pct"] = round(sl * m, 2); add(d, f"止损 {sl:g}%→{d['stop_loss_pct']:g}%")
        d = copy.deepcopy(sch); d["stop_loss_pct"] = None; add(d, f"去掉 {sl:g}% 固定止损")
    else:
        d = copy.deepcopy(sch); d["stop_loss_pct"] = DEFAULT_STOP.get(tf, 3.0); add(d, f"增加 {d['stop_loss_pct']:g}% 止损")
    if tp:
        for m in (0.6, 1.5):
            d = copy.deepcopy(sch); d["take_profit_pct"] = round(tp * m, 2); add(d, f"止盈 {tp:g}%→{d['take_profit_pct']:g}%")
        d = copy.deepcopy(sch); d["take_profit_pct"] = None; add(d, f"去掉 {tp:g}% 固定止盈")
    else:
        d = copy.deepcopy(sch); d["take_profit_pct"] = DEFAULT_TP.get(tf, 6.0); add(d, f"增加 {d['take_profit_pct']:g}% 止盈")
    tr = sch.get("trailing")
    if tr:
        for m in (0.6, 1.5):
            d = copy.deepcopy(sch); d["trailing"] = dict(tr, trail_pct=round(tr["trail_pct"] * m, 2))
            add(d, f"移动止损回撤 {tr['trail_pct']:g}%→{d['trailing']['trail_pct']:g}%")
            d = copy.deepcopy(sch); d["trailing"] = dict(tr, activate_pct=round(tr["activate_pct"] * m, 2))
            add(d, f"移动止损启动 {tr['activate_pct']:g}%→{d['trailing']['activate_pct']:g}%")
        # 让利润多跑一段、回撤收得更紧（趋势策略常见的组合调整）
        d = copy.deepcopy(sch)
        d["trailing"] = {"activate_pct": round(tr["activate_pct"] * 1.5, 2), "trail_pct": round(tr["trail_pct"] * 0.75, 2)}
        add(d, f"移动止损 盈利{tr['activate_pct']:g}%/回撤{tr['trail_pct']:g}% → "
               f"盈利{d['trailing']['activate_pct']:g}%/回撤{d['trailing']['trail_pct']:g}%")
        d = copy.deepcopy(sch); d["trailing"] = None; add(d, "去掉移动止损")
    else:
        st = DEFAULT_STOP.get(tf, 3.0)
        d = copy.deepcopy(sch); d["trailing"] = {"activate_pct": st, "trail_pct": st}
        add(d, f"增加移动止损（盈利 {st:g}% 后回撤 {st:g}% 离场）")
    # 4) 大周期趋势过滤：只在日线收盘价站上日线均线时才允许买入（没有过滤时才添加）
    ids = {i["id"] for i in sch.get("indicators", [])}
    has_filter = any(c["left"].get("ref") in ("aux.close", "close") and c["op"] == ">" and c["right"].get("ref")
                     for c in sch["entry"]["conditions"])
    if not has_filter and len(sch.get("indicators", [])) < 12 and len(sch["entry"]["conditions"]) < 8:
        for per in (50, 100):
            d = copy.deepcopy(sch)
            rid = f"regime{per}"
            if rid in ids:
                continue
            if tf >= 1440:
                d["indicators"].append({"id": rid, "type": "EMA", "params": {"period": per}, "tf": "main"})
                d["entry"]["conditions"].append({"left": {"ref": "close"}, "op": ">", "right": {"ref": rid}})
            else:
                if d.get("aux_tf") not in (None, 1440):
                    continue
                d["aux_tf"] = 1440
                d["indicators"].append({"id": rid, "type": "EMA", "params": {"period": per}, "tf": "aux"})
                d["entry"]["conditions"].append({"left": {"ref": "aux.close"}, "op": ">", "right": {"ref": rid}})
            add(d, f"增加大周期过滤：日线收盘价在 EMA{per} 上方才买入")
    # 5) 布林带（策略里还没有布林指标时才尝试；和其他改动一样必须通过稳健分、留出检验和挑战者检验）
    #    历史检验结论：布林带单独做买卖信号在加密货币上长期不赚钱，作为趋势策略的离场 / 入场过滤更稳，
    #    所以这里只尝试这两种用法。
    has_bb = any(str(i["type"]).startswith("BB_") for i in sch.get("indicators", []))
    if not has_bb and len(sch.get("indicators", [])) < 12:
        if tf >= 240 and sch.get("exit") is not None and len(sch["exit"]["conditions"]) < 8:
            for per in (20, 30):
                d = copy.deepcopy(sch)
                rid = f"bb_mid{per}"
                d["indicators"].append({"id": rid, "type": "BB_MID", "params": {"period": per, "std": 2}, "tf": "main"})
                d["exit"]["conditions"].append({"left": {"ref": "close"}, "op": "<", "right": {"ref": rid}})
                d["exit"]["mode"] = "any"
                add(d, f"增加布林离场：收盘跌破布林中轨（{per}）即卖出")
        if len(sch["entry"]["conditions"]) < 8:
            d = copy.deepcopy(sch)
            d["indicators"].append({"id": "bb_pctb", "type": "BB_PCTB", "params": {"period": 20, "std": 2}, "tf": "main"})
            d["entry"]["conditions"].append({"left": {"ref": "bb_pctb"}, "op": ">", "right": {"value": 50}})
            add(d, "增加布林入场过滤：%B 高于 50（价格在中轨上方）才买入")
    # 6) K 线周期（相邻周期；辅助周期必须更长）
    if tf in SEARCH_TFS:
        k = SEARCH_TFS.index(tf)
        from . import rhythm
        min_tf = int(rhythm.get_config().get("min_signal_tf") or 0)
        for ntf in SEARCH_TFS[max(0, k - 1):k] + SEARCH_TFS[k + 1:k + 2]:
            if ntf < min_tf and ntf < tf:
                continue   # 交易节奏：不再往更短的周期进化（短周期长期被手续费吃掉）
            aux = sch.get("aux_tf")
            if aux and ntf >= aux:
                continue
            d = copy.deepcopy(sch); d["signal_tf"] = ntf
            add(d, f"K线周期 {tf}分钟→{ntf}分钟")
    return out


# ---------------------------------------------------------------------------
# 回测与评分
# ---------------------------------------------------------------------------

def segment_returns(equity: list[list], start: datetime, end: datetime, n: int = N_SEGMENTS) -> list[float]:
    if not equity:
        return [0.0] * n
    ts = pd.to_datetime([e[0] for e in equity], utc=True)
    eq = np.array([e[1] for e in equity], float)
    edges = [pd.Timestamp(start) + (pd.Timestamp(end) - pd.Timestamp(start)) * k / n for k in range(n + 1)]
    vals = []
    for t in edges:
        i = int(np.searchsorted(ts.values, t.to_datetime64(), "right")) - 1
        vals.append(eq[max(0, min(i, len(eq) - 1))])
    return [round((vals[k + 1] / vals[k] - 1) * 100, 3) if vals[k] else 0.0 for k in range(n)]


def score(m: dict, segs: list[float]) -> float:
    mean = float(np.mean(segs))
    sd = float(np.std(segs))
    return round(mean - 0.5 * sd - 0.25 * abs(float(m.get("max_drawdown_pct") or 0)), 3)


def evaluate(df5: pd.DataFrame, sch: dict, start: datetime, end: datetime, r: dict) -> dict:
    obj, errs = validate(sch)
    if errs:
        raise ValueError("；".join(errs))
    tf = int(sch.get("signal_tf") or 15)
    from . import rhythm
    cfg = bt.BTConfig(start=start, end=end, initial_cash=float(r["initial_cash"]),
                      max_alloc_pct=float(r["max_alloc_pct"]), cost=research.cost_model(r),
                      rhythm=rhythm.current())
    res = bt.run(df5, obj, tf, sch.get("aux_tf"), cfg)
    m = res["metrics"]
    segs = segment_returns(res["equity"], start, end)
    ok_trades = (m["trades"] or 0) >= MIN_TRADES.get(tf, 5)
    return {"tf": tf, "aux": sch.get("aux_tf"), "metrics": research.clean(m), "segments": segs,
            "positive_segments": sum(1 for s in segs if s > 0),
            "score": score(m, segs) if ok_trades else -1e9, "enough_trades": ok_trades,
            "res": res, "cfg": cfg}


def _brief(e: dict) -> dict:
    m = e["metrics"]
    return {"score": e["score"] if e["enough_trades"] else None, "return_pct": round(m["net_return_pct"], 2),
            "max_dd_pct": round(m["max_drawdown_pct"], 2), "trades": m["trades"], "segments": e["segments"],
            "positive_segments": e["positive_segments"], "tf": e["tf"]}


# ---------------------------------------------------------------------------
# 单个部署的进化
# ---------------------------------------------------------------------------

def _windows() -> dict:
    sp = research.get_split()
    vs, ts_, te = (md.to_utc(sp[k]) for k in ("val_start", "test_start", "test_end"))
    return {"train": (vs - timedelta(days=365), vs), "validation": (vs, ts_), "test_end": te}


def _deepseek_candidate(cur: dict, cur_eval: dict, label: str) -> tuple[dict | None, str]:
    from .ai import deepseek
    payload = {"strategy_schema": cur["schema"], "train_metrics": _brief(cur_eval)}
    prompt = ("以下是一个 BTC/USD 只做多策略在训练集上的回测摘要（含 4 个季度分段收益）。"
              "请提出一个更稳健的改进版本：目标是各季度表现更均衡、回撤更小、扣除 0.25% 单边手续费后仍有正收益。\n"
              f"{json.dumps(payload, ensure_ascii=False, default=str)}\n"
              "输出 JSON：{change_summary, proposed_schema}。\n" + deepseek.SCHEMA_GUIDE)
    row = deepseek.call("evolve", payload, prompt, version_id=cur["id"])
    schema, errs = deepseek.check_proposed((row.get("parsed") or {}).get("proposed_schema"))
    if errs:
        return None, "DeepSeek 候选未通过校验"
    return schema, "DeepSeek：" + str((row.get("parsed") or {}).get("change_summary") or "")[:120]


def _holdout(df_cache: dict, cur: dict, cand_sch: dict, r: dict, source: str, now: datetime) -> dict:
    """在候选从未被挑选过的数据上比较候选与当前版本。"""
    w = _windows()
    tf = int(cand_sch.get("signal_tf") or 15)
    need = max(FORWARD_MIN_DAYS.get(tf, 14), FORWARD_MIN_DAYS.get(int(cur["schema"].get("signal_tf") or 15), 14))
    fstart = w["test_end"]
    fend = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days = (fend - fstart).days
    if days >= need:
        kind, (hs, he) = "前向新数据", (fstart, fend)
    elif research.validation_runs_used(cur["lineage_id"]) < research.VALIDATION_RUN_LIMIT:
        kind, (hs, he) = "验证集", w["validation"]
    else:
        return {"kind": None, "reason": f"验证集次数已用完，前向新数据只有 {days} 天（需要 {need} 天），本轮不替换"}
    key = (hs, he)
    if key not in df_cache:
        ds = md.get_dataset(hs, he, source=source)
        df_cache[key] = (ds, md.load_dataset(ds))
    ds, df = df_cache[key]
    c = evaluate(df, cur["schema"], hs, he, r)
    k = evaluate(df, cand_sch, hs, he, r)
    cr, kr = c["metrics"]["net_return_pct"], k["metrics"]["net_return_pct"]
    cd, kd = c["metrics"]["max_drawdown_pct"], k["metrics"]["max_drawdown_pct"]
    informative = (k["metrics"]["trades"] or 0) >= 1   # 候选在留出数据上一笔都没成交，无法证明更好
    passed = informative and kr >= cr - HOLDOUT_TOLERANCE and kd >= cd - HOLDOUT_DD_TOLERANCE
    reason = (f"{kind}（{hs:%Y-%m-%d}~{he:%Y-%m-%d}）：候选 {kr:+.2f}% / 回撤 {kd:.1f}% / "
              f"{k['metrics']['trades']} 笔，当前 {cr:+.2f}% / 回撤 {cd:.1f}% / {c['metrics']['trades']} 笔")
    if not informative:
        reason += "（候选在留出数据中没有成交，证据不足）"
    return {"kind": kind, "start": hs.isoformat(), "end": he.isoformat(), "passed": passed, "reason": reason,
            "current": _brief(c), "candidate": _brief(k), "_eval": k, "_ds": ds}


def evolve_one(dep: dict, cfgv: dict, r: dict, train: tuple[datetime, datetime], train_ds: dict,
               df_train: pd.DataFrame, holdout_cache: dict, run_id: int, now: datetime, stage=None) -> dict:
    from .paper import runner as pr
    dcfg = json.loads(dep["config_json"])
    label = dcfg.get("label") or pr.dep_label(dep["id"])
    cur = registry.get_version(dep["version_id"])
    start, end = train
    cur_sch = dict(cur["schema"])
    if not cur_sch.get("signal_tf"):
        cur_sch["signal_tf"] = int(dcfg.get("signal_tf") or 15)
    cur["schema"] = cur_sch
    cur_eval = evaluate(df_train, cur_sch, start, end, r)

    seen = {schema_hash(cur_sch)}
    pool: list[tuple[dict, str, dict]] = []

    def try_add(sch: dict, why: str) -> None:
        h = schema_hash(sch)
        if h in seen or len(seen) > cfgv["max_candidates"]:
            return
        seen.add(h)
        try:
            pool.append((sch, why, evaluate(df_train, sch, start, end, r)))
        except Exception as e:  # noqa: BLE001
            log.info("候选回测失败 %s: %s", why, e)

    for sch, why in mutations(cur_sch):            # 第一轮：当前版本的单步邻居
        try_add(sch, why)
    top = sorted(pool, key=lambda x: x[2]["score"], reverse=True)[:3]
    for base, why0, _ in top:                      # 第二轮：围绕前三名再走一步
        for sch, why in mutations(base):
            try_add(sch, f"{why0}；{why}")
    ds_note = ""
    if cfgv.get("use_deepseek"):
        try:
            sch, why = _deepseek_candidate(cur, cur_eval, label)
            if sch:
                sch.setdefault("signal_tf", cur_sch["signal_tf"])
                try_add(sch, why)
        except Exception as e:  # noqa: BLE001
            ds_note = f"（DeepSeek 候选未生成：{type(e).__name__}）"

    rec = {"run_id": run_id, "deployment_id": dep["id"], "label": label, "lineage_id": cur["lineage_id"],
           "current_version_id": cur["id"], "candidates": len(pool), "current": _brief(cur_eval),
           "best": None, "holdout": None, "decision": "保留", "reason": "", "new_version_id": None}
    valid = [p for p in pool if p[2]["enough_trades"]]
    if not valid:
        rec["reason"] = "没有成交次数足够的候选" + ds_note
        return rec
    best_sch, best_why, best_eval = max(valid, key=lambda x: x[2]["score"])
    best_sch = dict(best_sch, name=cur_sch["name"])
    rec["best"] = dict(_brief(best_eval), change=best_why)
    margin = float(cfgv["margin"])
    if best_eval["score"] < cur_eval["score"] + margin:
        rec["reason"] = (f"最佳候选稳健分 {best_eval['score']:.2f} 未比当前 "
                         f"{cur_eval['score'] if cur_eval['enough_trades'] else '（成交不足）'} 高出 {margin:g} 分") + ds_note
        return rec
    if best_eval["positive_segments"] < 2 and best_eval["metrics"]["net_return_pct"] <= 0 \
            and cur_eval["metrics"]["net_return_pct"] > 0:
        rec["reason"] = "最佳候选在训练集仍亏损，而当前版本盈利，保留当前版本" + ds_note
        return rec
    if stage:
        stage(f"{label}：留出数据检验")
    ho = _holdout(holdout_cache, cur, best_sch, r, r["source"], now)
    rec["holdout"] = {k: v for k, v in ho.items() if not k.startswith("_")}
    uninformative = ho.get("kind") and not ho["passed"] and (ho.get("candidate") or {}).get("trades", 0) == 0
    if not ho.get("kind") or uninformative:
        # 历史留出数据用完或无法给出证据：登记为挑战者，在之后的真实行情上与现役版本并行模拟运行
        from . import challenger
        cid = challenger.register_btc(dep, cur, best_sch, best_why, rec["best"], bool(dep["is_test"]))
        rec["decision"] = "挑战者上线"
        rec["reason"] = f"挑战者 #{cid} 已登记（先做更早历史 + ETH 快速检验，通过则立即上线，否则在实时行情上模拟运行）；" + (ho["reason"] if ho.get("reason") else "")
        if uninformative and ho["kind"] == "验证集":
            _record_validation_touch(cur, ho)
        return rec
    if not ho["passed"]:
        rec["reason"] = "留出检验未通过：" + ho["reason"]
        if ho["kind"] == "验证集":   # 验证集查看也计入谱系限额
            _record_validation_touch(cur, ho)
        return rec
    # ---- 晋级 ----
    if not cfgv.get("auto_apply", True):
        rec["decision"], rec["reason"] = "待人工确认", "通过全部检验，自动替换已关闭：" + ho["reason"]
        return rec
    nv = registry.freeze(cur["slot_no"], best_sch, nl_text=cur["nl_text"], source="auto_evolve",
                         change_summary=f"[自动进化#{run_id}] {best_why}"[:500], parent_id=cur["id"],
                         set_current=cur["slot_no"] is not None)
    research.store_backtest(job_id=None, version=nv, slot_no=cur["slot_no"], ds=train_ds, start=start, end=end,
                            tf=best_eval["tf"], aux=best_eval["aux"], split="训练集", cfg=best_eval["cfg"],
                            res=best_eval["res"])
    k = ho["_eval"]
    research.store_backtest(job_id=None, version=nv, slot_no=cur["slot_no"], ds=ho["_ds"],
                            start=md.to_utc(ho["start"]), end=md.to_utc(ho["end"]), tf=k["tf"], aux=k["aux"],
                            split=research.split_label(md.to_utc(ho["start"]), md.to_utc(ho["end"])),
                            cfg=k["cfg"], res=k["res"])
    if cur["slot_no"] is None:   # 实时预设：记录其谱系的最新版本，供策略组合使用
        db.set_setting(f"preset_current::{cur['name']}", nv["id"])
    rec["new_version_id"] = nv["id"]
    try:
        from . import challenger
        challenger.retire_for_deployment(dep["id"], f"现役版本已直接进化为 {registry.label(nv)}")
    except Exception:  # noqa: BLE001
        pass
    rec["decision"] = "替换"
    rec["reason"] = (f"稳健分 {cur_eval['score'] if cur_eval['enough_trades'] else '成交不足'} → "
                     f"{best_eval['score']:.2f}；{ho['reason']}")
    schedule_switch(dep["id"], nv["id"], best_eval["tf"], best_eval["aux"], rec["reason"])
    return rec


def _etf_brief(m: dict | None) -> dict | None:
    if not m:
        return None
    return {"score": m.get("calmar"), "return_pct": m.get("cagr"), "max_dd_pct": m.get("dd"), "trades": m.get("switches"),
            "segments": [], "positive_segments": None, "tf": 1440,
            "change": f"{m.get('sma')} 日均线 ±{float(m.get('band', 0)):.0%}（挑选期年化，2016-2024）"}


def _record_validation_touch(cur: dict, ho: dict) -> None:
    db.ex("INSERT INTO holdout_touches(version_id,split,window_start,window_end,backtest_id,touched_at)"
          " VALUES(?,?,?,?,?,?)", (cur["id"], "validation", ho["start"], ho["end"], None, db.now_iso()))


# ---------------------------------------------------------------------------
# 模拟盘切换（空仓时立即切换；有持仓时等平仓）
# ---------------------------------------------------------------------------

def schedule_switch(did: int, vid: int, tf: int, aux: int | None, reason: str,
                    trigger: str = "evolve", actor: str | None = None) -> None:
    """登记待切换版本（自动进化 / 挑战者晋级 / 策略总台手动部署共用）：空仓且无未完成订单时才生效。"""
    from .paper import runner as pr
    d = db.q1("SELECT * FROM paper_deployments WHERE id=?", (did,))
    cfg = json.loads(d["config_json"])
    cfg["pending_switch"] = {"version_id": vid, "signal_tf": tf, "aux_tf": aux, "reason": reason,
                             "at": db.now_iso(), "trigger": trigger, "actor": actor}
    db.ex("UPDATE paper_deployments SET config_json=?, updated_at=? WHERE id=?",
          (json.dumps(cfg, ensure_ascii=False), db.now_iso(), did))
    v = registry.get_version(vid)
    if trigger == "manual":
        msg = f"手动部署：{registry.label(v)} 已确认，等待空仓且无未完成订单时切换（{reason}）"
    else:
        msg = f"自动进化：新版本 {registry.label(v)} 已通过检验，等待空仓时切换（{reason}）"
    pr.event(did, "INFO", msg[:480], bool(d["is_test"]))
    apply_pending(did)


def _ledger_qty(did: int) -> float:
    q = 0.0
    for r in db.q("SELECT side, filled_qty FROM paper_orders WHERE deployment_id=? AND status='filled'", (did,)):
        q += r["filled_qty"] if r["side"] == "buy" else -r["filled_qty"]
    return max(q, 0.0)


def apply_pending(did: int) -> bool:
    """若部署有待切换版本且当前空仓、无未完成订单，则切换。返回是否切换。"""
    from .paper import runner as pr
    d = db.q1("SELECT * FROM paper_deployments WHERE id=?", (did,))
    if not d:
        return False
    cfg = json.loads(d["config_json"])
    p = cfg.get("pending_switch")
    if not p:
        return False
    if _ledger_qty(did) > 1e-9:
        return False
    if db.q1("SELECT id FROM paper_orders WHERE deployment_id=? AND status IN ('new','submitted','partially_filled',"
             "'unknown_after_restart')", (did,)):
        return False
    ok, checks = pr.deploy_gate(p["version_id"])
    if not ok and d["broker"] == "alpaca_paper":   # 门禁只认真实数据回测；Mock 测试部署不受此限
        cfg.pop("pending_switch", None)
        db.ex("UPDATE paper_deployments SET config_json=?, updated_at=? WHERE id=?",
              (json.dumps(cfg, ensure_ascii=False), db.now_iso(), did))
        pr.event(did, "WARN", "自动进化：新版本未通过部署门禁，取消切换", bool(d["is_test"]))
        return False
    old = registry.get_version(d["version_id"])
    new = registry.get_version(p["version_id"])
    cfg.pop("pending_switch", None)
    cfg["signal_tf"], cfg["aux_tf"] = p["signal_tf"], p["aux_tf"]
    cfg["gate"] = checks
    cfg["live_since"] = db.now_iso()   # 新版本的模拟盘盈亏从切换时刻起算
    lbl = cfg.get("label") or ""
    if lbl.startswith("策略位") and "·" in lbl:
        cfg["label"] = f"{lbl.split('·')[0]}·{new['name']} v{new['version_no']}"
    elif lbl:
        tf_cn = {15: "15分钟", 30: "30分钟", 60: "1小时", 240: "4小时", 1440: "日线"}.get(p["signal_tf"], "")
        cfg["label"] = f"{new['name']} v{new['version_no']}（{tf_cn}）"
    trig = p.get("trigger") or "evolve"
    cfg.setdefault("evolve_history", []).append({"from": old["id"], "to": new["id"], "at": db.now_iso(),
                                                 "trigger": trig})
    db.ex("UPDATE paper_deployments SET version_id=?, config_json=?, updated_at=? WHERE id=?",
          (new["id"], json.dumps(cfg, ensure_ascii=False), db.now_iso(), did))
    head = "手动部署" if trig == "manual" else "自动进化"
    pr.event(did, "INFO", f"{head}：已切换 {registry.label(old)} → {registry.label(new)}，"
                          f"K线周期 {p['signal_tf']} 分钟", bool(d["is_test"]))
    try:   # 结构化切换记录（策略总台审计）
        from . import console
        console.audit(p.get("actor") or "系统", "", "切换生效", console.key_of_version(new) or cfg.get("strategy_key"),
                      new["name"], did, old["id"], new["id"], "已切换",
                      f"{console.TRIGGER_CN.get(trig, trig)}：{p.get('reason') or ''}")
    except Exception:  # noqa: BLE001
        log.exception("switch audit failed")
    return True


def apply_all_pending() -> int:
    n = 0
    for d in db.q("SELECT id FROM paper_deployments WHERE status IN ('running','paused_entries','configured')"):
        try:
            n += apply_pending(d["id"])
        except Exception:  # noqa: BLE001
            log.exception("apply_pending failed")
    return n


# ---------------------------------------------------------------------------
# 一轮进化（任务队列调用）
# ---------------------------------------------------------------------------

def run(job: dict, stage, should_stop) -> int:
    from .paper import runner as pr
    ensure_tables()
    p = json.loads(job["params_json"] or "{}")
    cfgv = get_config()
    now = datetime.now(timezone.utc)
    is_test = int(job.get("is_test") or 0)
    rid = db.ex("INSERT INTO evolve_runs(trigger,status,started_at,is_test) VALUES(?,?,?,?)",
                (p.get("trigger", "手动"), "running", db.now_iso(), is_test))
    if not is_test:   # 先记下本轮开始时间，避免失败时被定时器反复提交
        db.set_setting("evolve_last_run", {"at": db.now_iso(), "run_id": rid, "status": "running"})
    deps = db.q("SELECT * FROM paper_deployments WHERE status IN ('running','paused_entries') AND is_test=? "
                "ORDER BY id", (is_test,))
    deps = [d for d in deps if not json.loads(d["config_json"] or "{}").get("pending_offline")]   # 待下线的不再进化
    r = research.get_research()
    if is_test:
        r = dict(r, source="test")
    train = _windows()["train"]
    stage("准备训练集数据")
    train_ds = md.get_dataset(*train, source=r["source"])
    df_train = md.load_dataset(train_ds)
    cache: dict = {}
    results = []
    for i, d in enumerate(deps, 1):
        if should_stop():
            break
        lbl = json.loads(d["config_json"]).get("label") or f"#{d['id']}"
        stage(f"进化 {i}/{len(deps)}：{lbl}")
        try:
            rec = evolve_one(d, cfgv, r, train, train_ds, df_train, cache, rid, now, stage)
        except Exception as e:  # noqa: BLE001
            log.exception("evolve_one failed")
            rec = {"deployment_id": d["id"], "label": lbl, "decision": "出错", "reason": f"{type(e).__name__}: {e}"[:300],
                   "candidates": 0, "current_version_id": d["version_id"], "lineage_id": None}
        db.ex("INSERT INTO evolve_results(run_id,deployment_id,label,lineage_id,current_version_id,candidates,"
              "current_json,best_json,holdout_json,decision,reason,new_version_id,created_at) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (rid, rec["deployment_id"], rec["label"], rec.get("lineage_id"), rec.get("current_version_id"),
               rec.get("candidates", 0), json.dumps(rec.get("current"), ensure_ascii=False),
               json.dumps(rec.get("best"), ensure_ascii=False), json.dumps(rec.get("holdout"), ensure_ascii=False),
               rec["decision"], rec["reason"], rec.get("new_version_id"), db.now_iso()))
        if rec["decision"] != "替换":
            pr.event(d["id"], "INFO", f"自动进化：{rec['decision']}（测试 {rec.get('candidates', 0)} 个候选）— "
                                      f"{rec['reason']}"[:480], bool(is_test))
        results.append(rec)
    if not is_test:   # 美股 ETF 择时也参与进化（参数挑战者）
        try:
            from . import challenger
            er = challenger.etf_round(rid)
            db.ex("INSERT INTO evolve_results(run_id,deployment_id,label,lineage_id,current_version_id,candidates,"
                  "current_json,best_json,holdout_json,decision,reason,new_version_id,created_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (rid, None, "美股 ETF 择时", None, None, 27,
                   json.dumps(_etf_brief(er.get("current")), ensure_ascii=False),
                   json.dumps(_etf_brief(er.get("best")), ensure_ascii=False), None,
                   er.get("decision", "保留"), er.get("reason", ""), None, db.now_iso()))
            results.append({"decision": er.get("decision"), "candidates": 27})
        except Exception:  # noqa: BLE001
            log.exception("etf evolve failed")
    try:
        from . import challenger
        challenger.evaluate_all()
    except Exception:  # noqa: BLE001
        log.exception("challenger evaluation failed")
    try:
        try:   # 新版本的长周期体检（2021 年起逐年回测）
            from . import rhythm
            rhythm.refresh_long_all()
        except Exception:  # noqa: BLE001
            log.exception("rhythm long refresh failed")
        health_check()   # 按本轮结果立即执行长期亏损暂停 / 恢复
    except Exception:  # noqa: BLE001
        log.exception("health_check failed")
    if not is_test:
        try:
            rebalance()
        except Exception:  # noqa: BLE001
            log.exception("rebalance failed")
    summary = {"strategies": len(results), "replaced": sum(1 for x in results if x["decision"] == "替换"),
               "challengers": sum(1 for x in results if x.get("decision") == "挑战者上线"),
               "candidates": sum(x.get("candidates", 0) for x in results)}
    db.ex("UPDATE evolve_runs SET status=?, finished_at=?, summary_json=? WHERE id=?",
          ("stopped" if should_stop() else "done", db.now_iso(), json.dumps(summary, ensure_ascii=False), rid))
    if not is_test:
        db.set_setting("evolve_last_run", {"at": (db.get_setting("evolve_last_run") or {}).get("at", db.now_iso()),
                                           "run_id": rid, "status": "done", **summary})
    return rid


def submit(trigger: str = "手动", is_test: bool = False) -> tuple[int, bool]:
    from . import jobs
    return jobs.submit("evolve", slot_no=None, version_id=None, params={"trigger": trigger},
                       dedupe_key="evolve" + ("_test" if is_test else ""), is_test=is_test)


def due(now: datetime | None = None) -> bool:
    c = get_config()
    if not c.get("enabled"):
        return False
    last = db.get_setting("evolve_last_run")
    if not last:
        return True
    now = now or datetime.now(timezone.utc)
    return now - md.to_utc(last["at"]) >= timedelta(days=float(c["interval_days"]))


def next_run_at() -> datetime | None:
    c = get_config()
    last = db.get_setting("evolve_last_run")
    if not c.get("enabled"):
        return None
    if not last:
        return datetime.now(timezone.utc)
    return md.to_utc(last["at"]) + timedelta(days=float(c["interval_days"]))


def runs(limit: int = 20) -> list[dict]:
    ensure_tables()
    return db.q("SELECT * FROM evolve_runs ORDER BY id DESC LIMIT ?", (limit,))


def results_of(run_id: int) -> list[dict]:
    rows = db.q("SELECT * FROM evolve_results WHERE run_id=? ORDER BY id", (run_id,))
    for x in rows:
        for k in ("current_json", "best_json", "holdout_json"):
            x[k[:-5]] = json.loads(x[k]) if x[k] else None
    return rows


# ---------------------------------------------------------------------------
# 长期亏损自动暂停 / 恢复
#   暂停 = 状态改为“暂停新开仓”：不再买入，已有持仓仍按卖出规则管理（不强制平仓）。
#   回测原因暂停：当前版本训练集一年回测收益 ≤ 阈值（默认 -10%）。
#   实盘原因暂停：当前版本上线以来模拟盘盈亏 ≤ 额度 × 阈值（默认 -10%）。
#   恢复：自动进化换上训练集为正收益、且通过留出检验的新版本后自动恢复；
#         用户手动恢复的同一版本不再被自动暂停。
# ---------------------------------------------------------------------------

def _save_cfg(did: int, cfg: dict) -> None:
    db.ex("UPDATE paper_deployments SET config_json=?, updated_at=? WHERE id=?",
          (json.dumps(cfg, ensure_ascii=False), db.now_iso(), did))


def _active_train_return(dep: dict) -> float | None:
    """当前部署版本最近一次进化评估的训练集收益（没有进化记录时返回 None）。"""
    vid = dep["version_id"]
    r = db.q1("SELECT best_json FROM evolve_results WHERE new_version_id=? ORDER BY id DESC LIMIT 1", (vid,))
    if r and r["best_json"]:
        return (json.loads(r["best_json"]) or {}).get("return_pct")
    r = db.q1("SELECT current_json FROM evolve_results WHERE current_version_id=? AND decision<>'替换' "
              "AND decision<>'出错' ORDER BY id DESC LIMIT 1", (vid,))
    if r and r["current_json"]:
        return (json.loads(r["current_json"]) or {}).get("return_pct")
    return None


def live_pnl(dep: dict, price: float | None) -> float:
    """当前版本上线（live_since）以来的模拟盘盈亏 = 现金流 + 持仓市值。"""
    since = (dep["config"].get("live_since") or "")
    rows = db.q("SELECT side, filled_qty, filled_price, created_at FROM paper_orders WHERE deployment_id=? "
                "AND status='filled' AND created_at>=?", (dep["id"], since))
    qty = cash = 0.0
    for o in rows:
        sgn = 1 if o["side"] == "buy" else -1
        qty += sgn * o["filled_qty"]
        cash -= sgn * o["filled_qty"] * (o["filled_price"] or 0)
    return cash + max(qty, 0.0) * (price or 0.0)


def _pause(dep: dict, kind: str, reason: str) -> None:
    from .paper import runner as pr
    cfg = dep["config"]
    cfg["auto_pause"] = {"kind": kind, "reason": reason, "version_id": dep["version_id"], "at": db.now_iso()}
    _save_cfg(dep["id"], cfg)
    pr.set_status(dep["id"], "paused_entries")
    pr.event(dep["id"], "WARN", f"自动暂停新开仓（长期亏损）：{reason}。已有持仓仍按卖出规则管理；"
                                f"自动进化找到不亏的新版本后会自动恢复。"[:480], bool(dep["is_test"]))


def _resume(dep: dict, reason: str) -> None:
    from .paper import runner as pr
    cfg = dep["config"]
    cfg.pop("auto_pause", None)
    if _ledger_qty(dep["id"]) <= 1e-9:
        cfg["live_since"] = db.now_iso()
    _save_cfg(dep["id"], cfg)
    pr.set_status(dep["id"], "running")
    pr.event(dep["id"], "INFO", f"自动恢复新开仓：{reason}"[:480], bool(dep["is_test"]))
    if not dep["is_test"] and dep.get("broker") == "alpaca_paper":
        try:   # 恢复后立即重新分配额度（长周期暂停期间额度为 0）
            rebalance()
        except Exception:  # noqa: BLE001
            log.exception("rebalance after resume failed")


def _long_check(dep: dict) -> dict | None:
    """长周期体检结果（只读缓存）。节奏总控关闭或未开启体检否决时返回 None。"""
    try:
        from . import rhythm
        rc = rhythm.get_config()
        if not rc.get("enabled") or not rc.get("long_veto") or dep.get("broker") != "alpaca_paper":
            return None
        return rhythm.long_metrics(dep)
    except Exception:  # noqa: BLE001
        return None


def health_check(price: float | None = None) -> list[str]:
    """按规则对每个运行中的部署执行自动暂停/恢复。幂等，可每分钟调用。返回本次动作说明。"""
    from .paper import runner as pr
    c = get_config()
    out = []
    converted = False
    for d in db.q("SELECT id FROM paper_deployments WHERE status IN ('running','paused_entries')"):
        dep = pr.get_deployment(d["id"])
        cfg = dep["config"]
        if cfg.get("pending_offline"):   # 策略总台已下线、等待空仓：不自动恢复 / 暂停
            continue
        ap = cfg.get("auto_pause")
        # 用户手动恢复了被自动暂停的策略：记住这个版本，不再反复暂停
        if dep["status"] == "running" and ap:
            cfg["auto_pause_override"] = ap.get("version_id")
            cfg.pop("auto_pause", None)
            _save_cfg(dep["id"], cfg)
            pr.event(dep["id"], "INFO", "已手动恢复：该版本不再因长期亏损被自动暂停", bool(dep["is_test"]))
            continue
        tr = _active_train_return(dep)
        lm = _long_check(dep)
        if dep["status"] == "paused_entries" and ap:
            new_version = dep["version_id"] != ap.get("version_id")
            if ap["kind"] == "long":
                if lm is not None and lm["ok"]:
                    _resume(dep, f"长周期体检达标：近 {lm['n_years']} 年平均年收益 {lm['mean_ret']:+.1f}%，"
                                 f"盈利 {lm['pos_years']} 年" + ("（自动进化已换上新版本）" if new_version else ""))
                    out.append(f"#{dep['id']} 恢复（长周期）")
                continue
            if lm is not None and not lm["ok"]:
                # 已因其他原因暂停、长周期体检也不达标：转为长周期暂停（额度释放，达标后才恢复）
                ap.update(kind="long", reason=f"长周期体检不达标：近 {lm['n_years']} 年平均年收益 {lm['mean_ret']:+.1f}%，"
                                              f"只有 {lm['pos_years']} 年盈利", at=db.now_iso())
                cfg["auto_pause"] = ap
                _save_cfg(dep["id"], cfg)
                pr.event(dep["id"], "INFO", f"暂停原因更新为{ap['reason']}；额度将释放给长期有优势的策略", bool(dep["is_test"]))
                out.append(f"#{dep['id']} 转为长周期暂停")
                if not dep["is_test"]:
                    converted = True
                continue
            if tr is not None and tr > 0 and (ap["kind"] == "backtest" or new_version):
                _resume(dep, f"当前版本训练集收益 {tr:+.1f}%" + ("（自动进化已换上新版本）" if new_version else ""))
                out.append(f"#{dep['id']} 恢复")
            continue
        if dep["status"] != "running" or not c.get("pause_losers"):
            continue
        if cfg.get("auto_pause_override") == dep["version_id"]:
            continue
        if lm is not None and not lm["ok"]:
            _pause(dep, "long", f"长周期体检不达标：2021 年起 {lm['n_years']} 个年度平均年收益 {lm['mean_ret']:+.1f}%，"
                                f"只有 {lm['pos_years']} 年盈利（含交易节奏规则）")
            out.append(f"#{dep['id']} 暂停（长周期）")
            continue
        if tr is not None and tr <= float(c["pause_train_loss_pct"]):
            _pause(dep, "backtest", f"训练集一年回测收益 {tr:+.1f}%（阈值 {c['pause_train_loss_pct']:g}%）")
            out.append(f"#{dep['id']} 暂停（回测）")
            continue
        budget = float(cfg.get("budget_usd") or 0)
        if budget > 0 and price:
            pnl = live_pnl(dep, price)
            if pnl <= budget * float(c["pause_live_loss_pct"]) / 100:
                _pause(dep, "live", f"本版本上线以来模拟盘亏损 {pnl:,.0f} USD（额度 {budget:,.0f} 的 "
                                    f"{pnl / budget * 100:.1f}%，阈值 {c['pause_live_loss_pct']:g}%）")
                out.append(f"#{dep['id']} 暂停（实盘）")
    if converted:
        try:
            rebalance()
        except Exception:  # noqa: BLE001
            log.exception("rebalance after long conversion failed")
    return out


# ---------------------------------------------------------------------------
# 按实力分配资金额度
#   可分配 = 账户权益 × (1 − 预留)。暂停中的策略各保留 paused_floor_pct 的最低额度（恢复后可用）。
#   运行中的策略：权重 = 训练集一年 收益% / |最大回撤%|，限制在 [weight_min, weight_max]；
#   训练集亏损或无记录的按 weight_min。额度按权重分配，取整到 100 USD。
#   额度只影响之后的买入金额，不会为调额而买卖已有持仓。
# ---------------------------------------------------------------------------

def _train_metrics(dep: dict) -> dict | None:
    vid = dep["version_id"]
    r = db.q1("SELECT best_json FROM evolve_results WHERE new_version_id=? ORDER BY id DESC LIMIT 1", (vid,))
    if r and r["best_json"]:
        return json.loads(r["best_json"])
    r = db.q1("SELECT current_json FROM evolve_results WHERE current_version_id=? AND decision NOT IN ('替换','出错') "
              "ORDER BY id DESC LIMIT 1", (vid,))
    if r and r["current_json"]:
        return json.loads(r["current_json"])
    b = research.latest_backtest_for_version(vid, kind_split="训练集")
    if b:
        return {"return_pct": b["metrics"]["net_return_pct"], "max_dd_pct": b["metrics"]["max_drawdown_pct"]}
    return None


def plan_budgets(equity: float, deps: list[dict], cfgv: dict | None = None) -> dict[int, dict]:
    c = cfgv or get_config()
    other = 0.0
    try:   # 美股 ETF 趋势策略占用的资金比例，从 BTC 策略可分配资金中扣除
        from .paper import etf_trend
        ec = etf_trend.get_config()
        other = float(ec["budget_pct"]) if ec.get("enabled") else 0.0
    except Exception:  # noqa: BLE001
        pass
    total = equity * (1 - (float(c["reserve_pct"]) + other) / 100)
    paused = [d for d in deps if d["status"] == "paused_entries"]
    running = [d for d in deps if d["status"] == "running"]
    floor = equity * float(c["paused_floor_pct"]) / 100
    plan: dict[int, dict] = {}
    n_floor = 0
    for d in paused:
        if ((d.get("config") or {}).get("auto_pause") or {}).get("kind") == "long":
            plan[d["id"]] = {"budget": 0, "weight": None, "why": "长周期体检暂停：额度释放给长期有优势的策略"}
            continue
        n_floor += 1
        plan[d["id"]] = {"budget": int(floor // 100 * 100), "weight": None, "why": "暂停中，保留最低额度"}
    left = max(0.0, total - floor * n_floor)
    w = {}
    for d in running:
        lm = _long_check(d)
        if lm is not None:
            raw = 2 * lm["mean_ret"] / max(lm["mean_dd"], 1.0)
            if lm["mean_ret"] <= 0:
                w[d["id"]] = (float(c["weight_min"]), f"长周期年均收益 {lm['mean_ret']:+.1f}%，最低权重")
            else:
                w[d["id"]] = (min(max(raw, float(c["weight_min"])), float(c["weight_max"])),
                              f"长周期 {lm['n_years']} 年：年均收益 {lm['mean_ret']:+.1f}% / 年均回撤 {lm['mean_dd']:.1f}%，"
                              f"盈利 {lm['pos_years']} 年 → 权重 {raw:.2f}")
            continue
        m = _train_metrics(d) or {}
        ret, dd = m.get("return_pct"), m.get("max_dd_pct")
        if ret is None or ret <= 0:
            w[d["id"]] = (float(c["weight_min"]), f"训练集收益 {ret if ret is not None else '--'}%，最低权重")
        else:
            raw = ret / max(abs(dd or 0), 1.0)
            w[d["id"]] = (min(max(raw, float(c["weight_min"])), float(c["weight_max"])),
                          f"训练集收益 {ret:+.1f}% / 回撤 {dd:.1f}% = {raw:.2f}")
    tw = sum(x[0] for x in w.values()) or 1.0
    for did, (wt, why) in w.items():
        plan[did] = {"budget": int(left * wt / tw // 100 * 100), "weight": round(wt, 2), "why": why}
    return plan


def rebalance(equity: float | None = None) -> list[str]:
    from .paper import runner as pr
    c = get_config()
    if not c.get("rebalance"):
        return []
    deps = [pr.get_deployment(d["id"]) for d in db.q(
        "SELECT id FROM paper_deployments WHERE broker='alpaca_paper' AND is_test=0 "
        "AND status IN ('running','paused_entries') ORDER BY id")]
    if not deps:
        return []
    if equity is None:
        from .paper.brokers import AlpacaPaperAdapter
        a = AlpacaPaperAdapter().account()
        if a is None:
            return []
        equity = a.equity
    plan = plan_budgets(equity, deps, c)
    out = []
    for d in deps:
        p = plan[d["id"]]
        old = int(float(d["config"].get("budget_usd") or 0))
        if abs(p["budget"] - old) < 100:
            continue
        cfg = d["config"]
        cfg["budget_usd"] = p["budget"]
        cfg["budget_reason"] = p["why"]
        _save_cfg(d["id"], cfg)
        pr.event(d["id"], "INFO", f"资金额度调整：{old:,} → {p['budget']:,} USD（{p['why']}）", False)
        out.append(f"#{d['id']} {old:,}→{p['budget']:,}")
    db.set_setting("evolve_last_rebalance", {"at": db.now_iso(), "equity": equity, "changes": out})
    return out
