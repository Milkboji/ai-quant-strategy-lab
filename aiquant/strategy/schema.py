"""Strategy Schema：机器可执行、可校验、可哈希的确定性策略描述。

不包含、也不执行任何任意代码。仅允许白名单内的指标与比较算子。
"""
from __future__ import annotations

import hashlib
import json
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

SCHEMA_VERSION = 1

INDICATOR_TYPES = {
    "EMA": {"period": (2, 500)},
    "SMA": {"period": (2, 500)},
    "RSI": {"period": (2, 100)},
    "BB_UPPER": {"period": (5, 200), "std": (0.5, 5.0)},
    "BB_MID": {"period": (5, 200), "std": (0.5, 5.0)},
    "BB_LOWER": {"period": (5, 200), "std": (0.5, 5.0)},
    "BB_PCTB": {"period": (5, 200), "std": (0.5, 5.0)},
    "BB_WIDTH": {"period": (5, 200), "std": (0.5, 5.0)},
    "BB_WIDTH_RANK": {"period": (5, 200), "std": (0.5, 5.0), "lookback": (20, 500)},
    "BB_SQUEEZE": {"period": (5, 200), "std": (0.5, 5.0), "lookback": (20, 500), "recent": (1, 50)},
    "DONCHIAN_HIGH": {"period": (2, 500)},   # 前 N 根已完成K线最高价（不含当前）
    "DONCHIAN_LOW": {"period": (2, 500)},
    "ATR": {"period": (2, 200)},
    "ROC": {"period": (1, 200)},             # 百分比变化
    "VOL_SMA": {"period": (2, 500)},
    "MACD": {"fast": (2, 100), "slow": (3, 300), "signal": (2, 100)},
    "MACD_SIGNAL": {"fast": (2, 100), "slow": (3, 300), "signal": (2, 100)},
}
INDICATOR_CN = {
    "EMA": "指数均线", "SMA": "简单均线", "RSI": "RSI", "BB_UPPER": "布林上轨", "BB_MID": "布林中轨",
    "BB_LOWER": "布林下轨", "BB_PCTB": "布林%B", "BB_WIDTH": "布林带宽%", "BB_WIDTH_RANK": "布林带宽分位",
    "BB_SQUEEZE": "布林近期收口度", "DONCHIAN_HIGH": "前N根最高价", "DONCHIAN_LOW": "前N根最低价", "ATR": "ATR",
    "ROC": "变化率%", "VOL_SMA": "成交量均线", "MACD": "MACD", "MACD_SIGNAL": "MACD信号线",
}
PRICE_FIELDS = {"open", "high", "low", "close", "volume"}
OPS = {">", "<", ">=", "<=", "cross_above", "cross_below"}
OP_CN = {">": "大于", "<": "小于", ">=": "大于等于", "<=": "小于等于", "cross_above": "上穿", "cross_below": "下穿"}
TF_ALLOWED = {5, 15, 30, 60, 240, 1440}
RESERVED_IDS = PRICE_FIELDS | {"open_time", "close_time", "complete", "n_5m", "expected_5m", "aux_close_time", "ts"}


class Indicator(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    type: str
    tf: Literal["main", "aux"] = "main"
    params: dict[str, float]
    source: Literal["close", "open", "high", "low"] = "close"

    @model_validator(mode="after")
    def _check(self):
        if self.id in RESERVED_IDS:
            raise ValueError(f"指标 id『{self.id}』为系统保留字段")
        if self.type not in INDICATOR_TYPES:
            raise ValueError(f"不支持的指标类型: {self.type}")
        spec = INDICATOR_TYPES[self.type]
        for k in spec:
            if k not in self.params:
                raise ValueError(f"指标 {self.id} 缺少参数 {k}")
        for k, v in self.params.items():
            if k not in spec:
                raise ValueError(f"指标 {self.id} 含未知参数 {k}")
            lo, hi = spec[k]
            if not (lo <= v <= hi):
                raise ValueError(f"指标 {self.id} 参数 {k}={v} 超出范围 [{lo},{hi}]")
        if self.type.startswith("MACD") and self.params["fast"] >= self.params["slow"]:
            raise ValueError("MACD fast 必须小于 slow")
        return self


class Operand(BaseModel):
    ref: Optional[str] = None       # 指标 id 或价格字段（main 周期），aux 价格用 aux.close
    value: Optional[float] = None
    mult: float = 1.0

    @model_validator(mode="after")
    def _one(self):
        if (self.ref is None) == (self.value is None):
            raise ValueError("操作数必须且只能是 ref 或 value 之一")
        return self


class Condition(BaseModel):
    left: Operand
    op: str
    right: Operand

    @field_validator("op")
    @classmethod
    def _op(cls, v):
        if v not in OPS:
            raise ValueError(f"不支持的比较符 {v}")
        return v


class RuleGroup(BaseModel):
    mode: Literal["all", "any"] = "all"
    conditions: list[Condition] = Field(min_length=1, max_length=8)


class Trailing(BaseModel):
    activate_pct: float = Field(ge=0, le=100)
    trail_pct: float = Field(gt=0, le=50)


class StrategySchema(BaseModel):
    schema_version: int = SCHEMA_VERSION
    name: str = Field(min_length=1, max_length=60)
    signal_tf: Optional[int] = None   # None = 跟随全局
    aux_tf: Optional[int] = None
    indicators: list[Indicator] = Field(default_factory=list, max_length=12)
    entry: RuleGroup
    exit: Optional[RuleGroup] = None
    stop_loss_pct: Optional[float] = Field(default=None, gt=0, le=50)
    take_profit_pct: Optional[float] = Field(default=None, gt=0, le=500)
    trailing: Optional[Trailing] = None
    max_hold_bars: Optional[int] = Field(default=None, ge=1, le=100000)
    alloc_pct: float = Field(default=100.0, gt=0, le=100)
    direction: Literal["long_only"] = "long_only"
    hypothesis: str = ""
    signal_symbol: Optional[str] = Field(default=None, pattern=r"^[A-Z]{1,6}$")   # 美股：用另一只 ETF 的日线出信号（如 QQQ → 交易 QLD）

    @model_validator(mode="after")
    def _refs(self):
        if self.signal_tf is not None and self.signal_tf not in TF_ALLOWED:
            raise ValueError(f"不支持的主周期 {self.signal_tf}")
        if self.aux_tf is not None:
            if self.aux_tf not in TF_ALLOWED:
                raise ValueError(f"不支持的辅助周期 {self.aux_tf}")
        ids = [i.id for i in self.indicators]
        if len(ids) != len(set(ids)):
            raise ValueError("指标 id 重复")
        if any(i.tf == "aux" for i in self.indicators) and self.aux_tf is None:
            raise ValueError("使用了辅助周期指标但未设置辅助周期")
        valid = set(ids) | PRICE_FIELDS | {f"aux.{p}" for p in PRICE_FIELDS}
        groups = [self.entry] + ([self.exit] if self.exit else [])
        for g in groups:
            for c in g.conditions:
                for o in (c.left, c.right):
                    if o.ref is not None and o.ref not in valid:
                        raise ValueError(f"条件引用了未定义的指标/字段: {o.ref}")
                    if o.ref and o.ref.startswith("aux.") and self.aux_tf is None:
                        raise ValueError("引用 aux.* 字段但未设置辅助周期")
                if c.left.ref is None:
                    raise ValueError("条件左侧必须是指标或价格字段")
                if c.op.startswith("cross") and c.right.ref is None and c.right.value is None:
                    raise ValueError("交叉条件需要右侧操作数")
        if self.exit is None and self.stop_loss_pct is None and self.take_profit_pct is None \
                and self.trailing is None and self.max_hold_bars is None:
            raise ValueError("策略没有任何退出机制（退出规则/止损/止盈/移动止损/最长持仓 至少一项）")
        return self


def validate(d: dict) -> tuple[StrategySchema | None, list[str]]:
    try:
        return StrategySchema.model_validate(d), []
    except ValidationError as e:
        errs = []
        for er in e.errors():
            loc = ".".join(str(x) for x in er.get("loc", ()))
            msg = er.get("msg", "")
            errs.append(f"{loc}: {msg}" if loc else msg)
        return None, errs


def canonical(s: StrategySchema | dict) -> str:
    d = s.model_dump() if isinstance(s, StrategySchema) else dict(s)
    if d.get("signal_symbol") is None:      # 新增的可选字段：为空时不参与哈希，已有版本的哈希保持不变
        d.pop("signal_symbol", None)
    return json.dumps(d, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def schema_hash(s: StrategySchema | dict) -> str:
    return hashlib.sha256(canonical(s).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 人类可读描述（固定格式字段）
# ---------------------------------------------------------------------------

def _tf_cn(m: int | None) -> str:
    from ..config import TIMEFRAMES
    if m is None:
        return "跟随全局"
    for k, v in TIMEFRAMES.items():
        if v == m:
            return k
    return f"{m}分钟"


def _ind_label(s: StrategySchema, ref: str) -> str:
    if ref in PRICE_FIELDS:
        return {"close": "收盘价", "open": "开盘价", "high": "最高价", "low": "最低价", "volume": "成交量"}[ref]
    if ref.startswith("aux."):
        return f"辅助周期{_ind_label(s, ref[4:])}"
    for i in s.indicators:
        if i.id == ref:
            p = ",".join(f"{v:g}" for v in i.params.values())
            pre = "辅助周期" if i.tf == "aux" else ""
            return f"{pre}{i.type}({p})"
    return ref


def _opnd(s, o: Operand) -> str:
    if o.value is not None:
        return f"{o.value:g}"
    base = _ind_label(s, o.ref)
    return f"{o.mult:g}×{base}" if o.mult != 1 else base


def describe_group(s: StrategySchema, g: RuleGroup | None) -> str:
    if g is None:
        return "无（仅依靠止损/止盈）"
    parts = [f"{_opnd(s, c.left)} {OP_CN[c.op]} {_opnd(s, c.right)}" for c in g.conditions]
    j = " 且 " if g.mode == "all" else " 或 "
    return j.join(parts)


def describe(s: StrategySchema) -> dict[str, str]:
    return {
        "策略名称": s.name,
        "入场规则": describe_group(s, s.entry),
        "退出规则": describe_group(s, s.exit),
        "止损规则": f"固定止损 {s.stop_loss_pct:g}%" if s.stop_loss_pct else "无",
        "止盈规则": f"固定止盈 {s.take_profit_pct:g}%" if s.take_profit_pct else "无",
        "移动止损": (f"盈利 {s.trailing.activate_pct:g}% 后启动，回撤 {s.trailing.trail_pct:g}% 离场"
                 if s.trailing else "无"),
        "资金配置": f"单笔使用可用资金 {s.alloc_pct:g}%（仅做多，1倍）",
        "指标": "、".join(f"{i.id}={_ind_label(s, i.id)}" for i in s.indicators) or "无",
        "参数": "；".join(f"{i.id}: " + ",".join(f"{k}={v:g}" for k, v in i.params.items())
                        for i in s.indicators) or "无",
        "主周期": _tf_cn(s.signal_tf),
        "辅助周期": _tf_cn(s.aux_tf) if s.aux_tf else "无",
        "最长持仓": f"{s.max_hold_bars} 根K线" if s.max_hold_bars else "不限",
        "研究假设": s.hypothesis or "（未填写）",
    }
