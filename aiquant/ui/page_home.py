"""总览（默认首页）：一眼看清钱在哪里、赚了多少、每条策略在做什么、系统是否健康。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .. import db
from ..data import alpaca
from . import components as C

BJ = timezone(timedelta(hours=8))
START_EQUITY = 100_000.0


@st.cache_data(ttl=30, show_spinner=False, max_entries=4)
def _broker(nonce: int) -> dict:
    out = {"account": None, "positions": [], "clock": None}
    code, b = alpaca.paper_get("/v2/account")
    if code == 200 and isinstance(b, dict):
        out["account"] = {k: b.get(k) for k in ("equity", "last_equity", "cash", "buying_power", "status")}
    code, b = alpaca.paper_get("/v2/positions")
    if code == 200 and isinstance(b, list):
        out["positions"] = [{k: p.get(k) for k in ("symbol", "qty", "market_value", "unrealized_pl", "avg_entry_price",
                                                   "current_price", "asset_class")} for p in b]
    code, b = alpaca.paper_get("/v2/clock")
    if code == 200 and isinstance(b, dict):
        out["clock"] = {k: b.get(k) for k in ("is_open", "next_open", "next_close")}
    return out


def _f(x, d=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def _bj(ts: str | None, fmt: str = "%m-%d %H:%M") -> str:
    if not ts:
        return "--"
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t
    return t.tz_convert(BJ).strftime(fmt)


def _ago(ts: str | None) -> str:
    if not ts:
        return "--"
    t = pd.Timestamp(ts)
    t = t.tz_localize("UTC") if t.tzinfo is None else t
    s = (pd.Timestamp.now(tz="UTC") - t).total_seconds()
    return f"{int(s)} 秒前" if s < 120 else (f"{int(s // 60)} 分钟前" if s < 7200 else f"{int(s // 3600)} 小时前")


def _money(x: float, sign: bool = False) -> str:
    return (f"{x:+,.2f}" if sign else f"{x:,.2f}")


def render() -> None:
    st.markdown(HOME_CSS, unsafe_allow_html=True)
    live()


@st.fragment(run_every=15.0)
def live() -> None:
    b = _broker(int(datetime.now().timestamp() // 30))
    acct = b["account"] or {}
    eq = _f(acct.get("equity"))
    last_eq = _f(acct.get("last_equity"), eq)
    cash = _f(acct.get("cash"))
    pos = b["positions"]
    crypto_val = sum(_f(p["market_value"]) for p in pos if p.get("asset_class") == "crypto")
    stock_val = sum(_f(p["market_value"]) for p in pos if p.get("asset_class") != "crypto")
    total_pnl = eq - START_EQUITY if eq else 0.0
    day_pnl = eq - last_eq if eq else 0.0

    # ---- 1. 资产总览 ----
    tone = "pos" if total_pnl >= 0 else "neg"
    dtone = "pos" if day_pnl >= 0 else "neg"
    st.markdown(
        f'''<div class="hm-hero">
          <div class="hm-hero-main">
            <div class="hm-label">模拟账户总资产（USD）<span class="hm-chip">Alpaca Paper · 不涉及真实资金</span></div>
            <div class="hm-big">{_money(eq) if eq else "--"}</div>
            <div class="hm-sub"><span class="{tone}">累计 {_money(total_pnl, True)}（{total_pnl / START_EQUITY * 100:+.2f}%）</span>
              <span class="hm-dot">·</span><span class="{dtone}">今日 {_money(day_pnl, True)}</span>
              <span class="hm-dot">·</span>起始 100,000.00</div>
          </div>
          <div class="hm-hero-side">
            {_alloc_bar(eq, cash, crypto_val, stock_val)}
          </div>
        </div>''', unsafe_allow_html=True)

    # ---- 2. 三条策略线 + 进化 ----
    cards = st.columns(4, gap="small")
    with cards[0]:
        _card_btc(pos)
    with cards[1]:
        _card_etf(pos, b["clock"])
    with cards[2]:
        _card_shadow()
    with cards[3]:
        _card_evolve()

    # ---- 3. 资产曲线 + 最近动态 ----
    left, right = st.columns([3, 2], gap="small")
    with left:
        with st.container(border=True):
            C.section("资产曲线", right=C.pill("每小时记录一次", "gray"))
            _equity_chart()
    with right:
        with st.container(border=True):
            C.section("最近发生了什么", right=C.pill("北京时间", "gray"))
            _activity()

    # ---- 4. 系统健康 ----
    _health(b["clock"])


def _alloc_bar(eq: float, cash: float, crypto: float, stock: float) -> str:
    if eq <= 0:
        return '<div class="hm-label">资金分布：--</div>'
    other = max(0.0, cash)
    parts = [("BTC 持仓", crypto, "#F59E0B"), ("美股 ETF", stock, "#2563EB"), ("现金", other, "#CBD5E1")]
    tot = sum(p[1] for p in parts) or 1
    bars = "".join(f'<span style="width:{v / tot * 100:.2f}%;background:{c}" title="{n} {v:,.0f}"></span>'
                   for n, v, c in parts if v > 0)
    leg = "".join(f'<div class="hm-leg"><i style="background:{c}"></i>{n}<b>{v:,.0f}</b><em>{v / tot * 100:.0f}%</em></div>'
                  for n, v, c in parts)
    return f'<div class="hm-label">资金分布</div><div class="hm-bar">{bars}</div><div class="hm-legs">{leg}</div>'


def _card(title: str, tag: str, tag_tone: str, big: str, big_tone: str, lines: list[tuple[str, str]], foot: str) -> None:
    rows = "".join(f'<div class="hm-row"><span>{C.esc(k)}</span><b>{C.esc(v)}</b></div>' for k, v in lines)
    st.markdown(f'''<div class="hm-card">
        <div class="hm-card-h"><span>{C.esc(title)}</span>{C.pill(tag, tag_tone)}</div>
        <div class="hm-card-big {big_tone}">{C.esc(big)}</div>
        {rows}
        <div class="hm-card-f">{C.esc(foot)}</div></div>''', unsafe_allow_html=True)


def _card_btc(pos: list[dict]) -> None:
    from ..paper import portfolio as pf
    rows = pf.overview()
    running = sum(1 for r in rows if r["raw_status"] == "running")
    paused = sum(1 for r in rows if r["raw_status"] == "paused_entries")
    holding = [r for r in rows if r["qty"] > 0]
    btc = next((p for p in pos if p.get("asset_class") == "crypto"), None)
    upl = _f(btc["unrealized_pl"]) if btc else 0.0
    filled = sum(r["filled"] for r in rows)
    _card("BTC 趋势组合", f"{running} 个运行 · {paused} 个暂停", "green" if running else "gray",
          f"{upl:+,.2f}" if btc else "空仓", "pos" if upl > 0 else ("neg" if upl < 0 else "muted"),
          [("持仓策略", "、".join(r["label"].split("·")[0] for r in holding) or "无"),
           ("BTC 持仓", f"{_f(btc['qty']):.6f}" if btc else "0"),
           ("累计成交", f"{filled} 笔")],
          "日线/4 小时趋势跟随，只做多、不加杠杆")
    st.page_link(st.session_state["_pages"]["paper"], label="查看模拟盘交易 →")


def _card_etf(pos: list[dict], clock: dict | None) -> None:
    from ..paper import etf_trend as et
    cfg = et.get_config()
    stt = db.get_setting("etf_trend_state") or {}
    sig = stt.get("signal") or {}
    p = next((x for x in pos if x.get("symbol") == cfg["trade_symbol"]), None)
    upl = _f(p["unrealized_pl"]) if p else 0.0
    if sig:
        tag, tone = ("看多 · 持有", "green") if sig.get("regime") else ("看空 · 现金", "orange")
    else:
        tag, tone = "等待美股开盘", "gray"
    nxt = "--" if not clock else ("交易中" if clock.get("is_open") else _bj(clock.get("next_open")))
    _card(f"美股 ETF 择时 · {cfg['trade_symbol']}", tag, tone,
          f"{upl:+,.2f}" if p else "未持仓", "pos" if upl > 0 else ("neg" if upl < 0 else "muted"),
          [(f"{cfg['signal_symbol']} / {cfg['sma']}日线", f"{sig['close']:,.2f} / {sig['sma']:,.2f}" if sig else "--"),
           ("持仓市值", f"{_f(p['market_value']):,.0f}" if p else "0"),
           ("下次美股开盘", nxt)],
          f"站上均线持有 2 倍纳指，跌破转现金；占资金 {cfg['budget_pct']:g}%")
    st.page_link(st.session_state["_pages"]["paper"], label="查看订单与原因 →")


def _card_shadow() -> None:
    from ..paper import leverage_shadow as ls
    ls.ensure_tables()
    price = None
    try:
        from ..live.feed import FEED
        price = FEED.snapshot()["mid"]
    except Exception:  # noqa: BLE001
        pass
    sm = ls.summary(price)
    _card(f"BTC {sm['leverage']:g} 倍杠杆模拟", "持仓中" if sm["qty"] else "等待买入信号", "purple" if sm["qty"] else "gray",
          f"{sm['pnl_pct']:+.2f}%", "pos" if sm["pnl"] > 0 else ("neg" if sm["pnl"] < 0 else "muted"),
          [("保证金权益", f"{sm['equity']:,.2f}"),
           ("强平价", f"{sm['liq']:,.0f}" if sm["liq"] else "--"),
           ("手续费+资金费", f"{sm['fees'] + sm['funding']:,.2f}")],
          "只在本地记账，不下单；观察加杠杆的效果")
    st.page_link(st.session_state["_pages"]["paper"], label="查看杠杆账户 →")


def _card_evolve() -> None:
    from .. import evolve
    last = db.get_setting("evolve_last_run") or {}
    nxt = evolve.next_run_at()
    job = db.q1("SELECT status, stage FROM jobs WHERE kind='evolve' AND is_test=0 ORDER BY id DESC LIMIT 1")
    running = job and job["status"] in ("queued", "running")
    from .. import challenger
    nch = len(challenger.running())
    _card("策略自动进化", "进行中" if running else (f"{nch} 个挑战者运行中" if nch else "已启用"),
          "purple" if (running or nch) else "blue",
          f"{last.get('replaced', 0)} / {last.get('strategies', 0)}" if last else "--", "muted",
          [("上次进化", _bj(last.get("at"))),
           ("下次进化", "进行中" if running else (_bj(nxt.isoformat()) if nxt else "已关闭")),
           ("上次测试候选", f"{last.get('candidates', 0)} 个")],
          "大数字 = 上一轮替换数 / 参与数；挑战者在实时行情上验证后自动上线")
    st.page_link(st.session_state["_pages"]["evolve"], label="查看进化记录 →")


def _equity_chart() -> None:
    rows = db.q("SELECT ts, equity FROM account_snapshots ORDER BY ts") if _has_table("account_snapshots") else []
    if len(rows) < 2:
        st.markdown('<div class="aq-placeholder">资产曲线从今天开始记录（每小时一个点），<br>几个小时后这里会出现走势。</div>',
                    unsafe_allow_html=True)
        return
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(BJ)
    up = df["equity"].iloc[-1] >= START_EQUITY
    col = "#16A34A" if up else "#DC2626"
    fig = go.Figure(go.Scatter(x=df["ts"], y=df["equity"], mode="lines", line=dict(color=col, width=2),
                               fill="tozeroy", fillcolor="rgba(22,163,74,.08)" if up else "rgba(220,38,38,.08)",
                               hovertemplate="%{x|%m-%d %H:%M}<br>%{y:,.2f} USD<extra></extra>"))
    fig.add_hline(y=START_EQUITY, line=dict(color="#94A3B8", width=1, dash="dot"))
    lo, hi = df["equity"].min(), df["equity"].max()
    pad = max((hi - lo) * 0.2, 50)
    fig.update_layout(height=230, margin=dict(l=8, r=8, t=6, b=6), plot_bgcolor="#fff", paper_bgcolor="#fff",
                      yaxis=dict(range=[min(lo, START_EQUITY) - pad, max(hi, START_EQUITY) + pad], gridcolor="#EEF2F7",
                                 tickformat=",.0f"),
                      xaxis=dict(gridcolor="#F5F7FA"), showlegend=False)
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def _has_table(name: str) -> bool:
    return bool(db.q1("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)))


ICON = {"买入": ("🔵", "buy"), "卖出": ("🟠", "sell"), "自动进化": ("🧬", "evo"), "暂停": ("⏸", "warn"),
        "恢复": ("▶", "ok"), "资金额度": ("💰", "info"), "[ETF趋势]": ("📈", "info")}


def _activity() -> None:
    from ..paper import runner as pr
    ev = db.q("SELECT * FROM paper_events WHERE is_test=0 AND message NOT LIKE 'K线收盘检查%' "
              "AND message NOT LIKE '资金额度调整%' AND message NOT LIKE '加入策略组合%' "
              "AND message NOT LIKE '保存部署配置%' ORDER BY id DESC LIMIT 14")
    if not ev:
        st.caption("还没有动态。")
        return
    labels: dict = {}
    html = []
    for e in ev:
        icon, cls = "•", "info"
        for k, (ic, c) in ICON.items():
            if k in e["message"]:
                icon, cls = ic, c
                break
        if e["level"] in ("WARN", "ERROR"):
            cls = "warn" if e["level"] == "WARN" else "err"
        did = e["deployment_id"]
        if did not in labels:
            labels[did] = pr.dep_label(did) if did else "系统"
        msg = e["message"].replace("[ETF趋势] ", "")
        html.append(f'<div class="hm-ev {cls}"><span class="t">{_bj(e["ts"])}</span><span class="i">{icon}</span>'
                    f'<span class="m"><b>{C.esc(labels[did].split("（")[0])}</b> {C.esc(msg[:140])}</span></div>')
    st.markdown(f'<div class="hm-feed">{"".join(html)}</div>', unsafe_allow_html=True)


def _health(clock: dict | None) -> None:
    hb = db.get_setting("paper_loop_heartbeat")
    mk = (db.get_setting("market_last_update") or {}).get("at")
    hb_ok = hb and (pd.Timestamp.now(tz="UTC") - pd.Timestamp(hb)).total_seconds() < 180
    items = [
        ("自动交易循环", "正常" if hb_ok else "无响应", "green" if hb_ok else "red", f"心跳 {_ago(hb)}"),
        ("BTC 行情", "正常" if mk else "--", "green" if mk else "gray", f"更新 {_ago(mk)}"),
        ("美股市场", "开盘中" if clock and clock.get("is_open") else "休市", "blue" if clock and clock.get("is_open") else "gray",
         f"下次开盘 {_bj(clock.get('next_open'))}" if clock and not clock.get("is_open") else
         (f"收盘 {_bj(clock.get('next_close'))}" if clock else "--")),
        ("真实资金交易", "永久关闭", "gray", "LIVE_ORDERS = 0"),
    ]
    try:
        from .. import rhythm
        rows = rhythm.status_rows()
        cool = sum(1 for r in rows if r["blocked"])
        items.insert(3, ("交易节奏", "纪律执行中" if rhythm.current() else "已关闭", "purple" if rhythm.current() else "gray",
                         f"{cool} 个策略冷却中" if cool else "冷却 · 连亏刹车 · 波动率定仓"))
    except Exception:  # noqa: BLE001
        pass
    cells = "".join(f'<div class="hm-h"><span>{n}</span>{C.pill(s, t)}<em>{C.esc(d)}</em></div>' for n, s, t, d in items)
    st.markdown(f'<div class="hm-health">{cells}</div>', unsafe_allow_html=True)


HOME_CSS = """
<style>
.hm-hero{display:flex;gap:18px;align-items:stretch;flex-wrap:wrap;background:#fff;border:1px solid var(--aq-border);
  border-radius:12px;padding:16px 20px;margin-bottom:10px;box-shadow:0 1px 3px rgba(15,23,42,.05)}
.hm-hero-main{flex:1 1 320px;min-width:0}
.hm-hero-side{flex:1 1 360px;min-width:0;display:flex;flex-direction:column;justify-content:center;gap:6px}
.hm-label{font-size:12px;color:var(--aq-muted);display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.hm-chip{font-size:11px;background:var(--aq-gray-50);border-radius:999px;padding:1px 8px;color:var(--aq-muted)}
.hm-big{font-size:38px;font-weight:800;letter-spacing:-.5px;line-height:1.15;font-variant-numeric:tabular-nums;color:#0F172A}
.hm-sub{font-size:13px;color:var(--aq-muted);font-variant-numeric:tabular-nums}
.hm-sub .pos,.hm-sub .neg{font-weight:700}
.hm-dot{margin:0 6px;color:#CBD5E1}
.hm-bar{display:flex;height:12px;border-radius:999px;overflow:hidden;background:#EEF2F7}
.hm-bar span{display:block;height:100%}
.hm-legs{display:flex;gap:14px;flex-wrap:wrap}
.hm-leg{font-size:12px;color:var(--aq-muted);display:flex;align-items:center;gap:5px;font-variant-numeric:tabular-nums}
.hm-leg i{width:9px;height:9px;border-radius:3px;display:inline-block}
.hm-leg b{color:var(--aq-text)} .hm-leg em{font-style:normal;color:#94A3B8}
.hm-card{background:#fff;border:1px solid var(--aq-border);border-radius:12px;padding:12px 14px;min-height:196px;
  display:flex;flex-direction:column;gap:5px;box-shadow:0 1px 3px rgba(15,23,42,.05)}
.hm-card-h{display:flex;justify-content:space-between;align-items:center;gap:6px;font-weight:700;font-size:14px}
.hm-card-h span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hm-card-big{font-size:24px;font-weight:800;font-variant-numeric:tabular-nums;margin:2px 0 4px}
.hm-row{display:flex;justify-content:space-between;gap:8px;font-size:12px;color:var(--aq-muted)}
.hm-row b{color:var(--aq-text);font-weight:600;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
  font-variant-numeric:tabular-nums}
.hm-card-f{margin-top:auto;font-size:11px;color:#94A3B8;padding-top:6px;border-top:1px dashed var(--aq-border)}
[data-testid="stPageLink"] a{font-size:12px !important;padding:2px 4px !important}
.hm-feed{display:flex;flex-direction:column;gap:2px;max-height:262px;overflow-y:auto}
.hm-ev{display:grid;grid-template-columns:78px 20px 1fr;gap:4px;font-size:12px;padding:4px 6px;border-radius:6px;align-items:start}
.hm-ev:hover{background:#F8FAFC}
.hm-ev .t{color:#94A3B8;font-variant-numeric:tabular-nums}
.hm-ev .m{color:var(--aq-text);line-height:1.45}
.hm-ev.warn{background:var(--aq-orange-50)} .hm-ev.err{background:var(--aq-red-50)}
.hm-health{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px;margin-top:8px}
.hm-h{background:#fff;border:1px solid var(--aq-border);border-radius:10px;padding:8px 12px;display:flex;align-items:center;
  gap:8px;font-size:12px;flex-wrap:wrap}
.hm-h span:first-child{font-weight:600}
.hm-h em{font-style:normal;color:var(--aq-muted);margin-left:auto}
@media (max-width: 900px){ .hm-big{font-size:30px} }
</style>
"""
