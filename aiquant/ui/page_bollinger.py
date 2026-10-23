"""布林带分析：多周期状态卡、带宽 / %B 图、在模拟盘策略里的用法与研究结论。"""
from __future__ import annotations

import json

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from .. import bollinger as B
from .. import db
from ..data import market_data as md
from . import components as C

UP, DN = "#E5484D", "#12A150"     # 红涨绿跌
BAND = "#7C3AED"
TONE_PILL = {"red": "red", "green": "green", "orange": "orange", "gray": "gray"}
LABEL_COLOR = {"red": UP, "green": DN, "orange": "#EA8A0C", "gray": "#64748B"}


@st.cache_data(ttl=60, show_spinner=False)
def _df5() -> pd.DataFrame:
    m = md.load_master()
    if m.empty:
        return m
    return m[m["ts"] >= m["ts"].max() - pd.Timedelta(days=200)].reset_index(drop=True)


def _bj(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, utc=True).dt.tz_convert("Asia/Shanghai").dt.tz_localize(None)


def _card(s: dict) -> None:
    with st.container(border=True):
        col = LABEL_COLOR.get(s["tone"], "#64748B")
        st.markdown(f'<div class="aq-section"><span class="bar purple"></span>{C.esc(s["tf_cn"])}'
                    f'<span style="margin-left:auto">{C.pill(s["label"], TONE_PILL.get(s["tone"], "gray"))}</span></div>',
                    unsafe_allow_html=True)
        if not s["ok"]:
            st.caption(s["text"])
            return
        rk = s["rank"]
        st.markdown(
            f'<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin:2px 0 8px">'
            f'<div><div class="aq-sub">位置 %B</div><div style="font-size:20px;font-weight:800;color:{col}">'
            f'{s["pctb"]:.0f}</div></div>'
            f'<div><div class="aq-sub">带宽</div><div style="font-size:20px;font-weight:800">{s["width"]:.2f}%</div></div>'
            f'<div><div class="aq-sub">带宽分位</div><div style="font-size:20px;font-weight:800">'
            f'{"--" if rk is None else f"{rk:.0f}%"}</div></div></div>'
            f'<div class="aq-sub">上轨 {s["upper"]:,.0f} · 中轨 {s["mid"]:,.0f} · 下轨 {s["lower"]:,.0f}</div>'
            f'<div style="margin-top:6px">{C.esc(s["text"])}</div>', unsafe_allow_html=True)
        for name, tone, txt in s["signals"]:
            st.markdown(f'<div class="aq-ai-li">{C.pill(name, TONE_PILL.get(tone, "gray"))} '
                        f'<span class="aq-sub">{C.esc(txt)}</span></div>', unsafe_allow_html=True)


def _chart(b: pd.DataFrame, tf: int, n: int = 160) -> go.Figure:
    b = b.tail(n)
    x = _bj(b["open_time"])
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.62, 0.19, 0.19])
    fig.add_trace(go.Scatter(x=x, y=b["bb_up"], name="上轨", line=dict(color=BAND, width=1)), 1, 1)
    fig.add_trace(go.Scatter(x=x, y=b["bb_low"], name="下轨", line=dict(color=BAND, width=1), fill="tonexty",
                             fillcolor="rgba(124,58,237,0.07)"), 1, 1)
    fig.add_trace(go.Scatter(x=x, y=b["bb_mid"], name="中轨（20均线）", line=dict(color="#F59E0B", width=1.4, dash="dot")), 1, 1)
    fig.add_trace(go.Candlestick(x=x, open=b["open"], high=b["high"], low=b["low"], close=b["close"], name="K线",
                                 increasing_line_color=UP, increasing_fillcolor=UP, decreasing_line_color=DN,
                                 decreasing_fillcolor=DN, line=dict(width=1)), 1, 1)
    fig.add_trace(go.Scatter(x=x, y=b["bb_width"], name="带宽 %", line=dict(color="#0EA5E9", width=1.3)), 2, 1)
    sq = b[b["bb_rank"] <= 15]
    if len(sq):
        fig.add_trace(go.Scatter(x=_bj(sq["open_time"]), y=sq["bb_width"], mode="markers", name="收口（带宽处于近期最窄 15%）",
                                 marker=dict(color="#EA8A0C", size=6)), 2, 1)
    fig.add_trace(go.Scatter(x=x, y=b["bb_pctb"], name="%B", line=dict(color=BAND, width=1.3)), 3, 1)
    for lvl, dash in ((0, "dash"), (50, "dot"), (100, "dash")):
        fig.add_hline(y=lvl, line=dict(color="#CBD5E1", width=1, dash=dash), row=3, col=1)
    fig.update_layout(height=640, margin=dict(l=8, r=8, t=8, b=8), paper_bgcolor="white", plot_bgcolor="white",
                      xaxis_rangeslider_visible=False, hovermode="x unified", uirevision=f"bb{tf}",
                      legend=dict(orientation="h", y=1.05, x=0, font=dict(size=11)), font=dict(size=11, color="#475569"))
    fig.update_xaxes(showgrid=False, linecolor="#E5E7EB")
    fig.update_yaxes(gridcolor="#F1F5F9", side="right")
    fig.update_yaxes(title_text="价格", row=1, col=1)
    fig.update_yaxes(title_text="带宽%", row=2, col=1)
    fig.update_yaxes(title_text="%B", row=3, col=1)
    return fig


def _strategy_usage(daily: dict) -> None:
    from ..paper import runner as pr
    from ..strategy import registry
    rows = []
    for d in db.q("SELECT id FROM paper_deployments WHERE broker='alpaca_paper' AND is_test=0 "
                  "AND status IN ('running','paused_entries') ORDER BY id"):
        dep = pr.get_deployment(d["id"])
        v = registry.get_version(dep["version_id"])
        if not v:
            continue
        sch = v["schema"]
        bbi = [i for i in sch.get("indicators", []) if str(i["type"]).startswith("BB_")]
        pend = (dep["config"].get("pending_switch") or {})
        if not bbi and not pend:
            continue
        use = _usage(sch, bbi)
        rows.append({"部署": f"#{d['id']}", "策略": dep["config"].get("label") or registry.label(v),
                     "版本": registry.label(v), "布林带用法": use,
                     "状态": "等待空仓后切换到新版本" if pend else ("运行中" if dep["status"] == "running" else "暂停开仓")})
    with st.container(border=True):
        C.section("模拟盘里的布林带", purple=True)
        if daily.get("ok"):
            gap = (daily["close"] / daily["mid"] - 1) * 100
            st.markdown(f'日线中轨 <b>{daily["mid"]:,.0f}</b>，最新日线收盘 <b>{daily["close"]:,.0f}</b>'
                        f'（{"高于" if gap >= 0 else "低于"}中轨 {abs(gap):.1f}%）。日线趋势策略持仓时，'
                        f'只要日线收盘跌破中轨就离场。', unsafe_allow_html=True)
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        else:
            ch = db.q("SELECT id, deployment_id, status, change FROM evolve_challengers WHERE change LIKE '%布林%' "
                      "ORDER BY id DESC LIMIT 5") if _has_table("evolve_challengers") else []
            if ch:
                st.dataframe(pd.DataFrame([{"挑战者": f"#{c['id']}", "部署": f"#{c['deployment_id']}",
                                            "状态": c["status"], "改动": c["change"]} for c in ch]),
                             hide_index=True, use_container_width=True)
            else:
                st.caption("布林带升级会在服务启动后自动登记为挑战者，通过系统检验后在空仓时切换。")


def _has_table(name: str) -> bool:
    return bool(db.q1("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def _usage(sch: dict, bbi: list[dict]) -> str:
    """从入场 / 离场条件里读出布林带的实际用法。"""
    from ..strategy.schema import OP_CN, INDICATOR_CN
    ids = {i["id"]: i for i in bbi}
    parts = []
    for grp, word in (("entry", "入场"), ("exit", "离场")):
        for c in (sch.get(grp) or {}).get("conditions", []):
            refs = [c["left"].get("ref"), (c.get("right") or {}).get("ref")]
            hit = [r for r in refs if r in ids]
            if not hit:
                continue
            l = c["left"].get("ref")
            r = (c.get("right") or {}).get("ref") or (c.get("right") or {}).get("value")
            name = lambda x: INDICATOR_CN.get(ids[x]["type"], x) if x in ids else {"close": "收盘价"}.get(x, str(x))
            parts.append(f"{word}：{name(l)}{OP_CN.get(c['op'], c['op'])}{name(r)}")
    return "；".join(parts) or "、".join(sorted({INDICATOR_CN.get(i["type"], i["type"]) for i in bbi})) or "--"


RESEARCH = [
    ("日线趋势 + 跌破中轨离场", "+8.0% / 9.6%", "5/6", "+6.4% / 10.6%", "5/6", "✅ 采用（策略 #1、#2）"),
    ("日线趋势（原版，对照）", "+6.3% / 11.0%", "5/6", "+4.4% / 12.6%", "5/6", "—"),
    ("日线趋势 + %B>50 才入场", "+6.9% / 10.4%", "5/6", "+4.4% / 12.6%", "5/6", "小幅改善，未采用"),
    ("4小时 上轨突破 + 日线趋势过滤", "+11.5% / 18.3%", "5/6", "+0.6% / 20.4%", "2/6", "❌ ETH 上不成立"),
    ("4小时 收口后突破", "−1.3% / 19.3%", "4/6", "−5.9% / 23.9%", "2/6", "❌"),
    ("日线 收口后突破", "−5.0% / 19.1%", "2/6", "−5.5% / 17.0%", "1/6", "❌"),
    ("趋势中回踩下轨买入（4小时）", "−9.3% / 15.2%", "1/6", "−3.0% / 12.5%", "2/6", "❌"),
]


def render() -> None:
    C.section("布林带分析", right=C.pill("多周期 · 只看已收盘K线", "purple"))
    st.markdown('<div class="aq-sub">布林带 = 中轨（20 根 K 线均线）± 2 倍标准差。<b>%B</b> 表示价格在带里的位置（0 = 下轨，'
                '100 = 上轨）；<b>带宽</b>越窄说明波动越小，“收口”之后常有一段较大的行情；价格连续贴着上轨或下轨走叫'
                '“沿轨运行”，是趋势强的表现，而不是反转信号。</div>', unsafe_allow_html=True)
    df5 = _df5()
    if df5.empty:
        st.info("还没有行情数据，等后台更新行情后再看。")
        return
    states = B.multi_tf(df5)
    cols = st.columns(3)
    for col, s in zip(cols, states):
        with col:
            _card(s)
    last = pd.Timestamp(df5["ts"].max()) + pd.Timedelta(hours=8, minutes=5)
    st.caption(f"行情截至 {last:%m-%d %H:%M} 北京时间（每分钟刷新）。仅为指标的客观描述，不是投资建议。")

    with st.container(border=True):
        tfc = st.segmented_control("周期", ["1小时", "4小时", "日线"], default="4小时", key="bb_tf")
        tf = {"1小时": 60, "4小时": 240, "日线": 1440}.get(tfc or "4小时", 240)
        s = next((x for x in states if x["tf"] == tf), None)
        if s and s.get("ok"):
            st.plotly_chart(_chart(s["frame"], tf), use_container_width=True, key=f"bb_chart_{tf}",
                            config={"displayModeBar": False, "scrollZoom": True})
        else:
            st.caption("数据不足，无法绘图。")

    _strategy_usage(next((x for x in states if x["tf"] == 1440), {}))

    with st.container(border=True):
        C.section("布林带策略的历史检验", right=C.pill("2021-03 → 2026-10", "gray"))
        st.markdown('<div class="aq-sub">每种用法在 BTC 和 ETH 上各回测 6 个年度，扣除 0.25% 单边手续费，并使用模拟盘同样的'
                    '交易节奏规则。数字为“平均年收益 / 平均年度最大回撤”。只有两个币上都稳定改善的用法才会上模拟盘。</div>',
                    unsafe_allow_html=True)
        st.dataframe(pd.DataFrame(RESEARCH, columns=["用法", "BTC 年均 / 回撤", "BTC 盈利年份", "ETH 年均 / 回撤",
                                                     "ETH 盈利年份", "结论"]),
                     hide_index=True, use_container_width=True)
        st.caption("结论：布林带单独做买卖信号（收口突破、碰下轨抄底）在加密货币上长期不赚钱；"
                   "把“收盘跌破中轨”作为日线趋势策略的离场条件，在两个币上都更稳。")
