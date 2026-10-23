"""DeepSeek 研究助手。

- 只做分析与"候选 Schema"生成，绝不执行模型返回的代码，绝不下单。
- 每次调用保存完整请求与原始响应（data/llm_raw/），可追溯、可防重复。
- 模型输出的策略必须通过 Strategy Schema 校验后才能冻结为新版本，新版本必须重新回测。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

from .. import config, db
from ..data import http
from ..strategy.schema import INDICATOR_TYPES, validate

SCHEMA_GUIDE = f"""
Strategy Schema（JSON）字段：
- name: 字符串
- signal_tf: 主周期分钟数，取值 5/15/30/60/240/1440 或 null（跟随全局）
- aux_tf: 辅助周期分钟数（必须大于主周期）或 null
- indicators: 列表，每项 {{"id": 小写字母开头的标识, "type": 指标类型, "tf": "main"|"aux", "params": {{...}}}}
  允许的指标类型与参数范围: {json.dumps({k: v for k, v in INDICATOR_TYPES.items()}, ensure_ascii=False)}
  DONCHIAN_HIGH/LOW 为前 N 根已完成K线的最高/最低价（不含当前K线）
  BB_PCTB = 布林%B（0=下轨，100=上轨）；BB_WIDTH = 带宽%；BB_WIDTH_RANK = 带宽在最近 lookback 根中的百分位（0=最窄）；
  BB_SQUEEZE = 前 recent 根带宽分位的最小值（小=刚收口）。历史检验：布林带单独做买卖信号在 BTC 上长期不稳，作为趋势策略的离场/过滤更有效
- entry / exit: {{"mode": "all"|"any", "conditions": [{{"left": {{"ref": 指标id或价格字段}}, "op": ">"|"<"|">="|"<="|"cross_above"|"cross_below", "right": {{"ref": ..., "mult": 1.0}} 或 {{"value": 数字}}}}]}}
  价格字段: open/high/low/close/volume；辅助周期价格: aux.open/aux.high/aux.low/aux.close/aux.volume
- stop_loss_pct, take_profit_pct: 百分比数字或 null
- trailing: {{"activate_pct": 数字, "trail_pct": 数字}} 或 null
- max_hold_bars: 整数或 null
- alloc_pct: 0-100
- direction: 固定 "long_only"
- hypothesis: 研究假设
只允许上述字段；不得包含代码。
"""

SYSTEM = ("你是严谨的量化研究员，只研究 BTC/USD 现货、仅做多、1倍、无杠杆策略。"
          "你必须区分『事实』（直接来自给定数据）与『假设』（需要验证的推测）。"
          "不得承诺收益，不得把训练集结果当成未来收益。你只输出 JSON。")


class DeepSeekError(RuntimeError):
    pass


def available() -> tuple[bool, str]:
    if os.environ.get("AIQUANT_DEEPSEEK_MOCK") == "1":
        return True, "测试模式（MOCK，不调用真实模型）"
    if config.secret("DEEPSEEK_API_KEY"):
        return True, f"已配置 DEEPSEEK_API_KEY（{config.mask(config.secret('DEEPSEEK_API_KEY'))}）"
    return False, "未配置 DEEPSEEK_API_KEY（在项目根目录 .env 中设置）"


def _extract_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise DeepSeekError("模型响应中没有 JSON")
    return json.loads(m.group(0))


def _mock_response(kind: str, payload: dict) -> str:
    """测试替身：确定性响应，仅用于自动化测试，所有记录标记为 MOCK。"""
    if kind == "parse":
        return json.dumps({"schema": {
            "name": "MOCK解析", "indicators": [{"id": "ema20", "type": "EMA", "params": {"period": 20}},
                                             {"id": "ema60", "type": "EMA", "params": {"period": 60}}],
            "entry": {"mode": "all", "conditions": [{"left": {"ref": "ema20"}, "op": "cross_above",
                                                      "right": {"ref": "ema60"}}]},
            "exit": {"mode": "any", "conditions": [{"left": {"ref": "ema20"}, "op": "cross_below",
                                                     "right": {"ref": "ema60"}}]},
            "stop_loss_pct": 2}, "unresolved": []}, ensure_ascii=False)
    if kind == "analyze":
        sch = json.loads(json.dumps(payload["strategy_schema"]))
        sch["stop_loss_pct"] = round(float(sch.get("stop_loss_pct") or 2.0) * 0.8, 2)
        sch["name"] = (sch.get("name") or "策略")[:50]
        return json.dumps({
            "observations": [{"fact": f"交易次数 {payload['metrics'].get('trades')}", "evidence": "回测指标"}],
            "hypotheses": ["MOCK：收紧止损可能降低单笔亏损（待验证）"],
            "rule_changes": [], "param_changes": [{"field": "stop_loss_pct", "to": sch["stop_loss_pct"]}],
            "validation_method": "在相同训练窗口重新回测并与父版本比较，再进行有限次验证集检验",
            "kill_criteria": "净收益低于父版本或最大回撤扩大则淘汰",
            "change_summary": "MOCK：止损收紧 20%", "proposed_schema": sch}, ensure_ascii=False)
    if kind == "batch":
        props = []
        for s in payload["strategies"]:
            if not s.get("strategy_schema"):
                continue
            sch = json.loads(json.dumps(s["strategy_schema"]))
            sch["stop_loss_pct"] = round(float(sch.get("stop_loss_pct") or 2.0) * 0.9, 2)
            props.append({"slot_no": s["slot_no"], "change_summary": "MOCK：止损收紧 10%", "proposed_schema": sch,
                          "hypothesis": "MOCK 假设"})
        return json.dumps({"summary": "MOCK 横向总结（测试数据）", "next_steps": ["MOCK 建议"],
                           "proposals": props}, ensure_ascii=False)
    raise ValueError(kind)


def call(kind: str, payload: dict, user_prompt: str, *, slot_no=None, version_id=None, backtest_id=None,
         force: bool = False) -> dict:
    """执行（或复用）一次模型调用。返回 llm_calls 行（含 parsed）。"""
    mock = os.environ.get("AIQUANT_DEEPSEEK_MOCK") == "1"
    model = "MOCK(测试)" if mock else config.DEEPSEEK_MODEL
    key = hashlib.sha256(json.dumps([kind, model, user_prompt], ensure_ascii=False).encode()).hexdigest()[:32]
    if not force:
        ex = db.q1("SELECT * FROM llm_calls WHERE dedupe_key=? AND status='done' ORDER BY id DESC LIMIT 1", (key,))
        if ex:
            ex["parsed"] = json.loads(ex["parsed_json"]) if ex["parsed_json"] else None
            ex["reused"] = True
            return ex
    cid = db.ex("INSERT INTO llm_calls(kind,slot_no,version_id,backtest_id,model,dedupe_key,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)", (kind, slot_no, version_id, backtest_id, model, key, "running",
                                           db.now_iso()))
    req = {"model": config.DEEPSEEK_MODEL, "temperature": 0.2, "response_format": {"type": "json_object"},
           "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_prompt}]}
    req_path = config.RAW_LLM_DIR / f"call_{cid}_request.json"
    raw_path = config.RAW_LLM_DIR / f"call_{cid}_response.json"
    req_path.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        if mock:
            content = _mock_response(kind, payload)
            raw = {"mock": True, "choices": [{"message": {"content": content}}]}
        else:
            api_key = config.secret("DEEPSEEK_API_KEY")
            if not api_key:
                raise DeepSeekError("未配置 DEEPSEEK_API_KEY")
            r = http.request("POST", f"{config.DEEPSEEK_URL}/chat/completions",
                             headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                             json_body=req, timeout=120)
            if r.status_code != 200:
                raise DeepSeekError(f"DeepSeek 返回 {r.status_code}: {r.text[:300]}")
            raw = r.json()
            content = raw["choices"][0]["message"]["content"]
        raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        parsed = _extract_json(content)
        db.ex("UPDATE llm_calls SET status='done', request_path=?, raw_response_path=?, parsed_json=?, finished_at=? "
              "WHERE id=?", (str(req_path), str(raw_path), json.dumps(parsed, ensure_ascii=False), db.now_iso(), cid))
    except Exception as e:  # noqa: BLE001
        db.ex("UPDATE llm_calls SET status='failed', request_path=?, error=?, finished_at=? WHERE id=?",
              (str(req_path), str(e)[:1000], db.now_iso(), cid))
        raise
    row = db.q1("SELECT * FROM llm_calls WHERE id=?", (cid,))
    row["parsed"] = parsed
    row["reused"] = False
    return row


def check_proposed(schema: Any) -> tuple[dict | None, list[str]]:
    """结构化校验 + 安全检查。"""
    if not isinstance(schema, dict):
        return None, ["模型未返回 proposed_schema 对象"]
    bad = [k for k in schema if k not in {"schema_version", "name", "signal_tf", "aux_tf", "indicators", "entry",
                                           "exit", "stop_loss_pct", "take_profit_pct", "trailing", "max_hold_bars",
                                           "alloc_pct", "direction", "hypothesis"}]
    if bad:
        return None, [f"包含不允许的字段: {bad}"]
    txt = json.dumps(schema, ensure_ascii=False)
    if re.search(r"(import |exec\(|eval\(|__|lambda|os\.|subprocess)", txt):
        return None, ["疑似包含代码片段，已拒绝"]
    obj, errs = validate(schema)
    if errs:
        return None, errs
    return obj.model_dump(), []
