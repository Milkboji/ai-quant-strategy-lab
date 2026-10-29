"""逐笔回归比对用的确定性合成行情（与生成 golden 文件时完全相同）。"""
import numpy as np
import pandas as pd


def btc_5m(days: int = 420, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = days * 288
    ts = pd.date_range("2024-01-01", periods=n, freq="5min", tz="UTC")
    drift = 0.00002 * np.sin(np.arange(n) / (288 * 30))
    r = rng.normal(0, 0.0025, n) + drift
    close = 40000 * np.exp(np.cumsum(r))
    op = np.r_[close[0], close[:-1]]
    hi = np.maximum(op, close) * (1 + np.abs(rng.normal(0, 0.0008, n)))
    lo = np.minimum(op, close) * (1 - np.abs(rng.normal(0, 0.0008, n)))
    df = pd.DataFrame({"ts": ts, "open": op, "high": hi, "low": lo, "close": close, "volume": rng.uniform(1, 10, n)})
    keep = rng.random(n) > 0.002                     # 少量缺失 K 线，覆盖缺口处理
    return df[keep].reset_index(drop=True)


def etf_daily(n: int = 2600, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2016-01-04", periods=n)
    r = rng.normal(0.0004, 0.012, n) + 0.01 * np.sin(np.arange(n) / 50.0)
    sig = 100 * np.cumprod(1 + r)
    tr = 50 * np.cumprod(1 + 2 * r)
    return pd.DataFrame({"sig_close": sig, "open": tr, "close": tr}, index=idx)


SCHEMAS = [
    {"name": "EMA 交叉 + 止损 + 移动止损", "signal_tf": 60,
     "indicators": [{"id": "f", "type": "EMA", "params": {"period": 20}}, {"id": "s", "type": "EMA", "params": {"period": 60}}],
     "entry": {"mode": "all", "conditions": [{"left": {"ref": "f"}, "op": "cross_above", "right": {"ref": "s"}}]},
     "exit": {"mode": "any", "conditions": [{"left": {"ref": "f"}, "op": "cross_below", "right": {"ref": "s"}}]},
     "stop_loss_pct": 2.0, "trailing": {"activate_pct": 3.0, "trail_pct": 1.5}},
    {"name": "RSI 超卖 + 止盈 + 最长持仓", "signal_tf": 15,
     "indicators": [{"id": "rsi", "type": "RSI", "params": {"period": 14}}],
     "entry": {"mode": "all", "conditions": [{"left": {"ref": "rsi"}, "op": "<", "right": {"value": 30}}]},
     "exit": {"mode": "any", "conditions": [{"left": {"ref": "rsi"}, "op": ">", "right": {"value": 60}}]},
     "take_profit_pct": 3.0, "stop_loss_pct": 2.5, "max_hold_bars": 96},
    {"name": "布林上轨突破 + 中轨离场", "signal_tf": 240,
     "indicators": [{"id": "bu", "type": "BB_UPPER", "params": {"period": 20, "std": 2.0}},
                    {"id": "bm", "type": "BB_MID", "params": {"period": 20, "std": 2.0}}],
     "entry": {"mode": "all", "conditions": [{"left": {"ref": "close"}, "op": ">", "right": {"ref": "bu"}}]},
     "exit": {"mode": "any", "conditions": [{"left": {"ref": "close"}, "op": "<", "right": {"ref": "bm"}}]},
     "stop_loss_pct": 4.0},
    {"name": "多周期：1 小时突破 + 日线趋势", "signal_tf": 60, "aux_tf": 1440,
     "indicators": [{"id": "dh", "type": "DONCHIAN_HIGH", "params": {"period": 20}},
                    {"id": "dl", "type": "DONCHIAN_LOW", "params": {"period": 10}},
                    {"id": "aux_sma", "type": "SMA", "tf": "aux", "params": {"period": 20}}],
     "entry": {"mode": "all", "conditions": [{"left": {"ref": "close"}, "op": ">", "right": {"ref": "dh"}},
                                             {"left": {"ref": "aux.close"}, "op": ">", "right": {"ref": "aux_sma"}}]},
     "exit": {"mode": "any", "conditions": [{"left": {"ref": "close"}, "op": "<", "right": {"ref": "dl"}}]}},
    {"name": "日线 MACD", "signal_tf": 1440,
     "indicators": [{"id": "m", "type": "MACD", "params": {"fast": 12, "slow": 26, "signal": 9}},
                    {"id": "ms", "type": "MACD_SIGNAL", "params": {"fast": 12, "slow": 26, "signal": 9}}],
     "entry": {"mode": "all", "conditions": [{"left": {"ref": "m"}, "op": "cross_above", "right": {"ref": "ms"}}]},
     "exit": {"mode": "any", "conditions": [{"left": {"ref": "m"}, "op": "cross_below", "right": {"ref": "ms"}}]},
     "stop_loss_pct": 6.0, "alloc_pct": 60},
]
