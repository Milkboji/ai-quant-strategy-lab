"""DeepSeek 优化中心业务逻辑：单策略分析、五策略横向分析、候选版本生成。"""
from __future__ import annotations

import json

import pandas as pd

from .. import db, research
from ..strategy import registry
from ..strategy.schema import describe, validate
from . import deepseek


def _trade_summary(bid: int) -> dict:
    t = research.get_trades(bid)
    if t.empty:
        return {"count": 0}
    by_reason = t.groupby("exit_reason")["pnl"].agg(["count", "sum", "mean"]).round(2)
    t["hold_min"] = (pd.to_datetime(t["exit_fill_ts"]) - pd.to_datetime(t["entry_fill_ts"])).dt.total_seconds() / 60
    return {
        "count": int(len(t)),
        "by_exit_reason": {k: {kk: float(vv) for kk, vv in v.items()} for k, v in by_reason.to_dict("index").items()},
        "avg_hold_minutes": round(float(t["hold_min"].mean()), 1),
        "largest_win": round(float(t["pnl"].max()), 2), "largest_loss": round(float(t["pnl"].min()), 2),
        "sample_trades": t.head(15)[["entry_fill_ts", "entry_price", "exit_fill_ts", "exit_price", "exit_reason",
                                     "pnl", "fee"]].round(4).to_dict("records"),
    }


def _history(v: dict) -> list[dict]:
    out = []
    for pv in registry.lineage_versions(v["lineage_id"]):
        b = research.latest_backtest_for_version(pv["id"], include_test=True, kind_split="训练集")
        out.append({"version": pv["version_no"], "change_summary": pv["change_summary"],
                    "train_metrics": {k: b["metrics"].get(k) for k in ("net_return_pct", "max_drawdown_pct",
                                                                          "trades", "win_rate_pct",
                                                                          "profit_factor")} if b else None})
    return out


def build_analyze_payload(slot_no: int) -> tuple[dict, dict, dict]:
    slot = registry.get_slot(slot_no)
    v = registry.get_version(slot["current_version_id"])
    if v is None:
        raise ValueError("该策略位还没有冻结版本")
    b = research.latest_backtest_for_version(v["id"], include_test=True, kind_split="训练集")
    if b is None:
        raise ValueError("该版本还没有完成训练集回测，无法分析（只允许基于训练集结果优化）")
    obj, _ = validate(v["schema"])
    bm = research.benchmark_for(b)
    payload = {
        "strategy_rules": describe(obj), "strategy_schema": v["schema"],
        "signal_tf_min": b["signal_tf"], "aux_tf_min": b["aux_tf"],
        "window": [b["window_start"], b["window_end"]], "data_split": b["split_label"],
        "metrics": b["metrics"], "cost_assumptions": b["assumptions"].get("cost_model"),
        "buy_and_hold": bm["metrics"] if bm else None,
        "trades": _trade_summary(b["id"]), "version_history": _history(v),
        "research_target": "90天净收益≥30%（仅作研究目标，不得以训练集结果宣称达成）",
    }
    return payload, v, b


def analyze_prompt(payload: dict) -> str:
    return (
        "以下是一个 BTC/USD 策略在训练集上的真实回测报告（JSON）。请分析并提出一个改进版本。\n"
        f"{json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
        "请输出 JSON，字段：observations（列表，每项 {fact, evidence}，只写数据中可直接验证的事实）、"
        "hypotheses（列表，写成待验证的推测）、rule_changes（列表）、param_changes（列表）、"
        "validation_method（字符串）、kill_criteria（失败或淘汰条件）、change_summary（一句话）、"
        "proposed_schema（完整的新 Strategy Schema）。\n" + deepseek.SCHEMA_GUIDE)


def run_analyze(job: dict, stage, should_stop) -> int:
    slot_no = job["slot_no"]
    stage("整理训练集报告")
    payload, v, b = build_analyze_payload(slot_no)
    stage("调用 DeepSeek（模型调用）")
    row = deepseek.call("analyze", payload, analyze_prompt(payload), slot_no=slot_no, version_id=v["id"],
                        backtest_id=b["id"], force=json.loads(job["params_json"]).get("force", False))
    return int(row["id"])


def latest_analysis(slot_no: int) -> dict | None:
    r = db.q1("SELECT * FROM llm_calls WHERE kind='analyze' AND slot_no=? ORDER BY id DESC LIMIT 1", (slot_no,))
    if r and r.get("parsed_json"):
        r["parsed"] = json.loads(r["parsed_json"])
    return r


def create_candidate(llm_call_id: int, slot_no: int | None = None, proposal: dict | None = None) -> dict:
    """把模型提出的 Schema 校验后冻结为新版本（不覆盖父版本，不自动替换策略位当前版本）。"""
    call = db.q1("SELECT * FROM llm_calls WHERE id=?", (llm_call_id,))
    if not call or call["status"] != "done":
        raise ValueError("模型调用不存在或未成功")
    parsed = json.loads(call["parsed_json"])
    prop = proposal or parsed
    schema, errs = deepseek.check_proposed(prop.get("proposed_schema"))
    if errs:
        raise ValueError("模型提出的策略未通过校验：" + "；".join(errs))
    parent_id = call["version_id"]
    if proposal is not None:
        slot = registry.get_slot(int(proposal["slot_no"]))
        parent_id = slot["current_version_id"]
        slot_no = int(proposal["slot_no"])
    parent = registry.get_version(parent_id)
    summary = prop.get("change_summary") or ""
    v = registry.freeze(slot_no or call["slot_no"], schema, nl_text=parent["nl_text"] if parent else "",
                        source="deepseek", change_summary=f"[DeepSeek#{llm_call_id}] {summary}"[:500],
                        parent_id=parent_id, llm_call_id=llm_call_id, set_current=False)
    return v


def build_batch_payload() -> dict:
    items = []
    for slot in registry.all_slots():
        v = registry.get_version(slot["current_version_id"])
        b = research.latest_backtest_for_version(v["id"], include_test=True, kind_split="训练集") if v else None
        if not v or not b:
            items.append({"slot_no": slot["slot_no"], "status": "无已完成回测"})
            continue
        obj, _ = validate(v["schema"])
        items.append({"slot_no": slot["slot_no"], "version": v["version_no"], "rules": describe(obj),
                      "strategy_schema": v["schema"], "split": b["split_label"], "signal_tf_min": b["signal_tf"],
                      "metrics": b["metrics"], "history": _history(v)})
    if not any("metrics" in i for i in items):
        raise ValueError("五个策略位都没有完成的回测，无法进行横向分析")
    return {"strategies": items}


def run_batch(job: dict, stage, should_stop) -> int:
    stage("整理五策略训练报告")
    payload = build_batch_payload()
    prompt = ("以下是五个 BTC/USD 策略位的训练集真实回测结果。请横向比较，指出共性问题，"
              "并为每个已有结果的策略位提出一个改进候选。\n"
              f"{json.dumps(payload, ensure_ascii=False, default=str)}\n\n"
              "输出 JSON：summary（总结，区分事实与假设）、next_steps（列表）、proposals（列表，每项 "
              "{slot_no, change_summary, hypothesis, proposed_schema}）。\n" + deepseek.SCHEMA_GUIDE)
    stage("调用 DeepSeek 横向分析（模型调用）")
    row = deepseek.call("batch", payload, prompt, force=json.loads(job["params_json"]).get("force", False))
    stage("校验并保存候选版本")
    created = []
    errors = []
    for p in (row["parsed"] or {}).get("proposals", []):
        try:
            v = create_candidate(row["id"], proposal=p)
            created.append(v["id"])
        except Exception as e:  # noqa: BLE001
            errors.append(f"策略{p.get('slot_no')}: {e}")
    parsed = row["parsed"]
    parsed["_created_versions"] = created
    parsed["_errors"] = errors
    db.ex("UPDATE llm_calls SET parsed_json=? WHERE id=?", (json.dumps(parsed, ensure_ascii=False), row["id"]))
    return int(row["id"])


def latest_batch() -> dict | None:
    r = db.q1("SELECT * FROM llm_calls WHERE kind='batch' ORDER BY id DESC LIMIT 1")
    if r and r.get("parsed_json"):
        r["parsed"] = json.loads(r["parsed_json"])
    return r


def parse_with_deepseek(text: str) -> tuple[dict | None, list[str], dict]:
    prompt = ("把下面的中文交易策略描述转换为 Strategy Schema。无法确定的部分放入 unresolved 列表，不要猜测。\n"
              f"策略描述：{text}\n输出 JSON：{{\"schema\": {{...}}, \"unresolved\": [..]}}\n" + deepseek.SCHEMA_GUIDE)
    row = deepseek.call("parse", {"text": text}, prompt)
    parsed = row["parsed"] or {}
    schema, errs = deepseek.check_proposed(parsed.get("schema"))
    unresolved = parsed.get("unresolved") or []
    return schema, errs + [f"待补充：{u}" for u in unresolved], row
