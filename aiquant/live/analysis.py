"""技术指标自动解读（面向新手的大白话）。

只做客观描述与打分，不构成投资建议。分数范围 -100（偏空）到 +100（偏多）。
"""
from __future__ import annotations

import math

import pandas as pd

from ..engine import indicators as ind


def add_indicators(c: pd.DataFrame) -> pd.DataFrame:
    c = c.copy()
    if c.empty:
        return c
    c["ema9"] = ind.ema(c["close"], 9)
    c["ema21"] = ind.ema(c["close"], 21)
    c["ema50"] = ind.ema(c["close"], 50)
    c["rsi"] = ind.rsi(c["close"], 14)
    macd = ind.ema(c["close"], 12) - ind.ema(c["close"], 26)
    c["macd"] = macd
    c["macd_sig"] = macd.ewm(span=9, adjust=False, min_periods=9).mean()
    c["macd_hist"] = c["macd"] - c["macd_sig"]
    mid = ind.sma(c["close"], 20)
    sd = c["close"].rolling(20, min_periods=20).std(ddof=0)
    c["bb_mid"], c["bb_up"], c["bb_low"] = mid, mid + 2 * sd, mid - 2 * sd
    c["atr"] = ind.atr(c, 14)
    return c


def _ok(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def analyze(c: pd.DataFrame) -> dict:
    """基于已完成K线给出指标解读。"""
    if c is None or len(c) < 30:
        return {"score": 0, "verdict": "数据积累中", "tone": "gray",
                "summary": "K线数量还不够（至少需要 30 根）来计算指标，请稍等或切换到更长的周期。", "items": []}
    c = add_indicators(c)
    last, prev = c.iloc[-1], c.iloc[-2]
    items, score = [], 0

    # 1) 趋势：短期均线 vs 长期均线
    if _ok(last["ema9"]) and _ok(last["ema21"]):
        up = last["ema9"] > last["ema21"]
        cross_up = prev["ema9"] <= prev["ema21"] and up
        cross_dn = prev["ema9"] >= prev["ema21"] and not up
        txt = ("刚刚出现『金叉』：短期均线上穿长期均线，常被视为上涨信号。" if cross_up else
               "刚刚出现『死叉』：短期均线下穿长期均线，常被视为下跌信号。" if cross_dn else
               "短期均线在长期均线上方，近期走势偏强。" if up else "短期均线在长期均线下方，近期走势偏弱。")
        score += 30 if up else -30
        items.append({"name": "趋势（EMA9 / EMA21）", "tone": "green" if up else "red",
                      "value": f"{last['ema9']:,.1f} / {last['ema21']:,.1f}", "explain": txt})
    if _ok(last["ema50"]):
        above = last["close"] > last["ema50"]
        score += 15 if above else -15
        items.append({"name": "大方向（EMA50）", "tone": "green" if above else "red",
                      "value": f"{last['ema50']:,.1f}",
                      "explain": "价格在 50 周期均线上方，大方向向上。" if above else "价格在 50 周期均线下方，大方向向下。"})
    # 2) 动能：MACD
    if _ok(last["macd_hist"]):
        pos = last["macd_hist"] > 0
        rising = _ok(prev["macd_hist"]) and last["macd_hist"] > prev["macd_hist"]
        score += (15 if pos else -15) + (5 if rising else -5)
        items.append({"name": "动能（MACD 柱）", "tone": "green" if pos else "red",
                      "value": f"{last['macd_hist']:+.2f}",
                      "explain": ("上涨动能" + ("在增强" if rising else "在减弱")) if pos else
                                 ("下跌动能" + ("在减弱" if rising else "在增强"))})
    # 3) 冷热：RSI
    if _ok(last["rsi"]):
        r = last["rsi"]
        if r >= 70:
            tone, txt, sc = "orange", "RSI 超过 70，短线『过热』，追涨要小心回调。", -10
        elif r <= 30:
            tone, txt, sc = "orange", "RSI 低于 30，短线『超卖』，可能出现反弹。", 10
        elif r >= 50:
            tone, txt, sc = "green", "RSI 在 50 以上，买方力量略占优。", 5
        else:
            tone, txt, sc = "red", "RSI 在 50 以下，卖方力量略占优。", -5
        score += sc
        items.append({"name": "冷热（RSI 14）", "tone": tone, "value": f"{r:.0f}", "explain": txt})
    # 4) 位置：布林带
    if _ok(last["bb_up"]) and _ok(last["bb_low"]):
        w = last["bb_up"] - last["bb_low"]
        pos = (last["close"] - last["bb_low"]) / w if w > 0 else 0.5
        if pos > 1:
            txt, tone = "价格冲出布林带上轨，涨得很急。", "orange"
        elif pos < 0:
            txt, tone = "价格跌破布林带下轨，跌得很急。", "orange"
        elif pos > 0.8:
            txt, tone = "价格靠近布林带上沿（区间高位）。", "gray"
        elif pos < 0.2:
            txt, tone = "价格靠近布林带下沿（区间低位）。", "gray"
        else:
            txt, tone = "价格在布林带中间区域，没有明显极端。", "gray"
        items.append({"name": "位置（布林带）", "tone": tone, "value": f"{pos * 100:.0f}%", "explain": txt})
    # 5) 波动：ATR
    if _ok(last["atr"]):
        atr_pct = last["atr"] / last["close"] * 100
        items.append({"name": "波动（ATR）", "tone": "gray", "value": f"{atr_pct:.2f}%",
                      "explain": f"每根K线平均波动约 {atr_pct:.2f}%。波动越大，止损要设得越宽。"})

    score = max(-100, min(100, score))
    if score >= 40:
        verdict, tone = "偏多（看涨）", "green"
    elif score <= -40:
        verdict, tone = "偏空（看跌）", "red"
    else:
        verdict, tone = "震荡（方向不明）", "gray"
    summary = {"green": "多数指标指向上涨，但请注意：指标只反映过去的价格，不能保证未来。",
               "red": "多数指标指向下跌，持币观望通常比抄底更稳妥。",
               "gray": "指标之间互相矛盾，市场可能在横盘震荡，频繁买卖容易被手续费吃掉利润。"}[tone]
    return {"score": score, "verdict": verdict, "tone": tone, "summary": summary, "items": items,
            "frame": c}
