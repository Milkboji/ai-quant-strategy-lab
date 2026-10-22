"""Mock Execution 与成本模型。

成本拆分且只扣一次：
- 点差：成交价相对中间价偏移 spread_bps/2（买入加、卖出减）
- 滑点：成交价再偏移 slippage_bps（买入加、卖出减）
- 手续费：按成交金额 fee_bps 计（每边）
回测中价格以 K 线开盘价作为中间价代理。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostModel:
    fee_bps: float = 25.0
    slippage_bps: float = 2.0
    spread_bps: float = 1.0
    spread_source: str = "冻结代理模型"

    def as_dict(self) -> dict:
        return {"fee_bps_per_side": self.fee_bps, "slippage_bps_per_fill": self.slippage_bps,
                "spread_bps_full": self.spread_bps, "spread_source": self.spread_source}


@dataclass
class Fill:
    side: str
    mid: float
    price: float
    qty: float
    notional: float
    fee: float
    spread_cost: float
    slippage_cost: float


def fill(side: str, mid: float, qty: float, cm: CostModel) -> Fill:
    half = cm.spread_bps / 2 / 1e4
    slip = cm.slippage_bps / 1e4
    sgn = 1 if side == "buy" else -1
    price = mid * (1 + sgn * (half + slip))
    notional = price * qty
    return Fill(side, mid, price, qty, notional, notional * cm.fee_bps / 1e4,
                mid * half * qty, mid * slip * qty)


def max_buy_qty(cash: float, mid: float, alloc_pct: float, cm: CostModel, reserve_pct: float) -> float:
    """在给定现金下可买入的最大数量，保证扣除全部成本后现金不为负。"""
    budget = cash * alloc_pct / 100 * (1 - reserve_pct / 100)
    unit = mid * (1 + (cm.spread_bps / 2 + cm.slippage_bps) / 1e4) * (1 + cm.fee_bps / 1e4)
    return max(budget / unit, 0.0)
