"""通用 UI 组件：数值格式、K 线图、对比图。所有图表数据均来自后端真实结果。"""
from __future__ import annotations

import html
import math

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

GREEN, RED, BLUE, PURPLE, ORANGE, GRAY = "#16A34A", "#DC2626", "#2563EB", "#7C3AED", "#EA8A0C", "#94A3B8"
SLOT_COLORS = {1: "#2563EB", 2: "#16A34A", 3: "#EA8A0C", 4: "#DC2626", 5: "#7C3AED", "BH": "#64748B"}


def esc(s) -> str:
    return html.escape(str(s))


def _num(x):
    if x is None:
        return None
    if isinstance(x, str):
        return math.inf if x == "inf" else None
    try:
        if isinstance(x, float) and math.isnan(x):
            return None
    except Exception:  # noqa: BLE001
        return None
    return x


def pct(x, sign=True, digits=2) -> str:
    x = _num(x)
    if x is None:
        return "--"
    return f"{x:+.{digits}f}%" if sign else f"{x:.{digits}f}%"


def usd(x, sign=False, digits=0) -> str:
    x = _num(x)
    if x is None:
        return "--"
    return f"{x:+,.{digits}f} USD" if sign else f"{x:,.{digits}f} USD"


def num(x, digits=2) -> str:
    x = _num(x)
    if x is None:
        return "--"
    if x == math.inf:
        return "∞"
    return f"{x:,.{digits}f}"


def tone(x) -> str:
    x = _num(x)
    if x is None:
        return "muted"
    return "pos" if x > 0 else ("neg" if x < 0 else "")


def pill(label: str, t: str) -> str:
    return f'<span class="aq-pill {t}">{esc(label)}</span>'


def section(title: str, purple: bool = False, right: str = "") -> None:
    st.markdown(f'<div class="aq-section"><span class="bar {"purple" if purple else ""}"></span>{esc(title)}'
                f'<span style="margin-left:auto">{right}</span></div>', unsafe_allow_html=True)


def metrics_grid(m: dict | None) -> str:
    m = m or {}
    def cell(label, val, cls="", sub=""):
        return (f'<div class="aq-m"><div class="l">{label}</div><div class="n {cls}">{val}</div>'
                f'<div class="s {cls}">{sub}</div></div>')
    cells = [
        cell("净收益", pct(m.get("net_return_pct")), tone(m.get("net_return_pct")), usd(m.get("net_profit"), True)),
        cell("最大回撤", pct(m.get("max_drawdown_pct")), "neg" if m.get("max_drawdown_pct") else "muted"),
        cell("胜率", pct(m.get("win_rate_pct"), sign=False, digits=1), ""),
        cell("Profit Factor", num(m.get("profit_factor")), "sm"),
        cell("交易次数", num(m.get("trades"), 0) if m.get("trades") is not None else "--", "sm"),
        cell("手续费", usd(m.get("fees")), "sm"),
        cell("点差成本", usd(m.get("spread_cost")), "sm"),
        cell("滑点成本", usd(m.get("slippage_cost")), "sm"),
        cell("总成本", usd(m.get("total_cost")), "sm"),
    ]
    return '<div class="aq-metrics">' + "".join(cells) + "</div>"


def kv(rows: list[tuple[str, str]]) -> str:
    return '<div class="aq-kv">' + "".join(
        f'<div class="k">{esc(k)}</div><div class="v" title="{esc(v)}">{esc(v)}</div>' for k, v in rows) + "</div>"


def _layout(fig: go.Figure, height: int, legend=False) -> go.Figure:
    fig.update_layout(height=height, margin=dict(l=4, r=4, t=6, b=4), paper_bgcolor="white", plot_bgcolor="white",
                      showlegend=legend, font=dict(size=10, color="#4B5563"),
                      legend=dict(orientation="h", y=1.08, x=0, font=dict(size=10)),
                      hovermode="x unified")
    fig.update_xaxes(showgrid=False, linecolor="#E5E7EB", rangeslider_visible=False)
    fig.update_yaxes(gridcolor="#F1F5F9", linecolor="#E5E7EB", side="right")
    return fig


def kline_figure(k: pd.DataFrame, trades: pd.DataFrame, height: int = 180, last_n: int | None = 160,
                 show_volume: bool = True) -> go.Figure:
    if last_n:
        k = k.tail(last_n)
    t0 = k["open_time"].min() if len(k) else None
    fig = go.Figure()
    fig.add_trace(go.Candlestick(x=k["open_time"], open=k["open"], high=k["high"], low=k["low"], close=k["close"],
                                 increasing_line_color=GREEN, decreasing_line_color=RED,
                                 increasing_fillcolor=GREEN, decreasing_fillcolor=RED, name="BTC/USD",
                                 line=dict(width=1)))
    if show_volume and len(k):
        vmax = k["volume"].max() or 1
        lo, hi = k["low"].min(), k["high"].max()
        base = lo - (hi - lo) * 0.02
        fig.add_trace(go.Bar(x=k["open_time"], y=k["volume"] / vmax * (hi - lo) * 0.18, base=base,
                             marker_color=["rgba(22,163,74,.35)" if c >= o else "rgba(220,38,38,.35)"
                                           for o, c in zip(k["open"], k["close"])],
                             name="成交量", hoverinfo="skip"))
    if trades is not None and len(trades) and t0 is not None:
        tt = trades.copy()
        tt["entry_fill_ts"] = pd.to_datetime(tt["entry_fill_ts"], utc=True)
        tt["exit_fill_ts"] = pd.to_datetime(tt["exit_fill_ts"], utc=True)
        en = tt[tt["entry_fill_ts"] >= t0]
        ex = tt[tt["exit_fill_ts"] >= t0]
        fig.add_trace(go.Scatter(x=en["entry_fill_ts"], y=en["entry_price"], mode="markers", name="买入",
                                 marker=dict(symbol="triangle-up", size=10, color=GREEN, line=dict(color="white", width=1)),
                                 hovertemplate="买入 %{y:,.2f}<extra></extra>"))
        fig.add_trace(go.Scatter(x=ex["exit_fill_ts"], y=ex["exit_price"], mode="markers", name="卖出",
                                 marker=dict(symbol="triangle-down", size=10, color=RED, line=dict(color="white", width=1)),
                                 text=ex["exit_reason"], hovertemplate="卖出 %{y:,.2f} %{text}<extra></extra>"))
    return _layout(fig, height)


def equity_compare(series: dict, height: int = 260) -> go.Figure:
    fig = go.Figure()
    for label, (eq, color, init) in series.items():
        if not eq:
            continue
        df = pd.DataFrame(eq, columns=["ts", "eq"])
        df["ts"] = pd.to_datetime(df["ts"])
        fig.add_trace(go.Scatter(x=df["ts"], y=(df["eq"] / init - 1) * 100, mode="lines", name=label,
                                 line=dict(width=1.6, color=color, dash="dot" if label.startswith("BTC") else None)))
    fig.update_yaxes(ticksuffix="%")
    return _layout(fig, height, legend=True)


def bar_compare(labels, values, height=240, suffix="%", colors=None, horizontal=False) -> go.Figure:
    vals = [_num(v) for v in values]
    cols = colors or [GREEN if (v or 0) >= 0 else RED for v in vals]
    txt = [("--" if v is None else f"{v:+.2f}{suffix}" if suffix == "%" else f"{v:,.0f}") for v in vals]
    if horizontal:
        fig = go.Figure(go.Bar(y=labels, x=[v or 0 for v in vals], orientation="h", marker_color=cols, text=txt,
                               textposition="outside"))
        fig.update_yaxes(autorange="reversed", side="left")
    else:
        fig = go.Figure(go.Bar(x=labels, y=[v or 0 for v in vals], marker_color=cols, text=txt,
                               textposition="outside"))
    fig = _layout(fig, height)
    fig.update_layout(hovermode="closest")
    return fig


def stacked_cost(labels, fees, spread, slip, height=240) -> go.Figure:
    fig = go.Figure()
    for name, vals, c in (("手续费", fees, BLUE), ("滑点成本", slip, GREEN), ("点差成本", spread, ORANGE)):
        fig.add_trace(go.Bar(y=labels, x=[_num(v) or 0 for v in vals], orientation="h", name=name, marker_color=c))
    fig.update_layout(barmode="stack")
    fig.update_yaxes(autorange="reversed", side="left")
    return _layout(fig, height, legend=True)
