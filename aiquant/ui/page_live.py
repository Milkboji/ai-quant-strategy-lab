"""实时看盘（新手版）：秒级/分钟级K线、买卖点、指标大白话解读、虚拟交易账户、快速回放测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from ..live import analysis, sim
from ..live.feed import FEED, TIMEFRAMES
from . import components as C
from .common import flash, show_flash

BJ = "Asia/Shanghai"
UP_RED = ("#E5484D", "#12A150")     # 红涨绿跌
UP_GREEN = ("#12A150", "#E5484D")   # 绿涨红跌
BUY_C, SELL_C = "#2563EB", "#F59E0B"
FEE_OPTS = {25.0: "0.25%（Alpaca 实际费率）", 10.0: "0.10%（低费率交易所）", 0.0: "0（只看信号，不计手续费）"}


def _bj(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, utc=True).dt.tz_convert(BJ).dt.tz_localize(None)


def _fmt_tf(sec: int) -> str:
    return next(k for k, v in TIMEFRAMES.items() if v == sec)


def chart(c: pd.DataFrame, trades: pd.DataFrame, tf_sec: int, adv: bool, colors, view_last: int | None = None) -> go.Figure:
    a = analysis.add_indicators(c) if len(c) else c
    rows = 4 if adv else 2
    heights = [0.58, 0.14, 0.14, 0.14] if adv else [0.8, 0.2]
    fig = make_subplots(rows=rows, cols=1, shared_xaxes=True, vertical_spacing=0.02, row_heights=heights)
    x = _bj(a["ts"]) if len(a) else []
    up, dn = colors
    if len(a):
        fig.add_trace(go.Candlestick(x=x, open=a["open"], high=a["high"], low=a["low"], close=a["close"], name="K线",
                                     increasing_line_color=up, increasing_fillcolor=up, decreasing_line_color=dn,
                                     decreasing_fillcolor=dn, line=dict(width=1)), 1, 1)
        fig.add_trace(go.Scatter(x=x, y=a["ema9"], name="短期均线 EMA9", line=dict(color="#F59E0B", width=1.3)), 1, 1)
        fig.add_trace(go.Scatter(x=x, y=a["ema21"], name="长期均线 EMA21", line=dict(color="#7C3AED", width=1.3)), 1, 1)
        if adv:
            fig.add_trace(go.Scatter(x=x, y=a["bb_up"], name="布林上轨", line=dict(color="#94A3B8", width=0.8, dash="dot")), 1, 1)
            fig.add_trace(go.Scatter(x=x, y=a["bb_low"], name="布林下轨", line=dict(color="#94A3B8", width=0.8, dash="dot")), 1, 1)
        vol_c = [up if cl >= op else dn for op, cl in zip(a["open"], a["close"])]
        fig.add_trace(go.Bar(x=x, y=a["volume"], name="成交量", marker_color=vol_c, opacity=0.5, showlegend=False), 2, 1)
        if adv:
            fig.add_trace(go.Scatter(x=x, y=a["rsi"], name="RSI", line=dict(color="#0EA5E9", width=1.2)), 3, 1)
            for lvl in (30, 70):
                fig.add_hline(y=lvl, line=dict(color="#CBD5E1", width=1, dash="dash"), row=3, col=1)
            fig.add_trace(go.Bar(x=x, y=a["macd_hist"], name="MACD 柱",
                                 marker_color=[up if v >= 0 else dn for v in a["macd_hist"].fillna(0)]), 4, 1)
    if trades is not None and len(trades) and len(a):
        t = trades.copy()
        t["x"] = _bj(t["ts"])
        t = t[t["x"] >= x.min()]
        lo, hi = float(a["low"].min()), float(a["high"].max())
        pad = (hi - lo) * 0.03 or 1
        b = t[t["side"] == "buy"]
        s = t[t["side"] == "sell"]
        fig.add_trace(go.Scatter(x=b["x"], y=b["price"] - pad, mode="markers+text", name="买入点", text=["买"] * len(b),
                                 textposition="bottom center", textfont=dict(color=BUY_C, size=13),
                                 marker=dict(symbol="triangle-up", size=14, color=BUY_C, line=dict(color="white", width=1)),
                                 customdata=b[["price", "reason"]],
                                 hovertemplate="买入 %{customdata[0]:,.2f}<br>%{customdata[1]}<extra></extra>"), 1, 1)
        fig.add_trace(go.Scatter(x=s["x"], y=s["price"] + pad, mode="markers+text", name="卖出点", text=["卖"] * len(s),
                                 textposition="top center", textfont=dict(color=SELL_C, size=13),
                                 marker=dict(symbol="triangle-down", size=14, color=SELL_C, line=dict(color="white", width=1)),
                                 customdata=s[["price", "reason"]],
                                 hovertemplate="卖出 %{customdata[0]:,.2f}<br>%{customdata[1]}<extra></extra>"), 1, 1)
    fig.update_layout(height=620 if adv else 500, margin=dict(l=8, r=8, t=8, b=8), paper_bgcolor="white",
                      plot_bgcolor="white", xaxis_rangeslider_visible=False, uirevision=f"tf{tf_sec}{adv}",
                      legend=dict(orientation="h", y=1.04, x=0, font=dict(size=11)), hovermode="x unified",
                      font=dict(size=11, color="#475569"))
    fig.update_xaxes(showgrid=False, linecolor="#E5E7EB")
    if view_last and len(a) > view_last:
        fig.update_xaxes(range=[x.iloc[-view_last], x.iloc[-1] + pd.Timedelta(seconds=tf_sec)])
        win = a.tail(view_last)
        lo, hi = float(win["low"].min()), float(win["high"].max())
        pad = (hi - lo) * 0.08 or 1
        fig.update_yaxes(range=[lo - pad, hi + pad], row=1, col=1)
    fig.update_yaxes(gridcolor="#F1F5F9", side="right")
    fig.update_yaxes(title_text="价格", row=1, col=1)
    fig.update_yaxes(title_text="量", row=2, col=1)
    if adv:
        fig.update_yaxes(title_text="RSI", range=[0, 100], row=3, col=1)
        fig.update_yaxes(title_text="MACD", row=4, col=1)
    return fig


def _session_trades() -> tuple[dict | None, pd.DataFrame]:
    a = sim.ENGINE.active()
    if a is None:
        a = sim.db.q1("SELECT * FROM sim_sessions WHERE is_replay=0 ORDER BY id DESC LIMIT 1")
    return a, (sim.trades_of(a["id"]) if a else pd.DataFrame())


def ticker() -> None:
    snap = FEED.snapshot()
    stt = snap["status"]
    px = snap["mid"]
    now_bj = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    chg = snap["chg24"]
    col = "#E5484D" if (chg or 0) >= 0 else "#12A150"
    src = "模拟行情（非真实，仅测试）" if stt.get("source") == "synthetic" else "Alpaca 实时行情"
    head = (f'<span style="font-size:30px;font-weight:800;color:{col}">{px:,.2f}</span>'
            f'<span style="color:{col};font-weight:700">{"" if chg is None else f"{chg:+.2f}%（24小时）"}</span>'
            if px else '<span style="font-size:20px">等待行情数据…</span>')
    ba = (f'买一 {snap["bid"]:,.2f} ／ 卖一 {snap["ask"]:,.2f}' if snap["bid"] and snap["ask"] else "")
    st.markdown(
        f'<div class="aq-banner" style="gap:8px 26px"><span style="font-size:14px;font-weight:700">BTC/USD 比特币</span>'
        f'{head}<span class="aq-sub">{ba}</span>'
        f'<span>{C.pill(stt.get("msg", ""), "green" if stt.get("ok") else "red")} '
        f'<span class="aq-sub">{src} · 北京时间 {now_bj:%H:%M:%S}</span></span></div>', unsafe_allow_html=True)


def analysis_card(c: pd.DataFrame, tf_sec: int) -> None:
    done = c[c["complete"]] if len(c) else c
    r = analysis.analyze(done)
    bar = int((r["score"] + 100) / 2)
    color = {"green": "#E5484D", "red": "#12A150", "gray": "#94A3B8"}[r["tone"]]
    with st.container(border=True):
        st.markdown(f'<div class="aq-section"><span class="bar"></span>行情体检（{_fmt_tf(tf_sec)}）</div>',
                    unsafe_allow_html=True)
        st.markdown(
            f'<div style="display:flex;align-items:center;gap:10px"><span style="font-size:20px;font-weight:800;'
            f'color:{color}">{C.esc(r["verdict"])}</span><span class="aq-sub">综合分 {r["score"]:+d}</span></div>'
            f'<div style="height:8px;background:linear-gradient(90deg,#12A150,#E2E8F0 50%,#E5484D);border-radius:4px;'
            f'position:relative;margin:6px 0 8px"><div style="position:absolute;left:{bar}%;top:-4px;width:4px;height:16px;'
            f'background:#0F172A;border-radius:2px"></div></div>'
            f'<div class="aq-sub" style="margin-bottom:6px">{C.esc(r["summary"])}</div>', unsafe_allow_html=True)
        tone_map = {"green": "red", "red": "green", "orange": "orange", "gray": "gray"}  # 指标灯颜色跟随红涨绿跌
        for it in r["items"]:
            lab = {"green": "偏多", "red": "偏空", "orange": "注意", "gray": "中性"}[it["tone"]]
            st.markdown(f'<div class="aq-ai-li">{C.pill(lab, tone_map[it["tone"]])} <b>{C.esc(it["name"])}</b> '
                        f'<span class="aq-tag">{C.esc(it["value"])}</span><br><span class="aq-sub">{C.esc(it["explain"])}</span>'
                        f'</div>', unsafe_allow_html=True)
        st.caption("仅为指标的客观解读，不是投资建议。")


def account_card() -> None:
    a, tr = _session_trades()
    snap = FEED.snapshot()
    with st.container(border=True):
        st.markdown('<div class="aq-section"><span class="bar purple"></span>我的虚拟账户 '
                    '<span class="aq-sub">本地纸上记账，不会下任何真实或 Paper 订单</span></div>', unsafe_allow_html=True)
        if a is None:
            st.caption("还没有开始虚拟交易。在下方选择一个策略，点击『开始虚拟交易』。")
            return
        bid = snap["bid"] or 0
        eq = a["cash"] + a["qty"] * bid
        ret = (eq / a["cash0"] - 1) * 100
        running = a["status"] == "running"
        cc = st.columns(4)
        dc = "inverse" if st.session_state.get("lv_color", "红涨绿跌") == "红涨绿跌" else "normal"
        cc[0].metric("总资产（USD）", f"{eq:,.2f}", f"{ret:+.2f}%", delta_color=dc)
        cc[1].metric("现金", f"{a['cash']:,.2f}")
        cc[2].metric("持有 BTC", f"{a['qty']:.6f}")
        if a["qty"] > 0 and a["entry_price"]:
            fl = (bid / a["entry_price"] - 1) * 100
            cc[3].metric("这笔浮动盈亏", f"{fl:+.2f}%", f"买入价 {a['entry_price']:,.2f}", delta_color="off")
        else:
            cc[3].metric("当前状态", "空仓等待" if running else "已停止")
        p = sim.PRESETS[a["preset"]]
        st.markdown(f'{C.pill("运行中" if running else "已停止", "green" if running else "gray")} '
                    f'<b>{C.esc(p["label"])}</b> · 看 {_fmt_tf(a["tf_sec"])} K线 · 手续费 {a["fee_bps"] / 100:.2f}% · '
                    f'起始资金 {a["cash0"]:,.0f} USD<br><span class="aq-sub">{C.esc(p["plain"])}</span>',
                    unsafe_allow_html=True)
        if len(tr):
            t = tr.sort_values("id", ascending=False).head(12)
            lines = []
            for _, r in t.iterrows():
                tm = pd.Timestamp(r["ts"]).tz_convert(BJ).strftime("%m-%d %H:%M:%S")
                side = "🔵 买入" if r["side"] == "buy" else "🟠 卖出"
                pnl = "" if pd.isna(r["pnl"]) else f"，这笔{'赚' if r['pnl'] >= 0 else '亏'} {abs(r['pnl']):,.2f} USD"
                lines.append(f"{tm} {side} {r['qty']:.5f} BTC @ {r['price']:,.2f}（手续费 {r['fee']:.2f}{pnl}）"
                             f" — {r['reason']}")
            st.markdown("**交易记录（最新在上）**\n\n" + "\n".join(f"- {x}" for x in lines))
            sells = tr[tr["side"] == "sell"]
            if len(sells):
                w = (sells["pnl"] > 0).mean() * 100
                st.caption(f"已完成 {len(sells)} 笔，盈利笔数占比 {w:.0f}%，累计手续费 {tr['fee'].sum():,.2f} USD")
        else:
            st.caption("还没有成交。策略只在条件满足的那根K线收盘时才会买卖，耐心等待即可。")


def cost_warning(tf_sec: int, fee_bps: float) -> None:
    """用大白话对比：这个周期一根K线平均能波动多少 vs 一买一卖要付多少成本。"""
    c = FEED.candles(tf_sec, 120)
    c = c[c["complete"]] if len(c) else c
    if len(c) < 20:
        return
    move = float(((c["high"] - c["low"]) / c["close"]).median() * 100)
    cost = fee_bps * 2 / 100 + 0.02
    if move < cost:
        st.markdown(f'<div class="aq-warn">⚠ 成本提醒：{_fmt_tf(tf_sec)}K线平均每根只波动约 <b>{move:.3f}%</b>，'
                    f'而一买一卖的手续费+滑点约 <b>{cost:.2f}%</b>。价格要连续走好几根K线才够付手续费，'
                    f'这个周期频繁交易大概率亏钱。建议换更长的周期（1小时、4小时），或者先用『只看信号』模式观察。</div>',
                    unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="aq-ok">成本检查：{_fmt_tf(tf_sec)}K线平均波动约 {move:.2f}%，'
                    f'一买一卖成本约 {cost:.2f}%，波动能覆盖成本。</div>', unsafe_allow_html=True)


def controls() -> None:
    a = sim.ENGINE.active()
    with st.container(border=True):
        st.markdown('<div class="aq-section"><span class="bar"></span>开始 / 停止虚拟交易</div>', unsafe_allow_html=True)
        c = st.columns([1.4, 1, 1, 1])
        preset = c[0].selectbox("选择策略", list(sim.PRESETS), format_func=lambda k: sim.PRESETS[k]["label"],
                                key="lv_preset")
        tfs = [v for v in TIMEFRAMES.values()]
        tf = c[1].selectbox("看哪个周期的K线", tfs, index=tfs.index(900), format_func=_fmt_tf, key="lv_tf_sim",
                            help="周期越短，交易越频繁，手续费越多")
        cash = c[2].number_input("起始资金 USD", 100.0, 1_000_000.0, 10_000.0, 1000.0, key="lv_cash")
        fee = c[3].selectbox("手续费", list(FEE_OPTS), format_func=FEE_OPTS.get, key="lv_fee")
        st.caption(sim.PRESETS[preset]["plain"])
        cost_warning(int(tf), float(fee))
        b = st.columns(3)
        if b[0].button("▶ 开始虚拟交易", type="primary", key="lv_start", use_container_width=True):
            sim.ENGINE.start(preset, int(tf), float(cash), float(fee), 1.0, FEED.source)
            flash("live", "ok", f"已开始：{sim.PRESETS[preset]['label']}，看 {_fmt_tf(int(tf))} K线（之前的虚拟会话已结束）")
            st.rerun()
        if b[1].button("停止（保留持仓）", key="lv_stop", use_container_width=True, disabled=a is None):
            sim.ENGINE.stop(False)
            st.rerun()
        if b[2].button("停止并全部卖出", key="warn_lv_stopsell", use_container_width=True, disabled=a is None):
            sim.ENGINE.stop(True, FEED)
            st.rerun()
        show_flash("live")


def replay_card() -> None:
    with st.container(border=True):
        st.markdown('<div class="aq-section"><span class="bar purple"></span>快速回放测试 '
                    '<span class="aq-sub">用最近的真实K线把策略"快进"跑一遍，几秒出结果，不用等几天</span></div>',
                    unsafe_allow_html=True)
        c = st.columns([1.4, 1, 1, 1, 1])
        preset = c[0].selectbox("策略", list(sim.PRESETS), format_func=lambda k: sim.PRESETS[k]["label"], key="rp_preset")
        tfs = [60, 180, 300, 900, 1800, 3600]
        tf = c[1].selectbox("周期", tfs, index=tfs.index(900), format_func=_fmt_tf, key="rp_tf")
        span = c[2].selectbox("回放多长时间", [1, 3, 7, 10], index=3, format_func=lambda d: f"最近 {d} 天", key="rp_span")
        fee = c[3].selectbox("手续费", list(FEE_OPTS), format_func=FEE_OPTS.get, key="rp_fee")
        go_ = c[4].button("▶ 开始回放", type="primary", key="rp_go", use_container_width=True)
        if go_:
            n = int(span * 86400 / tf)
            cd = FEED.candles(tf, n + 5)
            try:
                res = sim.replay(preset, tf, cd, 10_000.0, float(fee), 1.0, source=FEED.source)
                st.session_state["rp_res"] = res
            except Exception as e:  # noqa: BLE001
                flash("rp", "err", str(e))
        show_flash("rp")
        res = st.session_state.get("rp_res")
        if res:
            win = "--" if res["win_rate_pct"] is None else f"{res['win_rate_pct']:.0f}%"
            m = st.columns(5)
            m[0].metric("策略收益", f"{res['return_pct']:+.2f}%")
            m[1].metric("同期一直持有", f"{res['buy_hold_pct']:+.2f}%")
            m[2].metric("最大回撤", f"{res['max_drawdown_pct']:.2f}%")
            m[3].metric("交易次数 / 胜率", f"{res['trades']} / {win}")
            m[4].metric("手续费合计", f"{res['fees']:,.2f}")
            a = sim.db.q1("SELECT * FROM sim_sessions WHERE id=?", (res["session_id"],))
            tr = sim.trades_of(res["session_id"])
            cd = FEED.candles(a["tf_sec"], res["bars"] + 5)
            cd = cd[cd["ts"] >= pd.Timestamp(res["start"])]
            colors = UP_RED if st.session_state.get("lv_color", "红涨绿跌") == "红涨绿跌" else UP_GREEN
            st.plotly_chart(chart(cd, tr, a["tf_sec"], False, colors, view_last=240), use_container_width=True,
                            key="rp_chart", config={"scrollZoom": True})
            st.caption("图中默认显示最后 240 根K线，可用鼠标滚轮缩放、拖动查看更早的买卖点。")
            verdict = ("跑赢了一直持有" if res["return_pct"] > res["buy_hold_pct"] else "没有跑赢一直持有")
            st.caption(f"回放区间（北京时间）{pd.Timestamp(res['start']).tz_convert(BJ):%m-%d %H:%M} → "
                       f"{pd.Timestamp(res['end']).tz_convert(BJ):%m-%d %H:%M}，共 {res['bars']} 根K线；"
                       f"这次回放{verdict}。信号在K线收盘后、按下一根K线开盘价成交；同一根K线内同时碰到止损和止盈时按先止损计算。"
                       "历史回放不代表未来收益。")


def glossary() -> None:
    with st.expander("📘 看不懂？这里有名词解释"):
        st.markdown("""
- **K线（蜡烛图）**：一根K线代表一段时间（比如 1 分钟）的价格。实体上下两端是开盘价和收盘价，细线是最高价和最低价。默认**红色代表涨、绿色代表跌**（国内习惯），可在右上角切换。
- **周期**：『1分钟』表示每根K线是 1 分钟；『1秒』最细，适合看即时波动；『1小时』更适合看大方向。
- **均线（EMA）**：最近一段时间的平均价格。橙色是短期均线，紫色是长期均线。短期从下往上穿过长期叫**金叉**，反过来叫**死叉**。
- **RSI**：0~100，衡量涨得是否太猛。高于 70 算『过热』，低于 30 算『超卖』。
- **MACD 柱**：在 0 以上表示上涨动能，在 0 以下表示下跌动能。
- **布林带**：价格通常在上下轨之间波动，冲出轨道说明走势很急。
- **🔵 买 / 🟠 卖**：图上的三角形是虚拟账户真实发生的买入和卖出位置，鼠标放上去能看到原因。
- **止损 / 止盈**：亏到设定比例自动卖出叫止损，赚到设定比例自动卖出叫止盈。
- **手续费**：每买或卖一次都要付。交易越频繁，手续费吃掉的越多，这是短线策略最常见的亏损原因。
""")


def render() -> None:
    FEED.start()
    sim.ensure_tables()
    st.markdown('<div class="aq-section" style="font-size:20px"><span class="bar"></span>实时看盘 · 新手版 '
                '<span class="aq-sub">即时K线 · 买卖点 · 指标大白话 · 虚拟交易（不花真钱）</span></div>',
                unsafe_allow_html=True)
    @st.fragment(run_every=5.0)
    def paper_banner():
        try:
            from ..paper import portfolio as pf
            rows = pf.overview()
        except Exception:  # noqa: BLE001
            return
        run = [r for r in rows if r["raw_status"] in ("running", "paused_entries")]
        if not rows:
            return
        n_f = sum(r["filled"] for r in rows)
        hold = [r["label"] for r in rows if r["qty"] > 1e-9]
        last = max((r["last_at"] or "" for r in rows), default="")
        st.markdown(f'<div class="aq-banner">🤖 <b>模拟盘自动交易</b>：{len(run)} 个策略运行中 · 已成交 {n_f} 笔 · '
                    f'当前持仓策略：{C.esc("、".join(hold)) if hold else "无（全部空仓等待信号）"} · '
                    f'最近一次自动判断 {last[11:19]} UTC <span class="aq-sub">详情见顶部『模拟盘交易』</span></div>',
                    unsafe_allow_html=True)

    paper_banner()
    top = st.columns([4.2, 1.1, 1.1])
    tf = top[0].segmented_control("K线周期", list(TIMEFRAMES), default="1分钟", key="lv_tf",
                                  help="秒级K线由买卖报价实时聚合；分钟以上由成交数据聚合")
    adv = top[1].toggle("显示 RSI / MACD", value=False, key="lv_adv")
    top[2].radio("涨跌颜色", ["红涨绿跌", "绿涨红跌"], horizontal=True, key="lv_color", label_visibility="visible")
    tf_sec = TIMEFRAMES.get(tf or "1分钟", 60)
    colors = UP_RED if st.session_state.get("lv_color", "红涨绿跌") == "红涨绿跌" else UP_GREEN

    @st.fragment(run_every=1.0)
    def live_panel():
        ticker()
        left, right = st.columns([2.6, 1], gap="medium")
        c = FEED.candles(tf_sec, 240 if tf_sec >= 60 else 300)
        a, tr = _session_trades()
        with left:
            with st.container(border=True):
                n_done = int(c["complete"].sum()) if len(c) else 0
                st.markdown(f'<div class="aq-section"><span class="bar"></span>BTC/USD · {tf or "1分钟"} K线 '
                            f'<span class="aq-sub">每秒刷新 · 最后一根为进行中 · 已完成 {n_done} 根 · 时间为北京时间</span></div>',
                            unsafe_allow_html=True)
                if len(c) == 0:
                    st.info("正在加载行情…（首次打开需要几秒钟回补历史数据）")
                else:
                    st.plotly_chart(chart(c, tr, tf_sec, adv, colors), use_container_width=True, key="lv_chart",
                                    config={"displayModeBar": False, "scrollZoom": True})
                if a and a["tf_sec"] != tf_sec and len(tr):
                    st.caption(f"提示：虚拟账户按 {_fmt_tf(a['tf_sec'])} K线交易，切换到该周期看买卖点最清楚。")
        with right:
            analysis_card(c, tf_sec)

    live_panel()

    @st.fragment(run_every=2.0)
    def acct():
        account_card()

    l2, r2 = st.columns([1.6, 1], gap="medium")
    with l2:
        acct()
    with r2:
        controls()
    replay_card()
    glossary()
