"""五个示例模板。模板只是起点，用户可自由修改；填充模板只影响当前策略位。"""
from __future__ import annotations


def _c(l, op, r, mult=1.0):
    left = {"ref": l} if isinstance(l, str) else {"value": l}
    right = {"ref": r, "mult": mult} if isinstance(r, str) else {"value": r}
    return {"left": left, "op": op, "right": right}


TEMPLATES: dict[str, dict] = {
    "趋势跟踪": {
        "text": "当BTC的EMA20向上突破EMA60时买入，EMA20下穿EMA60时平仓，固定止损2%，盈利3%后启动移动止损，回撤1.5%离场。",
        "schema": {
            "name": "趋势跟踪-EMA交叉",
            "indicators": [
                {"id": "ema20", "type": "EMA", "params": {"period": 20}},
                {"id": "ema60", "type": "EMA", "params": {"period": 60}},
            ],
            "entry": {"mode": "all", "conditions": [_c("ema20", "cross_above", "ema60")]},
            "exit": {"mode": "any", "conditions": [_c("ema20", "cross_below", "ema60")]},
            "stop_loss_pct": 2.0,
            "trailing": {"activate_pct": 3.0, "trail_pct": 1.5},
            "hypothesis": "BTC 存在持续数日的趋势段，均线交叉可捕捉主升浪。",
        },
    },
    "动量突破": {
        "text": "收盘价突破前20根K线最高价时买入，收盘价跌破前10根K线最低价时平仓，固定止损2%。",
        "schema": {
            "name": "动量突破-唐奇安",
            "indicators": [
                {"id": "dc_high20", "type": "DONCHIAN_HIGH", "params": {"period": 20}},
                {"id": "dc_low10", "type": "DONCHIAN_LOW", "params": {"period": 10}},
            ],
            "entry": {"mode": "all", "conditions": [_c("close", ">", "dc_high20")]},
            "exit": {"mode": "any", "conditions": [_c("close", "<", "dc_low10")]},
            "stop_loss_pct": 2.0,
            "hypothesis": "价格突破近期区间高点后存在短期动量延续。",
        },
    },
    "均值回归": {
        "text": "收盘价跌破布林带下轨(20,2)且RSI14小于30时买入，收盘价回到布林中轨上方时平仓，固定止损1.5%。",
        "schema": {
            "name": "均值回归-布林RSI",
            "indicators": [
                {"id": "bb_lower", "type": "BB_LOWER", "params": {"period": 20, "std": 2}},
                {"id": "bb_mid", "type": "BB_MID", "params": {"period": 20, "std": 2}},
                {"id": "rsi14", "type": "RSI", "params": {"period": 14}},
            ],
            "entry": {"mode": "all", "conditions": [_c("close", "<", "bb_lower"), _c("rsi14", "<", 30)]},
            "exit": {"mode": "any", "conditions": [_c("close", ">", "bb_mid")]},
            "stop_loss_pct": 1.5,
            "hypothesis": "震荡行情中价格过度偏离均值后倾向回归。",
        },
    },
    "波动突破": {
        "text": "收盘价突破布林带上轨(20,2)且成交量大于1.5倍20周期均量时买入，收盘价跌破EMA20时平仓，止损2%，止盈5%。",
        "schema": {
            "name": "波动突破-布林放量",
            "indicators": [
                {"id": "bb_upper", "type": "BB_UPPER", "params": {"period": 20, "std": 2}},
                {"id": "vol_sma20", "type": "VOL_SMA", "params": {"period": 20}},
                {"id": "ema20", "type": "EMA", "params": {"period": 20}},
            ],
            "entry": {"mode": "all", "conditions": [_c("close", ">", "bb_upper"),
                                                     _c("volume", ">", "vol_sma20", 1.5)]},
            "exit": {"mode": "any", "conditions": [_c("close", "<", "ema20")]},
            "stop_loss_pct": 2.0,
            "take_profit_pct": 5.0,
            "hypothesis": "放量突破波动区间上沿意味着新一轮波动扩张。",
        },
    },
    "多周期策略": {
        "text": "4小时收盘价在EMA50之上判断为上升趋势，15分钟EMA20上穿EMA50时买入；15分钟收盘价跌破EMA50或4小时收盘价跌破EMA50时平仓，固定止损1.5%。",
        "schema": {
            "name": "多周期-4H趋势15M入场",
            "signal_tf": 15,
            "aux_tf": 240,
            "indicators": [
                {"id": "h4_ema50", "type": "EMA", "tf": "aux", "params": {"period": 50}},
                {"id": "ema20", "type": "EMA", "params": {"period": 20}},
                {"id": "ema50", "type": "EMA", "params": {"period": 50}},
            ],
            "entry": {"mode": "all", "conditions": [_c("aux.close", ">", "h4_ema50"),
                                                     _c("ema20", "cross_above", "ema50")]},
            "exit": {"mode": "any", "conditions": [_c("close", "<", "ema50"), _c("aux.close", "<", "h4_ema50")]},
            "stop_loss_pct": 1.5,
            "hypothesis": "顺大周期趋势、在小周期回调后再启动时入场，可提升胜率。",
        },
    },
}

DEFAULT_SLOT_TEMPLATES = {1: "趋势跟踪", 2: "动量突破", 3: "均值回归", 4: "波动突破", 5: "多周期策略"}
