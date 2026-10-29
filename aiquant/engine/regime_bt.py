"""均线择时回测（美股 ETF：信号品种收盘 vs N 日均线 ± 缓冲带 → 持有交易品种或现金）。

与主引擎共用同一套交易节奏（engine/rhythm.py 的 Gate / daily_context）和市场配置（aiquant/market.py）：
- 信号：信号品种收盘价出状态（带缓冲带，中间保持原状态），次日开盘按前一日状态调仓。
- 持有期收益：开盘 → 下一交易日开盘；空仓部分为现金（收益 0）。
- 成本（按市场）：每次调仓的成交金额 × (半个点差 + 滑点 + 佣金)，卖出另加监管费。
- 交易节奏：开仓前问 Gate（冷却 / 连亏刹车，时长单位为交易日）；开仓仓位 = 波动率系数（交易品种近 N 日
  年化波动率，√252）；只影响开仓，持仓照常按信号离场。和模拟盘 etf_trend.step 用同一套规则。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .rhythm import Gate, Rhythm, daily_context, lookup


def regime_series(close: pd.Series, n: int, band: float) -> pd.Series:
    """带缓冲的均线状态：收盘 > 均线×(1+band) → 1；< 均线×(1−band) → 0；中间保持上一状态。"""
    ma = close.rolling(n).mean()
    st = pd.Series(float("nan"), index=close.index)
    st[close > ma * (1 + band)] = 1.0
    st[close < ma * (1 - band)] = 0.0
    return st.ffill().fillna(0.0)


def _side_cost(market, side: str) -> float:
    c = market.cost()
    bps = c.spread_bps / 2 + c.slippage_bps + c.fee_bps + (c.sell_fee_bps if side == "sell" else 0.0)
    return bps / 1e4


def simulate(df: pd.DataFrame, sma: int, band: float, *, market=None, rhythm: Rhythm | None = None) -> pd.DataFrame:
    """逐日模拟。df：index=美东交易日（naive Timestamp），列 sig_close / open / close（交易品种）。
    返回每日一行：size（当日开盘后持有的仓位比例）、cost、r（开盘→次日开盘收益）、pr（净收益）、event、blocked。"""
    from .. import market as mk
    m = market or mk.US_EQUITY
    rh = rhythm if (rhythm is not None and rhythm.active) else None
    st = regime_series(df["sig_close"], int(sma), float(band)).to_numpy()
    opens = df["open"].to_numpy(dtype=float)
    n = len(df)
    r = np.zeros(n)
    if n > 1:
        r[:-1] = opens[1:] / opens[:-1] - 1
    days = [d.date() for d in df.index]
    t_open = pd.to_datetime([mk.session_open(d) for d in days], utc=True)
    gate = Gate(rh, m) if rh else None
    if rh and rh.vol_target_pct:
        ctx = daily_context(pd.DataFrame({"ts": pd.to_datetime([mk.session_close(d) for d in days], utc=True),
                                          "close": df["close"].to_numpy(dtype=float)}), rh, m)
        _, vmult = lookup(ctx, t_open)
    else:
        vmult = np.ones(n)
    c_buy, c_sell = _side_cost(m, "buy"), _side_cost(m, "sell")
    size = np.zeros(n)
    cost = np.zeros(n)
    event = [""] * n
    blocked = [""] * n
    cur, eq, eq_entry = 0.0, 1.0, 1.0
    for i in range(n):
        if i > 0:
            eq *= 1 + cur * r[i - 1]
        want = st[i - 1] if i > 0 else 0.0
        if want == 1 and cur == 0:
            why = gate.check(t_open[i]) if gate else None
            if why is None:
                cur = float(vmult[i])
                cost[i] = cur * c_buy
                eq_entry = eq
                eq *= 1 - cost[i]
                event[i] = "buy"
                if gate:
                    gate.on_entry(t_open[i])
            else:
                blocked[i] = why
        elif want == 0 and cur > 0:
            cost[i] = cur * c_sell
            eq *= 1 - cost[i]
            event[i] = "sell"
            if gate:
                gate.on_exit(t_open[i], "信号离场", eq / eq_entry - 1)
            cur = 0.0
        size[i] = cur
    out = pd.DataFrame({"size": size, "cost": cost, "r": r, "event": event, "blocked": blocked}, index=df.index)
    out["pr"] = out["size"] * out["r"] - out["cost"]
    return out


def metrics(sim: pd.DataFrame, start: str, end: str, periods_per_year: int = 252) -> dict:
    x = sim["pr"][start:end]
    if len(x) < 20:
        return {"cagr": 0.0, "dd": 0.0, "ret": 0.0, "calmar": 0.0, "days": len(x), "switches": 0, "blocked": 0}
    eq = (1 + x).cumprod()
    yrs = len(x) / periods_per_year
    cagr = (eq.iloc[-1] ** (1 / yrs) - 1) * 100
    dd = float((eq / eq.cummax() - 1).min() * 100)
    w = sim["event"][start:end]
    return {"cagr": round(cagr, 2), "dd": round(dd, 2), "ret": round((eq.iloc[-1] - 1) * 100, 2),
            "calmar": round(cagr / max(abs(dd), 1.0), 3), "days": int(len(x)),
            "switches": int((w != "").sum()), "blocked": int((sim["blocked"][start:end] != "").sum())}


def backtest(df: pd.DataFrame, sma: int, band: float, start: str, end: str, *, market=None,
             rhythm: Rhythm | None = None) -> dict:
    from .. import market as mk
    m = market or mk.US_EQUITY
    return metrics(simulate(df, sma, band, market=m, rhythm=rhythm), start, end, m.periods_per_year)


def yearly(df: pd.DataFrame, sma: int, band: float, first_year: int, last_year: int, *, market=None,
           rhythm: Rhythm | None = None) -> list[dict]:
    """逐个自然年的结果（长周期体检用）：一次模拟，按年切片。"""
    from .. import market as mk
    m = market or mk.US_EQUITY
    sim = simulate(df, sma, band, market=m, rhythm=rhythm)
    out = []
    for y in range(first_year, last_year + 1):
        k = metrics(sim, f"{y}-01-01", f"{y}-12-31", m.periods_per_year)
        if k["days"] >= 120:
            out.append({"year": y, "ret": k["ret"], "dd": k["dd"], "trades": k["switches"]})
    return out
