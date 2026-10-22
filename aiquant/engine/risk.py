"""Risk Engine（研究隔离配置）。不读取、不修改任何 Paper / TQQQ 生产风控配置。"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import RESEARCH_RISK


@dataclass
class RiskDecision:
    ok: bool
    reason: str = ""


def check_buy(cash: float, notional: float, fee: float, alloc_pct: float, min_order_usd: float | None = None,
              max_alloc_pct: float | None = None) -> RiskDecision:
    min_order = RESEARCH_RISK.min_order_usd if min_order_usd is None else min_order_usd
    max_alloc = RESEARCH_RISK.max_alloc_pct if max_alloc_pct is None else max_alloc_pct
    if alloc_pct > max_alloc:
        return RiskDecision(False, f"资金使用比例 {alloc_pct}% 超过上限 {max_alloc}%")
    if notional < min_order:
        return RiskDecision(False, f"订单金额 {notional:.2f} 低于最小值 {min_order}")
    if notional + fee > cash + 1e-9:
        return RiskDecision(False, "现金不足以覆盖成交金额与手续费（禁止负现金）")
    return RiskDecision(True)


def check_sell(position_qty: float, qty: float) -> RiskDecision:
    if qty <= 0 or qty > position_qty + 1e-12:
        return RiskDecision(False, "卖出数量超过持仓（仅做多，不允许卖空）")
    return RiskDecision(True)
