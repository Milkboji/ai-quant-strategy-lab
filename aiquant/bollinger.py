"""布林带分析工具：多周期状态（位置 %B、带宽、收口 / 开口、沿轨运行、中轨方向）+ 大白话解读。

只使用已收盘的 K 线；只做客观描述，不构成投资建议。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from .engine import indicators as ind

PERIOD, K = 20, 2.0
TF_CN = {60: "1小时", 240: "4小时", 1440: "日线"}


def bands(c: pd.DataFrame, period: int = PERIOD, k: float = K, lookback: int = 120) -> pd.DataFrame:
    """在 K 线表上加布林带各列：bb_mid/bb_up/bb_low/bb_pctb/bb_width/bb_rank。"""
    c = c.copy()
    if c.empty:
        return c
    src = c["close"].astype(float)
    mid = ind.sma(src, period)
    sd = src.rolling(period, min_periods=period).std(ddof=0)
    c["bb_mid"], c["bb_up"], c["bb_low"] = mid, mid + k * sd, mid - k * sd
    w = (c["bb_up"] - c["bb_low"]).replace(0, np.nan)
    c["bb_pctb"] = (src - c["bb_low"]) / w * 100
    c["bb_width"] = w / mid * 100
    lb = max(30, min(lookback, len(c) - period))
    c["bb_rank"] = c["bb_width"].rolling(lb, min_periods=min(lb, 30)).rank(pct=True) * 100
    return c


def _f(x) -> float | None:
    try:
        x = float(x)
        return None if math.isnan(x) else x
    except Exception:  # noqa: BLE001
        return None


def state(c: pd.DataFrame, tf: int | None = None) -> dict:
    """最后一根已收盘 K 线的布林带状态与解读。c 只应包含已收盘 K 线。"""
    name = TF_CN.get(tf, "")
    if c is None or len(c) < PERIOD + 10:
        return {"tf": tf, "tf_cn": name, "ok": False, "label": "数据不足", "tone": "gray",
                "text": "已收盘 K 线不足 30 根，暂时无法判断。", "signals": []}
    b = bands(c)
    last = b.iloc[-1]
    close, up, mid, low = (_f(last[k]) for k in ("close", "bb_up", "bb_mid", "bb_low"))
    pctb, width, rank = _f(last["bb_pctb"]), _f(last["bb_width"]), _f(last["bb_rank"])
    mid_prev = _f(b["bb_mid"].iloc[-6]) if len(b) > 6 else None
    slope = (mid / mid_prev - 1) * 100 if mid and mid_prev else None
    w3 = b["bb_width"].tail(4).to_numpy()
    expanding = bool(len(w3) == 4 and np.all(np.diff(w3) > 0))
    recent_min_rank = _f(b["bb_rank"].iloc[-6:-1].min()) if len(b) > 6 else None
    tail = b.tail(5)
    walk_up = int((tail["close"] >= tail["bb_up"] * 0.998).sum())
    walk_dn = int((tail["close"] <= tail["bb_low"] * 1.002).sum())

    signals = []
    if rank is not None and rank <= 15:
        signals.append(("收口", "orange", f"带宽处于近期最窄的 {rank:.0f}% —— 波动被压缩，常在大行情前出现，但方向不确定。"))
    if expanding and recent_min_rank is not None and recent_min_rank <= 20 and pctb is not None:
        d = "向上" if pctb > 50 else "向下"
        signals.append(("开口", "red" if pctb > 50 else "green",
                        f"刚经历收口后带宽连续放大，价格{d}突破 —— 波动正在释放{d}。"))
    if walk_up >= 3:
        signals.append(("沿上轨", "red", f"最近 5 根有 {walk_up} 根收在上轨附近 —— 强势单边，常见于上涨趋势中，不宜仅因“碰上轨”就看空。"))
    if walk_dn >= 3:
        signals.append(("沿下轨", "green", f"最近 5 根有 {walk_dn} 根收在下轨附近 —— 弱势单边，抄底风险大。"))
    if pctb is not None and pctb > 100 and walk_up < 3:
        signals.append(("冲出上轨", "orange", "收盘在上轨之外，短线涨得急，追高要留意回落。"))
    if pctb is not None and pctb < 0 and walk_dn < 3:
        signals.append(("跌破下轨", "orange", "收盘在下轨之外，短线跌得急，可能有技术性反弹，但不代表趋势结束。"))

    above_mid = close is not None and mid is not None and close >= mid
    if walk_up >= 3 or (above_mid and slope is not None and slope > 0.3 and (pctb or 0) > 60):
        label, tone = "上升趋势", "red"
    elif walk_dn >= 3 or (not above_mid and slope is not None and slope < -0.3 and (pctb or 100) < 40):
        label, tone = "下降趋势", "green"
    elif rank is not None and rank <= 15:
        label, tone = "收口蓄势", "orange"
    else:
        label, tone = "区间震荡", "gray"
    pos_txt = ("在上轨之外" if pctb is not None and pctb > 100 else "在下轨之外" if pctb is not None and pctb < 0 else
               "靠近上轨" if (pctb or 0) >= 80 else "靠近下轨" if (pctb or 100) <= 20 else
               "在中轨上方" if above_mid else "在中轨下方")
    slope_txt = ("中轨在上行" if slope is not None and slope > 0.3 else "中轨在下行" if slope is not None and slope < -0.3
                 else "中轨走平")
    text = {"上升趋势": f"价格{pos_txt}，{slope_txt}。趋势行情里，中轨通常是回踩的支撑位；收盘跌破中轨是趋势转弱的第一个信号。",
            "下降趋势": f"价格{pos_txt}，{slope_txt}。下跌趋势里，中轨通常是反弹的压力位；站回中轨之前不宜重仓抄底。",
            "收口蓄势": f"价格{pos_txt}，带宽很窄。收口之后往往有一段较大的波动，可以等它向哪边开口再跟随。",
            "区间震荡": f"价格{pos_txt}，{slope_txt}，带宽不算窄。这种时候上下轨更像区间边界，频繁追涨杀跌容易被手续费吃掉。"}[label]
    return {"tf": tf, "tf_cn": name, "ok": True, "label": label, "tone": tone, "text": text, "signals": signals,
            "close": close, "upper": up, "mid": mid, "lower": low, "pctb": pctb, "width": width, "rank": rank,
            "mid_slope_pct": slope, "expanding": expanding, "walk_up": walk_up, "walk_dn": walk_dn,
            "bar_close": str(last.get("close_time") or last.get("ts") or ""), "frame": b}


def multi_tf(df5: pd.DataFrame, tfs=(60, 240, 1440)) -> list[dict]:
    from .data import market_data as md
    out = []
    for tf in tfs:
        try:
            c = md.resample(df5, tf)
            c = c[c["complete"]].reset_index(drop=True)
            out.append(state(c, tf))
        except Exception as e:  # noqa: BLE001
            out.append({"tf": tf, "tf_cn": TF_CN.get(tf, ""), "ok": False, "label": "出错", "tone": "gray",
                        "text": str(e)[:120], "signals": []})
    return out
