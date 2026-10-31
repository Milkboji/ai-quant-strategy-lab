"""买卖点看板：BTC K 线上同时显示所有策略的真实成交和规则信号（可按策略筛选）。数据见 aiquant/chartboard.py。"""
from __future__ import annotations

from datetime import timedelta, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .. import chartboard as cb
from . import components as C

BJ = timezone(timedelta(hours=8))
PALETTE = ["#2563EB", "#DC2626", "#059669", "#D97706", "#7C3AED", "#DB2777", "#0891B2", "#65A30D", "#9333EA",
           "#EA580C", "#0F766E", "#B91C1C", "#4F46E5", "#CA8A04"]
UP, DN = "#16A34A", "#DC2626"


def _bj(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, utc=True).dt.tz_convert(BJ).dt.tz_localize(None)


@st.cache_data(ttl=60, show_spinner=False)
def _load(tf: int, days: int, ids: tuple, show_signals: bool, _minute: str) -> dict:
    d = cb.board(tf, days, list(ids) if ids else None, show_signals)
    return {k: v for k, v in d.items()}


def render() -> None:
    st.markdown('<div class="aq-title">买卖点看板</div>', unsafe_allow_html=True)
    st.caption("BTC/USD K 线上叠加每个策略的真实成交（实心三角，Alpaca Paper 模拟盘台账）和规则信号（空心圆，"
               "按策略正在运行的版本算出、入场/退出规则刚成立的位置）。信号没有成交的常见原因：止损/止盈另算、"
               "交易节奏（冷却、连亏刹车）、额度为 0、暂停新开仓、已有持仓。时间为北京时间。")
    strat = cb.strategies()
    c = st.columns([1, 1, 2.2, 1])
    tf = c[0].selectbox("K 线周期", list(cb.TF_CN), index=0, format_func=lambda m: cb.TF_CN[m], key="cb_tf")
    days = c[1].selectbox("时间范围", [3, 7, 14, 30, 90, 180], index=2, format_func=lambda d: f"最近 {d} 天", key="cb_days")
    opts = {s["id"]: s["label"] + ("" if s["active"] else "（已停止）") for s in strat}
    default = [s["id"] for s in strat if s["active"]]
    ids = c[2].multiselect("策略", list(opts), default=default, format_func=lambda i: opts[i], key="cb_ids")
    show_sig = c[3].toggle("显示信号", value=True, key="cb_sig")
    if not ids:
        st.info("请至少选择一个策略")
        return
    with st.spinner("读取行情、成交并计算信号…"):
        minute = pd.Timestamp.now("UTC").floor("min").isoformat()
        d = _load(int(tf), int(days), tuple(sorted(ids)), bool(show_sig), minute)
    bars, f, sig = d["bars"], d["fills"], d["signals"]
    if bars is None or not len(bars):
        st.warning("本地没有这段时间的 BTC 行情")
        return
    labels = sorted(set(f["label"]) | set(sig["label"]) | {opts[i].replace("（已停止）", "") for i in ids})
    color = {lb: PALETTE[i % len(PALETTE)] for i, lb in enumerate(labels)}
    x = _bj(bars["open_time"])
    fig = go.Figure()
    fig.add_trace(go.Candlestick(x=x, open=bars["open"], high=bars["high"], low=bars["low"], close=bars["close"],
                                 name="BTC/USD", increasing_line_color=UP, increasing_fillcolor=UP,
                                 decreasing_line_color=DN, decreasing_fillcolor=DN, line=dict(width=1), showlegend=False))
    span = float(bars["high"].max() - bars["low"].min()) or 1.0
    pad = span * 0.012
    for lb in labels:
        col = color[lb]
        for side, sym, dy, word in (("buy", "triangle-up", -pad, "买"), ("sell", "triangle-down", pad, "卖")):
            t = f[(f["label"] == lb) & (f["side"] == side)]
            if len(t):
                fig.add_trace(go.Scatter(
                    x=_bj(t["ts"]), y=t["price"] + dy * 1.6, mode="markers", name=f"{lb} · 成交{word}",
                    legendgroup=lb, marker=dict(symbol=sym, size=13, color=col, line=dict(color="white", width=1)),
                    customdata=t[["price", "qty", "source"]],
                    hovertemplate=f"{C.esc(lb)}<br>成交{word} %{{customdata[0]:,.2f}} · 数量 %{{customdata[1]:.6f}}"
                                  "<br>%{customdata[2]}<extra></extra>"))
            s = sig[(sig["label"] == lb) & (sig["side"] == side)] if show_sig and len(sig) else sig.iloc[0:0]
            if len(s):
                fig.add_trace(go.Scatter(
                    x=_bj(s["ts"]), y=s["price"] + dy * 3.2, mode="markers", name=f"{lb} · 信号{word}",
                    legendgroup=lb, marker=dict(symbol="circle-open", size=9, color=col, line=dict(width=2)),
                    customdata=s[["price"]],
                    hovertemplate=f"{C.esc(lb)}<br>{'入场' if side == 'buy' else '退出'}信号 · 收盘 %{{customdata[0]:,.2f}}"
                                  "<extra></extra>"))
    fig.update_layout(height=640, margin=dict(l=6, r=6, t=6, b=6), paper_bgcolor="white", plot_bgcolor="white",
                      xaxis_rangeslider_visible=False, hovermode="closest", uirevision=f"{tf}-{days}",
                      legend=dict(orientation="h", y=-0.08, x=0, font=dict(size=10)), font=dict(size=11, color="#475569"))
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(gridcolor="#F1F5F9", tickformat=",.0f")
    st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": True, "displaylogo": False})
    if d["errors"]:
        st.caption("部分策略的信号没算出来：" + "；".join(d["errors"]))
    m = st.columns(3)
    m[0].metric("真实成交", f"{len(f)} 笔")
    m[1].metric("规则信号", f"{len(sig)} 个" if show_sig else "--")
    m[2].metric("显示的策略", f"{len(ids)} 个")
    with st.expander(f"成交明细（{len(f)} 笔）"):
        if len(f):
            t = f.copy()
            t["时间"] = _bj(t["ts"]).dt.strftime("%m-%d %H:%M")
            t["方向"] = t["side"].map({"buy": "买入", "sell": "卖出"})
            st.dataframe(t[["时间", "label", "方向", "price", "qty", "source"]].rename(
                columns={"label": "策略", "price": "成交价", "qty": "数量", "source": "台账"}).iloc[::-1],
                hide_index=True, use_container_width=True)
        else:
            st.caption("这段时间没有成交")
