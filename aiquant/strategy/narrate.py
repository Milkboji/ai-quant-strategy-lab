"""把 Strategy Schema 用大白话完整复述一遍（入场、离场、止损止盈、周期、过滤条件、仓位），供用户确认后再保存。"""
from __future__ import annotations

from .schema import validate

TF_CN = {5: "5 分钟", 15: "15 分钟", 30: "30 分钟", 60: "1 小时", 240: "4 小时", 1440: "日线"}
OP_TXT = {">": "高于", "<": "低于", ">=": "不低于", "<=": "不高于", "cross_above": "从下往上穿过", "cross_below": "从上往下跌破"}
PRICE_TXT = {"close": "收盘价", "open": "开盘价", "high": "最高价", "low": "最低价", "volume": "成交量"}


def _g(p: dict, k: str) -> str:
    v = p.get(k)
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def _ind_txt(ind: dict) -> str:
    t, p = ind["type"], ind["params"]
    bb = f"（{_g(p, 'period')} 周期，{_g(p, 'std')} 倍标准差）"
    return {
        "EMA": f"{_g(p, 'period')} 周期指数均线",
        "SMA": f"{_g(p, 'period')} 周期均线",
        "RSI": f"{_g(p, 'period')} 周期 RSI",
        "BB_UPPER": f"布林上轨{bb}",
        "BB_MID": f"布林中轨{bb}",
        "BB_LOWER": f"布林下轨{bb}",
        "BB_PCTB": f"布林 %B（价格在布林带里的位置，0 = 下轨、100 = 上轨）{bb}",
        "BB_WIDTH": f"布林带宽（上下轨距离占中轨的百分比）{bb}",
        "BB_WIDTH_RANK": f"布林带宽在最近 {_g(p, 'lookback')} 根K线里的分位（0 = 最窄）",
        "BB_SQUEEZE": f"布林收口度（最近 {_g(p, 'recent')} 根里带宽分位的最低值，越小说明刚收口）",
        "DONCHIAN_HIGH": f"最近 {_g(p, 'period')} 根已收盘K线的最高价",
        "DONCHIAN_LOW": f"最近 {_g(p, 'period')} 根已收盘K线的最低价",
        "ATR": f"{_g(p, 'period')} 周期平均波幅 ATR",
        "ROC": f"{_g(p, 'period')} 根K线的涨跌幅（%）",
        "VOL_SMA": f"{_g(p, 'period')} 周期平均成交量",
        "MACD": f"MACD 快线（{_g(p, 'fast')},{_g(p, 'slow')},{_g(p, 'signal')}）",
        "MACD_SIGNAL": "MACD 信号线",
    }.get(t, t)


def _operand(o: dict, inds: dict, aux_tf: int | None) -> str:
    if o.get("value") is not None:
        return f"{o['value']:g}"
    ref = o["ref"]
    pre = ""
    if ref.startswith("aux."):
        ref, pre = ref[4:], f"{TF_CN.get(aux_tf, '辅助周期')}的"
    if ref in PRICE_TXT:
        base = pre + PRICE_TXT[ref]
    else:
        ind = inds.get(ref)
        base = ((f"{TF_CN.get(aux_tf, '辅助周期')}的" if ind and ind.get("tf") == "aux" else pre)
                + (_ind_txt(ind) if ind else ref))
    m = o.get("mult", 1.0)
    return f"{m:g} 倍的{base}" if m not in (None, 1, 1.0) else base


def _cond_txt(c: dict, inds: dict, aux_tf: int | None) -> str:
    return f"{_operand(c['left'], inds, aux_tf)} {OP_TXT.get(c['op'], c['op'])} {_operand(c['right'], inds, aux_tf)}"


def narrate(schema: dict | None) -> list[tuple[str, str]]:
    """返回 [(小标题, 大白话)]；schema 不合法时返回空列表。"""
    obj, errs = validate(schema or {})
    if not obj:
        return []
    s = obj.model_dump()
    inds = {i["id"]: i for i in s["indicators"]}
    aux = s.get("aux_tf")
    out: list[tuple[str, str]] = []
    tf = TF_CN.get(s.get("signal_tf"), "全局设置里的周期")
    out.append(("周期", f"信号周期：{tf}K线；每根K线收盘后才判断，不看还没走完的K线。"
                + (f"辅助周期：{TF_CN.get(aux, '辅助周期')}，用来看大方向（只用已收盘的K线）。" if aux else "")))
    ent = s["entry"]["conditions"]
    trig = [c for c in ent if c["op"].startswith("cross")]
    filt = [c for c in ent if not c["op"].startswith("cross")]
    j = "并且" if s["entry"]["mode"] == "all" else "或者"
    if trig:
        out.append(("入场", f"买入信号：{f' {j} '.join(_cond_txt(c, inds, aux) for c in trig)}（K线收盘时出现就买入）。"))
        if filt:
            out.append(("过滤条件", "买入时还必须同时满足：" + "；".join(_cond_txt(c, inds, aux) for c in filt) + "。"
                        "不满足就不买，即使出现了上面的买入信号。"))
    else:
        out.append(("入场", f"买入条件：{f' {j} '.join(_cond_txt(c, inds, aux) for c in filt)}（条件成立的那根K线收盘后买入）。"))
        out.append(("过滤条件", "没有额外的过滤条件。"))
    ex = s.get("exit")
    if ex:
        jx = "任一条件成立" if ex["mode"] == "any" else "全部条件同时成立"
        out.append(("离场", f"持有期间，{jx}就卖出：" + "；".join(_cond_txt(c, inds, aux) for c in ex["conditions"]) + "。"))
    else:
        out.append(("离场", "没有离场信号规则，只靠下面的止损 / 止盈 / 移动止损 / 最长持仓离场。"))
    risk = []
    if s.get("stop_loss_pct"):
        risk.append(f"买入后亏损达到 {s['stop_loss_pct']:g}% 立即止损")
    if s.get("take_profit_pct"):
        risk.append(f"盈利达到 {s['take_profit_pct']:g}% 止盈")
    if s.get("trailing"):
        tr = s["trailing"]
        risk.append((f"盈利达到 {tr['activate_pct']:g}% 后启动移动止损，" if tr["activate_pct"] else "一开始就使用移动止损，")
                    + f"从持仓期间的最高价回撤 {tr['trail_pct']:g}% 就卖出")
    if s.get("max_hold_bars"):
        risk.append(f"最多持有 {s['max_hold_bars']} 根K线，到时间就卖出")
    out.append(("止损止盈", ("；".join(risk) + "。") if risk else "没有设置止损、止盈和移动止损（只靠离场规则）。"))
    out.append(("仓位", f"每次买入使用可用资金的 {s['alloc_pct']:g}%；只做多，不做空，不加杠杆。"))
    out.append(("系统统一规则", "回测、进化和模拟盘还会统一执行交易节奏：止损后冷却、连亏刹车、按波动率缩小仓位；"
                "入场信号超过 15 分钟没执行就放弃，不追单。"))
    return out
