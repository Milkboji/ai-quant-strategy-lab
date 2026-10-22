"""白名单技术指标（纯函数，向量化，只使用当前及以前数据）。"""
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    out = s.ewm(span=n, adjust=False, min_periods=n).mean()
    return out


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def rsi(s: pd.Series, n: int) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    r = 100 - 100 / (1 + rs)
    r = r.where(dn != 0, 100.0)
    return r.where(up.notna())


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def compute(df: pd.DataFrame, typ: str, params: dict, source: str = "close") -> pd.Series:
    p = {k: (int(v) if k in ("period", "fast", "slow", "signal") else float(v)) for k, v in params.items()}
    src = df[source]
    if typ == "EMA":
        return ema(src, p["period"])
    if typ == "SMA":
        return sma(src, p["period"])
    if typ == "RSI":
        return rsi(src, p["period"])
    if typ in ("BB_UPPER", "BB_MID", "BB_LOWER"):
        mid = sma(src, p["period"])
        sd = src.rolling(p["period"], min_periods=p["period"]).std(ddof=0)
        return {"BB_UPPER": mid + p["std"] * sd, "BB_MID": mid, "BB_LOWER": mid - p["std"] * sd}[typ]
    if typ == "DONCHIAN_HIGH":
        return df["high"].rolling(p["period"], min_periods=p["period"]).max().shift(1)
    if typ == "DONCHIAN_LOW":
        return df["low"].rolling(p["period"], min_periods=p["period"]).min().shift(1)
    if typ == "ATR":
        return atr(df, p["period"])
    if typ == "ROC":
        return src.pct_change(p["period"]) * 100
    if typ == "VOL_SMA":
        return sma(df["volume"], p["period"])
    if typ in ("MACD", "MACD_SIGNAL"):
        m = ema(src, p["fast"]) - ema(src, p["slow"])
        if typ == "MACD":
            return m
        return m.ewm(span=p["signal"], adjust=False, min_periods=p["signal"]).mean()
    raise ValueError(f"未知指标 {typ}")
