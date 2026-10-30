"""Mock Execution 与成本模型。

成本拆分且只扣一次：
- 点差：成交价相对中间价偏移 spread_bps/2（买入加、卖出减）。
  spread_hourly（24 个值，按成交 K 线的 UTC 小时取）来自 Alpaca 历史报价的分时段估计（aiquant/spread_model.py）；
  为空时用固定的 spread_bps（旧口径：冻结代理值）。
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
    sell_fee_bps: float = 0.0          # 只在卖出时收取的费用（美股 SEC / FINRA TAF 监管费），计入手续费
    spread_hourly: tuple | None = None  # 按 UTC 小时的完整点差（bps，24 个）；None = 固定 spread_bps
    basis: str = "fixed_v1"            # 成本口径：fixed_v1 = 固定代理值（旧口径）；quotes_vN = 历史报价估计第 N 版

    def spread_at(self, at=None) -> float:
        """成交时刻的完整点差（bps）。"""
        if self.spread_hourly is None or at is None:
            return self.spread_bps
        import pandas as pd
        return float(self.spread_hourly[pd.Timestamp(at).hour])

    def as_dict(self) -> dict:
        d = {"fee_bps_per_side": self.fee_bps, "slippage_bps_per_fill": self.slippage_bps,
             "spread_bps_full": self.spread_bps, "spread_source": self.spread_source}
        if self.sell_fee_bps:
            d["sell_fee_bps"] = self.sell_fee_bps
        if self.spread_hourly is not None:
            d["spread_bps_by_utc_hour"] = [round(float(x), 3) for x in self.spread_hourly]
        if self.basis != "fixed_v1":
            d["cost_basis"] = self.basis
        return d


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


def fill(side: str, mid: float, qty: float, cm: CostModel, at=None) -> Fill:
    """at：成交 K 线时间（只在 spread_hourly 存在时用来取分时段点差）。"""
    half = (cm.spread_at(at) if cm.spread_hourly is not None else cm.spread_bps) / 2 / 1e4
    slip = cm.slippage_bps / 1e4
    sgn = 1 if side == "buy" else -1
    price = mid * (1 + sgn * (half + slip))
    notional = price * qty
    fee_bps = cm.fee_bps + (cm.sell_fee_bps if side == "sell" else 0.0)
    return Fill(side, mid, price, qty, notional, notional * fee_bps / 1e4,
                mid * half * qty, mid * slip * qty)


def max_buy_qty(cash: float, mid: float, alloc_pct: float, cm: CostModel, reserve_pct: float) -> float:
    """在给定现金下可买入的最大数量，保证扣除全部成本后现金不为负。"""
    budget = cash * alloc_pct / 100 * (1 - reserve_pct / 100)
    unit = mid * (1 + (cm.spread_bps / 2 + cm.slippage_bps) / 1e4) * (1 + cm.fee_bps / 1e4)
    return max(budget / unit, 0.0)
