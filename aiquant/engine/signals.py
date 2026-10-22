"""Strategy Engine：把 Strategy Schema 编译为逐 K 线信号（仅使用已完成 K 线，无未来数据）。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..data.market_data import resample
from ..strategy.schema import Condition, Operand, RuleGroup, StrategySchema
from . import indicators

PRICE = ["open", "high", "low", "close", "volume"]


def build_frame(df5: pd.DataFrame, s: StrategySchema, signal_tf: int, aux_tf: int | None,
                data_end=None) -> pd.DataFrame:
    """返回主周期 K 线 + 指标列（含 aux.* 列），只保留已完成 K 线。"""
    main = resample(df5, signal_tf, data_end)
    n_partial = int((~main["complete"]).sum())
    main = main[main["complete"]].reset_index(drop=True)
    for ind in s.indicators:
        if ind.tf == "main":
            main[ind.id] = indicators.compute(main, ind.type, ind.params, ind.source).values
    if aux_tf:
        aux = resample(df5, aux_tf, data_end)
        aux = aux[aux["complete"]].reset_index(drop=True)
        cols = {}
        for ind in s.indicators:
            if ind.tf == "aux":
                cols[ind.id] = indicators.compute(aux, ind.type, ind.params, ind.source).values
        a = pd.DataFrame({"aux_close_time": aux["close_time"]})
        for p in PRICE:
            a[f"aux.{p}"] = aux[p].values
        for k, v in cols.items():
            a[k] = v
        a = a.sort_values("aux_close_time")
        # 对齐：主周期在 close_time 时刻，只能看到 close_time <= 该时刻 已收盘的辅助周期 K 线
        main = pd.merge_asof(main.sort_values("close_time"), a, left_on="close_time",
                             right_on="aux_close_time", direction="backward")
    main.attrs["partial_bars_excluded"] = n_partial
    return main


def _val(frame: pd.DataFrame, o: Operand) -> np.ndarray:
    if o.value is not None:
        return np.full(len(frame), o.value, dtype=float)
    return frame[o.ref].to_numpy(dtype=float) * o.mult


def _cond(frame: pd.DataFrame, c: Condition) -> np.ndarray:
    l, r = _val(frame, c.left), _val(frame, c.right)
    with np.errstate(invalid="ignore"):
        if c.op == ">":
            out = l > r
        elif c.op == "<":
            out = l < r
        elif c.op == ">=":
            out = l >= r
        elif c.op == "<=":
            out = l <= r
        else:
            lp, rp = np.roll(l, 1), np.roll(r, 1)
            lp[0] = np.nan
            rp[0] = np.nan
            if c.op == "cross_above":
                out = (lp <= rp) & (l > r)
            else:
                out = (lp >= rp) & (l < r)
    valid = ~(np.isnan(l) | np.isnan(r))
    return out & valid


def group_signal(frame: pd.DataFrame, g: RuleGroup | None) -> np.ndarray:
    if g is None:
        return np.zeros(len(frame), dtype=bool)
    arrs = [_cond(frame, c) for c in g.conditions]
    if g.mode == "all":
        return np.logical_and.reduce(arrs)
    return np.logical_or.reduce(arrs)
