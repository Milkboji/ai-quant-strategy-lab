"""模拟盘交易中心：选择冻结策略 → 部署配置 → 风险确认 → 启动（Alpaca Paper 订单权限锁定）。"""
from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import streamlit as st

from .. import config, db, research
from ..data import http
from ..data import market_data as md
from ..paper import runner as pr
from ..paper.brokers import AlpacaPaperAdapter, MockBroker, OrderPermissionLocked
from ..strategy import registry
from . import components as C
from .common import flash, show_flash

TF_LABEL = {v: k for k, v in config.TIMEFRAMES.items()}


def _version_opts() -> list[dict]:
    out = []
    for slot in registry.all_slots():
        if slot["current_version_id"]:
            out.append(registry.get_version(slot["current_version_id"]))
    seen = {v["id"] for v in out}
    for v in registry.all_versions():
        if v["id"] not in seen:
            out.append(v)
    return out


def render() -> None:
    st.markdown('<div class="aq-section"><span class="bar"></span>模拟盘交易中心 '
                f'{C.pill("Paper Trading", "orange")} <span class="aq-sub">基于 Alpaca Paper 账户进行模拟交易（不涉及真实资金）'
                '· Live 交易永久禁用</span></div>', unsafe_allow_html=True)
    locked = not http.paper_orders_unlocked()
    if not locked:
        st.markdown('<div class="aq-ok">✅ 已授权 Alpaca Paper 模拟下单：BTC 趋势组合 + 美股 ETF 择时（全部为模拟资金）。'
                    '真实资金交易永久关闭；DeepSeek 不参与下单，交易只由规则触发。</div>', unsafe_allow_html=True)
    if locked:
        st.markdown('<div class="aq-warn">🔒 实际订单权限锁定：用户尚未授权提交任何 Alpaca Paper 订单（PAPER_ORDERS = 0，LIVE_ORDERS = 0）。'
                    '页面可以完成选择策略、保存部署配置和只读连接检查；『启动模拟交易』对 Alpaca Paper 保持锁定，'
                    '只有在未来单独明确授权后才可解除。研究 API Key 或只读凭证不构成交易授权。</div>', unsafe_allow_html=True)
    show_flash("paper")
    portfolio_panel()
    with st.expander("单个策略部署 / 只读检查 / 手动控制（高级）", expanded=False):
        _single_section()
    deployments_and_logs()


@st.fragment(run_every=5.0)
def portfolio_live() -> None:
    from ..paper import portfolio as pf
    rows = pf.overview()
    acct, pos = _acct(st.session_state.get("pp_nonce", 0) + int(datetime.now().timestamp() // 30))
    hb = db.get_setting("paper_loop_heartbeat")
    running = [r for r in rows if r["raw_status"] in ("running", "paused_entries")]
    filled = sum(r["filled"] for r in rows)
    price = None
    try:
        from ..live.feed import FEED
        price = FEED.snapshot()["mid"]
    except Exception:  # noqa: BLE001
        pass
    c = st.columns(5)
    c[0].metric("运行中的策略", f"{len(running)} 个")
    c[1].metric("已成交订单", f"{filled} 笔")
    c[2].metric("Paper 账户权益", f"{acct.equity:,.2f}" if acct else "--")
    c[3].metric("账户 BTC 持仓", f"{(pos or {}).get('qty', 0):.6f}" if acct else "--")
    c[4].metric("自动循环心跳", (str(hb)[11:19] + " UTC") if hb else "--", help="每 60 秒检查一次全部策略")
    whys = {}
    try:   # “为什么没有动作”：与策略总台同一套解释
        from .. import console
        plans, vol = console.plans_for(), console.vol_sizing()
        for r in rows:
            if r["raw_status"] in ("running", "paused_entries"):
                whys[r["id"]] = console.explain(pr.get_deployment(r["id"]), plans.get(r["id"]), price, vol)
    except Exception:  # noqa: BLE001
        whys = {}
    if rows:
        tbl = []
        for r in rows:
            pnl = r["cash_flow"] + (r["qty"] * price if price else 0)
            act = {"buy": "🔵 买入", "sell": "🟠 卖出", "none": "观望", "blocked": "⛔ 阻止", "error": "❗ 异常"}.get(
                r["last_action"], r["last_action"] or "--")
            tbl.append({"部署": f"#{r['id']}", "策略": r["label"], "状态": r["status"],
                        "资金额度 USD": r["budget"], "持仓 BTC": round(r["qty"], 6),
                        "盈亏 USD（含浮动）": round(pnl, 2) if r["filled"] else 0.0,
                        "成交笔数": r["filled"], "最近判断（UTC）": (r["last_at"] or "")[11:19],
                        "动作": act, "原因": r["last_reason"] or "--",
                        "为什么没有动作": (whys.get(r["id"]) or {}).get("headline") or "--"})
        st.dataframe(pd.DataFrame(tbl), hide_index=True, use_container_width=True,
                     column_config={"资金额度 USD": st.column_config.NumberColumn(format="%,.0f"),
                                    "盈亏 USD（含浮动）": st.column_config.NumberColumn(format="%+,.2f")})
        st.caption("『观望』表示程序已检查过、条件不满足所以没下单；每个策略只在自己的K线收盘、条件满足时才会买卖。"
                   "日线策略可能几天才交易一次，15 分钟策略一般几小时内会有动作。")
        if whys:
            from .page_console import why_block
            with st.expander("每个策略为什么没有动作（冷却、体检、信号过期、资金权重和仓位的计算）"):
                for r in rows:
                    if r["id"] in whys:
                        st.markdown(f"**#{r['id']} {C.esc(r['label'])}**")
                        why_block(whys[r["id"]], key=f"pp{r['id']}")
    else:
        st.info("还没有模拟盘部署。")
    ords = _broker_orders(int(datetime.now().timestamp() // 15))
    if ords:
        st.markdown("**Alpaca Paper 账户里的真实订单记录（券商侧，只读）**")
        st.dataframe(pd.DataFrame(ords), hide_index=True, use_container_width=True, height=220)


@st.cache_data(ttl=15, show_spinner=False, max_entries=4)
def _broker_orders(nonce: int) -> list[dict]:
    from ..data import alpaca as _al
    code, b = _al.paper_get("/v2/orders", {"status": "all", "limit": 50, "direction": "desc"})
    if code != 200 or not isinstance(b, list):
        return []
    out = []
    for o in b:
        out.append({"提交时间(UTC)": (o.get("submitted_at") or "")[:19].replace("T", " "), "方向": "买入" if o.get("side") == "buy" else "卖出",
                    "数量": o.get("qty"), "成交均价": o.get("filled_avg_price"), "状态": o.get("status"),
                    "订单编号(client_order_id)": o.get("client_order_id")})
    return out


def portfolio_panel() -> None:
    from ..paper import portfolio as pf
    with st.container(border=True):
        C.section("全部策略 · 模拟盘自动交易总览",
                  right=C.pill(f"交易日志：logs/trading_{datetime.now():%Y%m%d}.log", "gray"))
        st.caption("5 个研究策略 + 3 个实时预设策略在同一个 Alpaca Paper 账户自动交易；每个策略独立资金额度和订单台账；"
                   "只做多、不加杠杆；每笔订单和每次判断都写入日志。")
        c = st.columns([1.3, 1.3, 3])
        if c[0].button("① 准备：补齐策略回测", key="pf_prep", use_container_width=True):
            msgs = pf.submit_missing_backtests()
            flash("paper", "ok", "；".join(msgs) if msgs else "全部策略已有真实数据回测，可直接部署")
            st.rerun()
        if c[1].button("② 一键部署并启动全部策略", key="pf_go", type="primary", use_container_width=True):
            try:
                from .login import effective_user, role
                res = pf.deploy_all(actor=effective_user(), role=role())
                flash("paper", "ok", " ｜ ".join(res))
            except Exception as e:  # noqa: BLE001
                flash("paper", "err", str(e))
            st.rerun()
        c[2].caption("资金额度按策略实力自动分配（每轮进化后调整）。已在运行的策略会被并入组合，不会重复部署，"
                     "也不会被替换成实验室的新版本；单个策略的部署、换版本、下线请到「策略总台」。")
        portfolio_live()
    etf_panel()
    leverage_panel()


@st.fragment(run_every=30.0)
def etf_panel() -> None:
    from ..paper import etf_trend as et
    et.ensure_tables()
    cfg = et.get_config()
    stt = db.get_setting("etf_trend_state") or {}
    sig = stt.get("signal") or {}
    pos = stt.get("position") or {}
    with st.container(border=True):
        C.section(f"美股 ETF 趋势择时：{cfg['signal_symbol']} {cfg['sma']} 日均线 → {cfg['trade_symbol']}（2 倍纳指）",
                  right=C.pill(f"资金比例 {cfg['budget_pct']:g}%", "blue"))
        st.caption(f"{cfg['signal_symbol']} 收盘站上 {cfg['sma']} 日均线 {cfg['band']:.0%} 以上时持有 {cfg['trade_symbol']}，"
                   f"跌破均线 {cfg['band']:.0%} 以下时卖出持有现金；美股开盘 5 分钟后按前一日收盘信号下单，每天最多一次。"
                   "10.8 年回测：年化约 +30%，最大回撤 -34%（一直持有 QLD 回撤 -64%）。")
        c = st.columns(5)
        c[0].metric("当前信号", ("看多 · 持有" if sig.get("regime") else "看空 · 空仓") if sig else "等待美股开盘")
        c[1].metric(f"{cfg['signal_symbol']} 收盘 / 均线", f"{sig['close']:,.2f} / {sig['sma']:,.2f}" if sig else "--")
        c[2].metric(f"{cfg['trade_symbol']} 持仓（股）", f"{pos.get('qty', 0):,.4f}" if pos else "--")
        c[3].metric("持仓市值 / 浮动盈亏", f"{pos.get('market_value', 0):,.0f} / {pos.get('unrealized_pl', 0):+,.0f}" if pos else "--")
        c[4].metric("最近检查", (str(stt.get("checked_at"))[5:16].replace("T", " ") + " UTC") if stt else "--")
        if stt.get("action"):
            st.caption(f"最近动作：{stt['action']}（信号日 {sig.get('date')}）")
        why = et.get_why()
        if why:
            st.caption(f"现在：{why['text']}")
        od = et.orders(10)
        if od:
            st.dataframe(pd.DataFrame([{"时间(UTC)": o["ts"][:16].replace("T", " "), "方向": "买入" if o["side"] == "buy" else "卖出",
                                        "品种": o["symbol"], "金额/数量": o["notional"] or o["qty"], "状态": o["status"],
                                        "成交价": o["filled_price"], "原因": o["reason"]} for o in od]),
                         hide_index=True, use_container_width=True, height=160)


@st.fragment(run_every=10.0)
def leverage_panel() -> None:
    from ..paper import leverage_shadow as ls
    ls.ensure_tables()
    price = None
    try:
        from ..live.feed import FEED
        price = FEED.snapshot()["mid"]
    except Exception:  # noqa: BLE001
        pass
    sm = ls.summary(price)
    lbl = (registry_label_for(sm["source"]) or f"部署 #{sm['source']}")
    with st.container(border=True):
        C.section(f"{sm['leverage']:g} 倍杠杆模拟账户", purple=True,
                  right=C.pill(f"跟随 {lbl}", "purple"))
        st.caption("把核心策略的每一笔真实 Paper 成交，按永续合约逐仓规则放大到 2 倍记账：含 0.07% 手续费、每 8 小时 0.01% 资金费率、"
                   "强平规则。只是本地计算，不连接任何交易所、不下单。用来实时观察“加杠杆后会怎样”。")
        c = st.columns(5)
        c[0].metric("保证金权益 USD", f"{sm['equity']:,.2f}", f"{sm['pnl_pct']:+.2f}%")
        c[1].metric("持仓 BTC", f"{sm['qty']:.6f}")
        c[2].metric("开仓价", f"{sm['entry']:,.2f}" if sm["entry"] else "--")
        c[3].metric("强平价", f"{sm['liq']:,.2f}" if sm["liq"] else "--")
        c[4].metric("已付手续费 / 资金费", f"{sm['fees']:,.2f} / {sm['funding']:,.2f}")
        tr = ls.trades(20)
        if tr:
            st.dataframe(pd.DataFrame([{"时间(UTC)": t["ts"][:16].replace("T", " "), "类型": t["kind"],
                                        "价格": t["price"], "数量 BTC": t["qty"], "手续费": t["fee"],
                                        "盈亏": t["pnl"], "之后权益": t["equity_after"], "说明": t["note"]} for t in tr]),
                         hide_index=True, use_container_width=True, height=200)
        else:
            st.caption(f"起始保证金 {sm['margin0']:,.0f} USD，自 {sm['started_at'][:16].replace('T', ' ')} UTC 起等待核心策略的下一次买入。")


def registry_label_for(did: int) -> str | None:
    d = db.q1("SELECT config_json FROM paper_deployments WHERE id=?", (did,))
    return json.loads(d["config_json"]).get("label") if d else None


def _single_section() -> None:
    vers = _version_opts()
    left, mid, right = st.columns([1.25, 1.6, 0.9], gap="medium")
    vers = _version_opts()
    with left:
        with st.container(border=True):
            C.section("部署策略到模拟盘")
            st.markdown('<div class="aq-step"><span class="on"><i>1</i>选择策略</span><span class="on"><i>2</i>部署设置</span>'
                        '<span class="on"><i>3</i>风险确认</span><span><i>4</i>启动交易</span></div>', unsafe_allow_html=True)
            if not vers:
                st.info("还没有冻结的策略版本。请先在策略实验室保存版本并完成回测。")
                return
            ids = [v["id"] for v in vers]
            pre = st.session_state.get("deploy_version")
            idx = ids.index(pre) if pre in ids else 0
            vid = st.selectbox("选择策略版本", ids, index=idx, key="pp_vid",
                               format_func=lambda i: next(f"策略位{v['slot_no']} · {v['name']} v{v['version_no']}"
                                                          for v in vers if v["id"] == i))
            v = registry.get_version(vid)
            b = research.latest_backtest_for_version(vid, include_test=True)
            fin = db.q1("SELECT b.metrics_json FROM holdout_touches h JOIN backtests b ON b.id=h.backtest_id "
                        "WHERE h.version_id=? AND h.split='final_test'", (vid,))
            val = db.q1("SELECT COUNT(*) AS n FROM holdout_touches WHERE version_id=? AND split='validation'", (vid,))["n"]
            m = b["metrics"] if b else {}
            st.markdown(C.kv([
                ("策略名称", v["name"]), ("策略版本", f"v{v['version_no']}（{v['source']}）"),
                ("冻结哈希", v["schema_hash"][:24]), ("信号周期", TF_LABEL.get(b["signal_tf"], "--") if b else "--"),
                ("回测净收益", f"{C.pct(m.get('net_return_pct'))}（{b['split_label']}{'·测试数据' if b['is_test'] else ''}，历史回测非前瞻收益）" if b else "--（无回测）"),
                ("最大回撤", C.pct(m.get("max_drawdown_pct"))), ("交易次数", C.num(m.get("trades"), 0) if m else "--"),
                ("独立验证", ("独立测试 " + C.pct(json.loads(fin["metrics_json"])["net_return_pct"])) if fin
                 else (f"验证集 {val} 次；未做独立测试" if val else "未验证")),
            ]), unsafe_allow_html=True)
            ok, checks = pr.deploy_gate(vid)
            for nm, p, dt in checks:
                st.markdown(f'{"✅" if p else "❌"} {C.esc(nm)} <span class="aq-sub">{C.esc(dt)}</span>', unsafe_allow_html=True)
            st.divider()
            with st.form("deploy_form", border=False):
                broker = st.radio("执行通道", ["alpaca_paper", "mock"], horizontal=True,
                                  format_func=lambda x: "Alpaca Paper（订单锁定）" if x == "alpaca_paper" else "Mock Broker（测试）")
                c = st.columns(2)
                alloc = c[0].number_input("使用账户可用资金 %", 1.0, 100.0, 100.0, 5.0)
                cap = c[1].number_input("单笔最大金额 USD（0=不限）", 0.0, 1e7, 0.0, 100.0)
                stop_mode = st.selectbox("停止策略时的持仓处理", ["keep", "ask"],
                                         format_func=lambda x: "保留持仓（默认，不悄悄平仓）" if x == "keep" else "每次询问")
                ack = st.checkbox("我确认：训练回测收益不代表未来；Paper 只用于模拟；DeepSeek 不会直接下单；运行中的策略不会自动替换版本。")
                if st.form_submit_button("保存部署配置", type="primary", use_container_width=True):
                    if not ack:
                        flash("paper", "err", "请先勾选风险确认")
                    else:
                        try:
                            did = pr.save_deployment(vid, {"alloc_pct": alloc, "max_position_usd": cap or None,
                                                           "stop_mode": stop_mode, "signal_tf": b["signal_tf"] if b else None,
                                                           "aux_tf": b["aux_tf"] if b else None,
                                                           "fee_bps": research.get_research()["fee_bps"]}, broker)
                            st.session_state["pp_did"] = did
                            flash("paper", "ok", f"部署配置 #{did} 已保存（{broker}）")
                        except Exception as e:  # noqa: BLE001
                            flash("paper", "err", str(e))
                    st.rerun()
    with mid:
        paper_account_panel(vid)
    with right:
        control_panel()


@st.cache_data(ttl=30, show_spinner=False, max_entries=8)
def _acct(nonce: int):
    # 只缓存内置类型：缓存里若存自定义类实例，模块被重新加载后会出现“不是同一个类”的序列化错误
    from dataclasses import asdict
    from types import SimpleNamespace
    ad = AlpacaPaperAdapter()
    a = ad.account()
    pos = ad.position() if a else None
    return (SimpleNamespace(**asdict(a)) if a else None), (dict(pos) if pos else None)


@st.cache_data(ttl=30, show_spinner=False, max_entries=8)
def _readiness(vid: int, nonce: int):
    return pr.readiness(vid)


def paper_account_panel(vid: int) -> None:
    with st.container(border=True):
        h = st.columns([3, 1])
        h[0].markdown('<div class="aq-section"><span class="bar"></span>Alpaca Paper 账户（只读）</div>', unsafe_allow_html=True)
        if h[1].button("刷新数据", key="pp_refresh", use_container_width=True):
            st.session_state["pp_nonce"] = st.session_state.get("pp_nonce", 0) + 1
            st.session_state["paper_nonce"] = st.session_state.get("paper_nonce", 0) + 1
            st.rerun()
        acct, pos = _acct(st.session_state.get("pp_nonce", 0))
        if acct:
            c = st.columns(4)
            c[0].metric("账户权益", f"{acct.equity:,.2f} USD")
            c[1].metric("现金余额", f"{acct.cash:,.2f} USD")
            c[2].metric("可用购买力", f"{acct.buying_power:,.2f} USD")
            c[3].metric("BTC 持仓", f"{(pos or {}).get('qty', 0):.6f}")
            c = st.columns(3)
            c[0].metric("持仓市值", f"{(pos or {}).get('market_value', 0):,.2f} USD")
            c[1].metric("未实现盈亏", f"{(pos or {}).get('unrealized_pl', 0):,.2f} USD")
            c[2].metric("已实现盈亏", "见 Alpaca 账户活动", help="Alpaca 账户接口不直接提供累计已实现盈亏；本系统未提交过任何 Paper 订单")
            st.caption("以上为 Alpaca Paper 实际账户数据，与策略虚拟研究本金（全局设置中的初始资金）完全独立。")
        else:
            st.markdown('<div class="aq-warn">无法读取 Alpaca Paper 账户：请在项目根目录 .env 配置 ALPACA_PAPER_KEY_ID / '
                        'ALPACA_PAPER_SECRET_KEY 并确认网络可访问 paper-api.alpaca.markets。</div>', unsafe_allow_html=True)
        st.markdown("**只读连接检查**")
        for nm, p, dt in _readiness(vid, st.session_state.get("pp_nonce", 0)):
            st.markdown(f'{"✅" if p else "⛔"} {C.esc(nm)} <span class="aq-sub">{C.esc(dt)}</span>', unsafe_allow_html=True)


def control_panel() -> None:
    with st.container(border=True):
        C.section("策略运行控制")
        deps = db.q("SELECT * FROM paper_deployments ORDER BY id DESC LIMIT 30")
        if not deps:
            st.caption("尚无部署配置")
            return
        ids = [d["id"] for d in deps]
        pre = st.session_state.get("pp_did")
        did = st.selectbox("部署", ids, index=ids.index(pre) if pre in ids else 0, key="pp_sel",
                           format_func=lambda i: next(f"#{d['id']} {d['broker']} · 版本{d['version_id']} · "
                                                      f"{pr.STATE_CN.get(d['status'], d['status'])}" for d in deps if d["id"] == i))
        dep = pr.get_deployment(did)
        st.markdown(C.pill(pr.STATE_CN.get(dep["status"], dep["status"]),
                           "green" if dep["status"] == "running" else "orange" if dep["status"] == "paused_entries" else "gray")
                    + (" " + C.pill("测试", "orange") if dep["is_test"] else ""), unsafe_allow_html=True)
        is_alp = dep["broker"] == "alpaca_paper"
        locked = is_alp and not http.paper_orders_unlocked()
        if st.button("▶ 启动模拟交易", key="pp_start", type="primary", use_container_width=True, disabled=locked,
                     help="Alpaca Paper 订单权限锁定（PAPER_ORDERS=0）" if locked else None):
            try:
                pr.start(did)
                flash("paper", "ok", f"部署 #{did} 已启动（{dep['broker']}）")
            except (OrderPermissionLocked, ValueError) as e:
                flash("paper", "err", str(e))
            st.rerun()
        if locked:
            st.caption("🔒 启动门禁锁定：需用户未来单独明确授权。")
        if is_alp:
            lp = db.get_setting(f"paper_loop::{did}")
            hb = db.get_setting("paper_loop_heartbeat")
            st.caption(f"运行循环心跳：{(hb or '--')[:19]} UTC · 每 60 秒检查一次")
            if lp:
                st.caption(f"最近一次决策 {str(lp.get('at', ''))[:19]}：{lp.get('action')} · {lp.get('reason', '')}"
                           + (f" · 移动止损价 {lp['trailing_stop']}" if lp.get("trailing_stop") else ""))
        if st.button("暂停新开仓", key="warn_pause", use_container_width=True):
            try:
                pr.pause_entries(did)
            except ValueError as e:
                flash("paper", "err", str(e))
            st.rerun()
        if st.button("停止策略（保留持仓）", key="pp_stop", use_container_width=True):
            pr.stop(did); flash("paper", "ok", "已停止策略，持仓未被平掉"); st.rerun()
        conf = st.checkbox("我确认执行撤单/平仓操作", key="pp_conf")
        c = st.columns(2)
        if c[0].button("撤销未成交订单", key="warn_cancel", use_container_width=True):
            try:
                pr.cancel_orders(did, conf); flash("paper", "ok", "已撤单")
            except Exception as e:  # noqa: BLE001
                flash("paper", "err", str(e))
            st.rerun()
        if c[1].button("平掉模拟持仓", key="warn_flat", use_container_width=True):
            try:
                pr.flatten(did, conf); flash("paper", "ok", "已平仓")
            except Exception as e:  # noqa: BLE001
                flash("paper", "err", str(e))
            st.rerun()
        if st.button("⛔ 紧急停止", key="danger_estop", use_container_width=True):
            pr.emergency_stop(did); flash("paper", "warn", "已紧急停止（持仓未自动平仓）"); st.rerun()
        if dep["broker"] == "mock":
            st.divider()
            st.markdown("**Mock Broker 回放测试**（测试数据，不计入正式研究）")
            n = st.number_input("回放 5 分钟K线数", 100, 5000, 600, 100, key="pp_n")
            rs = st.checkbox("中途模拟服务重启", value=True, key="pp_rs")
            if st.button("运行回放测试", key="pp_replay", use_container_width=True):
                if dep["status"] not in pr.ACTIVE_STATES:
                    flash("paper", "err", "请先启动该 Mock 部署")
                else:
                    ds = db.q1("SELECT * FROM datasets WHERE end_utc<=? ORDER BY id DESC LIMIT 1",
                               (research.get_split()["val_start"],))
                    if not ds:
                        flash("paper", "err", "没有可用的行情快照")
                    else:
                        with st.spinner("回放中…"):
                            res = pr.mock_replay(did, md.load_dataset(ds), int(n), int(n) // 2 if rs else None)
                        flash("paper", "ok", f"回放完成：{res}")
                st.rerun()
            mb = MockBroker(did)
            p = mb.position()
            st.caption(f"Mock 账户：现金 {mb.account().cash:,.2f}，BTC {p['qty']:.6f}，已实现 {p['realized_pl']:,.2f}")


def deployments_and_logs() -> None:
    with st.container(border=True):
        t1, t2, t3 = st.tabs(["订单台账（Order State Ledger）", "运行日志", "部署配置"])
        with t1:
            o = pd.DataFrame(db.q("SELECT id,deployment_id,client_order_id,broker,side,qty,status,filled_price,signal_ts,"
                                  "created_at,is_test FROM paper_orders ORDER BY id DESC LIMIT 300"))
            real = db.q1("SELECT COUNT(*) AS n FROM paper_orders WHERE broker='alpaca_paper' AND status='filled'")["n"]
            st.markdown(f"已提交到 Alpaca Paper 的订单：**{real}**（PAPER_ORDERS）；Live 订单：**0**（LIVE_ORDERS）")
            st.dataframe(o, use_container_width=True, hide_index=True, height=240)
        with t2:
            e = pd.DataFrame(db.q("SELECT ts,deployment_id,level,message,is_test FROM paper_events ORDER BY id DESC LIMIT 300"))
            st.dataframe(e, use_container_width=True, hide_index=True, height=240)
        with t3:
            d = pd.DataFrame(db.q("SELECT id,version_id,broker,status,created_at,updated_at,is_test,config_json "
                                  "FROM paper_deployments ORDER BY id DESC"))
            st.dataframe(d, use_container_width=True, hide_index=True, height=240)
