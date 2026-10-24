"""策略总台：每个策略一行卡片 —— 来源、状态、实验室版本 vs 运行版本、挑战者、持仓与资金权重；单个策略的部署 / 下线。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from .. import console
from ..strategy import registry
from . import components as C
from .common import flash, show_flash

BJ = timezone(timedelta(hours=8))
STATUS_TONE = {"草稿": "gray", "已回测": "blue", "已验证": "purple", "已部署": "blue", "运行中": "green",
               "暂停中": "orange", "已下线": "gray"}
TF_CN = {15: "15分钟", 30: "30分钟", 60: "1小时", 240: "4小时", 1440: "日线"}


def _bj(iso: str | None) -> str:
    if not iso:
        return "--"
    try:
        return pd.Timestamp(iso).tz_convert(BJ).strftime("%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return str(iso)[5:16]


def _vlabel(v: dict | None) -> str:
    return f"v{v['version_no']}" if v else "--"


def _price() -> float | None:
    try:
        from ..live.feed import FEED
        return float(FEED.snapshot()["mid"])
    except Exception:  # noqa: BLE001
        return None


def render() -> None:
    from .login import effective_user, role
    rows = console.rows(price=_price())
    st.markdown('<div class="aq-title">策略总台</div>', unsafe_allow_html=True)
    _rules_panel(effective_user(), role())
    show_flash("console")
    n_run = sum(1 for r in rows if r["status"] in ("运行中", "暂停中"))
    n_mis = sum(1 for r in rows if r["mismatch"])
    n_ch = sum(1 for r in rows if r["challenger"])
    n_pend = len(console.awaiting())
    cells = [("策略总数", f"{len(rows)}", ""), ("模拟盘运行中", f"{n_run}", ""),
             ("版本不一致", f"{n_mis}", "color:#EA8A0C" if n_mis else ""), ("挑战者 / 待确认", f"{n_ch} / {n_pend}", "color:#7C3AED" if n_pend else "")]
    st.markdown('<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin:6px 0 10px">'
                + "".join(f'<div class="aq-m"><div class="l">{C.esc(l)}</div><div class="n" style="font-size:22px;{s}">'
                          f'{C.esc(v)}</div></div>' for l, v, s in cells) + "</div>", unsafe_allow_html=True)
    _queue_panel(effective_user(), role())
    _deploy_all_bar(effective_user(), role())
    focus = st.session_state.pop("console_focus", None)
    for r in rows:
        _card(r, effective_user(), role(), focus == r["key"])
    _audit_panel()


def _deploy_all_bar(actor: str, rl: str) -> None:
    from ..paper import portfolio as pf
    with st.container(border=True):
        cols = st.columns([1.4, 3])
        if cols[0].button("一键部署全部", key="cs_deploy_all", type="primary", use_container_width=True):
            try:
                res = pf.deploy_all(actor=actor, role=rl)
                flash("console", "ok", " ｜ ".join(res))
            except Exception as e:  # noqa: BLE001
                flash("console", "err", str(e))
            st.rerun()
        cols[1].caption("部署所有未下线、尚未运行的策略；已在运行的策略只调整资金额度，保持原版本。"
                        "在本页下线的策略不会被重新部署。")


def _card(r: dict, actor: str, rl: str, focus: bool) -> None:
    lab, run, dep = r["lab"], r["run"], r["deployment"]
    with st.container(border=True):
        pills = C.pill(r["status"], STATUS_TONE.get(r["status"], "gray"))
        if r["mismatch"]:
            pills += " " + C.pill("版本不一致", "orange")
        if r["pending"]:
            pills += " " + C.pill("等待空仓后切换", "purple")
        if r["pending_offline"]:
            pills += " " + C.pill("等待空仓后下线", "orange")
        if r["lock"]:
            pills += " " + C.pill("已锁定", "blue")
        if r["awaiting"]:
            pills += " " + C.pill("挑战者胜出，等待确认", "purple")
        if focus:
            pills += " " + C.pill("刚从实验室跳转", "blue")
        st.markdown(f'<div class="aq-slothead"><div><span class="aq-slotname">{C.esc(r["name"])}</span> '
                    f'<span class="aq-meta">{C.esc(r["origin"])} · 来源：{C.esc(r["source_cn"])}</span></div>'
                    f'<div>{pills}</div></div>', unsafe_allow_html=True)
        cols = st.columns([1.5, 1.2, 1.0, 1.4])
        with cols[0]:
            tf = r["signal_tf"]
            st.markdown(C.kv([("实验室", f"{_vlabel(lab)} · {registry.label(lab)}" if lab else "尚未保存版本"),
                              ("模拟盘", f"{_vlabel(run)} · {registry.label(run)}" if run else "未部署"),
                              ("K线周期", TF_CN.get(int(tf), f"{tf}分钟") if tf else "--")]),
                        unsafe_allow_html=True)
            if r["mismatch"]:
                side = ("实验室有更新的版本，尚未部署" if r["lab_newer"]
                        else "模拟盘已自动进化到更新的版本")
                st.markdown(f'<div class="aq-warn">版本不一致：实验室 {_vlabel(lab)} ≠ 模拟盘 {_vlabel(run)}。{side}</div>',
                            unsafe_allow_html=True)
            if r["pending"]:
                p = r["pending"]
                st.markdown(f'<div class="aq-sub">待切换到 {C.esc(registry.label(p["version"]))}'
                            f'（{C.esc(console.TRIGGER_CN.get(p["trigger"], p["trigger"]))}，{_bj(p["at"])} 提交）：'
                            '该策略空仓且没有未完成订单时生效</div>', unsafe_allow_html=True)
            if r["status"] == "草稿" and not lab and r["draft"]:
                st.caption("只有草稿，请先在策略实验室保存版本并回测")
        with cols[1]:
            ch = r["challenger"]
            if ch:
                prog = (f"{float(ch['days'] or 0):.1f} / {ch['need_days']} 天" if ch.get("need_days")
                        else "等待首次评估")
                fast = {"pass": "通过", "fail": "更差", "inconclusive": "证据不足，转前向"}.get(ch.get("fast"), "--")
                st.markdown(C.kv([("挑战者", f"#{ch['id']}"), ("改动", ch["change"] or "--"),
                                  ("前向进度", prog), ("快速检验", fast)]), unsafe_allow_html=True)
                cv, mv = ch.get("challenger") or {}, ch.get("champion") or {}
                if cv and mv:
                    st.caption(f"前向收益：挑战者 {cv.get('return_pct', 0):+.2f}% · 现役 {mv.get('return_pct', 0):+.2f}%")
            elif not r["awaiting"]:
                st.markdown(C.kv([("挑战者", "无")]), unsafe_allow_html=True)
            if r["awaiting"]:
                a = r["awaiting"]
                st.markdown(f'<div class="aq-sub" style="color:#6D28D9">挑战者 #{a["id"]} 胜出，等待确认：'
                            f'{C.esc(a["change"] or "")}。请在上方「待确认」里批准或拒绝。</div>', unsafe_allow_html=True)
        with cols[2]:
            st.markdown(C.kv([("持仓", f"{r['qty']:.6f} BTC" if dep else "--"),
                              ("市值", C.usd(r["value"]) if dep and r["value"] is not None else "--"),
                              ("资金权重", C.pct(r["weight_pct"], sign=False, digits=1) if r["weight_pct"] is not None else "--"),
                              ("额度", C.usd(r["budget"]) if dep else "--")]), unsafe_allow_html=True)
        with cols[3]:
            _actions(r, actor, rl)
        if r.get("why"):
            why_block(r["why"], key=r["key"])
        if r["mismatch"] and r["diff"]:
            with st.expander(f"规则差异：模拟盘 {_vlabel(run)} → 实验室 {_vlabel(lab)}（{len(r['diff'])} 项）",
                             expanded=focus):
                st.dataframe(pd.DataFrame([{"项目": k, f"模拟盘 {_vlabel(run)}": a, f"实验室 {_vlabel(lab)}": b}
                                           for k, a, b in r["diff"]]), hide_index=True, use_container_width=True)


def _lock_toggle(r: dict, actor: str, rl: str) -> None:
    key, lk = r["key"], r["lock"]
    if lk and lk.get("until"):
        why = ("手动部署后自动锁定" if lk.get("kind") == "manual_deploy" else "已锁定") + f"，至 {_bj(lk['until'])}"
    elif lk:
        why = f"{lk.get('by') or ''} 手动锁定，不会自动到期"
    else:
        why = "未锁定：挑战者胜出后按规则自动晋级" if not r["block"] else f"未锁定（{r['block']}）"
    on = st.toggle("锁定版本", value=bool(lk), key=f"cs_lock_{key}", help="锁定后，自动进化仍会生成并前向测试挑战者，"
                   "但胜出后不会自动替换，只进入待确认队列")
    st.caption(why)
    if on != bool(lk):
        try:
            res = console.set_lock(key, on, actor=actor, role=rl)
            flash("console", "ok", f"{r['name']}：{res['message']}")
        except Exception as e:  # noqa: BLE001
            flash("console", "err", f"{r['name']}：{e}")
        st.rerun()


def _actions(r: dict, actor: str, rl: str) -> None:
    key = r["key"]
    if r["deployment"]:
        _lock_toggle(r, actor, rl)
    vs = console.versions_of(key)
    if not vs:
        st.caption("还没有可部署的版本")
        return
    ids = [v["id"] for v in vs]
    pick_key = f"console_pick::{key}"
    want = st.session_state.get(pick_key) or (r["lab"] or {}).get("id")
    idx = ids.index(want) if want in ids else 0
    tags = {}
    for v in vs:
        t = []
        if r["lab"] and v["id"] == r["lab"]["id"]:
            t.append("实验室")
        if r["run"] and v["id"] == r["run"]["id"]:
            t.append("运行中")
        tags[v["id"]] = f"v{v['version_no']} · {console.SOURCE_CN.get(v['source'], v['source'])}" + \
            (f"（{'、'.join(t)}）" if t else "")
    vid = st.selectbox("选择版本", ids, index=idx, key=f"cs_v_{key}", format_func=lambda i: tags.get(i, str(i)))
    if st.button("部署此版本", key=f"cs_dep_{key}", type="primary", use_container_width=True):
        try:
            res = console.deploy_version(key, int(vid), actor=actor, role=rl)
            flash("console", "ok" if res["result"] != "等待空仓后切换" else "warn", f"{r['name']}：{res['message']}")
        except Exception as e:  # noqa: BLE001
            flash("console", "err", f"{r['name']}：{e}")
        st.session_state.pop(pick_key, None)
        st.rerun()
    if r["deployment"] and not r["pending_offline"]:
        conf = st.checkbox("确认下线", key=f"cs_offc_{key}",
                           help="空仓时立即停止；有持仓时先暂停新开仓，持仓按规则卖出后自动下线。不会主动平仓。")
        if st.button("下线", key=f"cs_off_{key}", use_container_width=True, disabled=not conf):
            try:
                res = console.take_offline(key, actor=actor, role=rl)
                flash("console", "ok" if res["result"] == "已下线" else "warn", f"{r['name']}：{res['message']}")
            except Exception as e:  # noqa: BLE001
                flash("console", "err", f"{r['name']}：{e}")
            st.rerun()


def _audit_panel() -> None:
    log = console.audit_log(60)
    with st.expander(f"部署 / 下线审计日志（最近 {len(log)} 条）"):
        if not log:
            st.caption("还没有记录")
            return
        lab = {}

        def vl(i):
            if not i:
                return "--"
            if i not in lab:
                lab[i] = registry.label(registry.get_version(i))
            return lab[i]
        st.dataframe(pd.DataFrame([{"时间（北京）": _bj(x["ts"]), "操作人": x["actor"], "操作": x["action"],
                                    "策略": x["strategy_name"] or x["strategy_key"] or "--",
                                    "部署": f"#{x['deployment_id']}" if x["deployment_id"] else "--",
                                    "原版本": vl(x["from_version_id"]), "新版本": vl(x["to_version_id"]),
                                    "结果": x["result"], "说明": x["detail"] or ""} for x in log]),
                     hide_index=True, use_container_width=True)
        st.caption(f"更新时间 {datetime.now(BJ):%m-%d %H:%M} 北京时间")


def _rules_panel(actor: str, rl: str) -> None:
    pc = console.promotion_config()
    days = float(pc["manual_lock_days"])
    rc = bool(pc["require_confirm"])
    lock_txt = (f"你手动部署某个版本后，这个策略会自动锁定 <b>{days:g} 天</b>" if days > 0
                else "手动部署后不自动锁定（锁定天数设为 0）")
    rules = [
        "<b>你说了算</b>：实验室保存新版本不会替换模拟盘正在跑的版本，只有你在这里点「部署此版本」才会换。",
        f"<b>手动部署优先</b>：{lock_txt}，期间自动进化不会把它换掉。你也可以随时打开或关闭每个策略的「锁定版本」。",
        "<b>锁定时</b>：自动进化照常找更好的版本、照常让挑战者在实时行情上跑前向测试；但挑战者赢了也不会自动上线，"
        "只标记为「挑战者胜出，等待确认」，由你批准或拒绝。",
        "<b>没锁定时</b>：维持原来的规则，挑战者通过检验后自动晋级。" + " " +
        ("<b>当前已开启「晋级需人工确认」：所有策略（包括美股 ETF）的晋级都要你确认。</b>" if rc
         else "如果开启「晋级需人工确认」，所有策略（包括美股 ETF）的晋级都要你确认。"),
        "<b>批准 / 拒绝</b>：批准后按正常流程上线；拒绝的挑战者会被淘汰并记下原因。现役版本换过之后，旧的待确认挑战者自动作废。",
        "<b>何时生效</b>：任何换版本（手动部署、批准晋级、自动晋级）和下线，都只在这个策略空仓、没有未完成订单时生效，"
        "否则先等待空仓。",
    ]
    with st.container(border=True):
        C.section("晋级与部署规则", purple=True,
                  right=C.pill("晋级需人工确认：开" if rc else "自动晋级（锁定的策略除外）", "purple" if rc else "gray"))
        st.markdown('<ol class="aq-sub" style="margin:2px 0 6px 18px;padding:0;line-height:1.75">'
                    + "".join(f"<li>{x}</li>" for x in rules) + "</ol>", unsafe_allow_html=True)
        c = st.columns([1.2, 1, 0.8, 2])
        new_rc = c[0].toggle("晋级需人工确认", value=rc, key="cs_rc")
        new_days = c[1].number_input("手动部署后锁定天数", min_value=0, max_value=365, value=int(round(days)), step=1,
                                     key="cs_days", help="0 = 手动部署后不自动锁定")
        if c[2].button("保存规则", key="cs_rules_save", use_container_width=True):
            try:
                console.set_promotion_config(actor=actor, role=rl, require_confirm=new_rc, manual_lock_days=new_days)
                flash("console", "ok", "规则已保存")
            except Exception as e:  # noqa: BLE001
                flash("console", "err", str(e))
            st.rerun()
        c[3].caption("「晋级需人工确认」与策略进化页的同名开关是同一个设置。")


def _queue_panel(actor: str, rl: str) -> None:
    q = console.awaiting()
    if not q:
        return
    with st.container(border=True):
        C.section(f"待确认：{len(q)} 个胜出的挑战者", purple=True)
        for ch in q:
            cols = st.columns([3, 1.6])
            ev = ch.get("last_eval") or {}
            cv, mv = ev.get("challenger") or {}, ev.get("champion") or {}
            extra = (f"前向：挑战者 {cv.get('return_pct', 0):+.2f}% · 现役 {mv.get('return_pct', 0):+.2f}%"
                     if cv and mv else "")
            name = "美股 ETF 择时" if ch["kind"] == "etf" else (ch["label"] or f"部署 #{ch['deployment_id']}")
            cols[0].markdown(f'<div><b>#{ch["id"]} · {C.esc(name)}</b> <span class="aq-meta">{_bj(ch["decided_at"])}</span></div>'
                             f'<div class="aq-sub">改动：{C.esc(ch["change"] or "--")}</div>'
                             f'<div class="aq-sub">{C.esc(ch["reason"] or "")}</div>'
                             + (f'<div class="aq-sub">{C.esc(extra)}</div>' if extra else ""), unsafe_allow_html=True)
            with cols[1]:
                b = st.columns(2)
                if b[0].button("批准", key=f"cs_ap_{ch['id']}", type="primary", use_container_width=True):
                    try:
                        res = console.approve(ch["id"], actor=actor, role=rl)
                        flash("console", "ok", res["message"])
                    except Exception as e:  # noqa: BLE001
                        flash("console", "err", str(e))
                    st.rerun()
                reason = st.text_input("拒绝原因", key=f"cs_rr_{ch['id']}", placeholder="例如：回撤太大")
                if b[1].button("拒绝", key=f"cs_rj_{ch['id']}", use_container_width=True):
                    try:
                        res = console.reject(ch["id"], reason, actor=actor, role=rl)
                        flash("console", "ok", res["message"])
                    except Exception as e:  # noqa: BLE001
                        flash("console", "err", str(e))
                    st.rerun()


def why_block(w: dict, key: str = "") -> None:
    """“为什么没有动作”：一句话结论 + 原因明细 + 计算明细（资金权重、波动率定仓、当前仓位）。"""
    if not w:
        return
    st.markdown(f'<div class="aq-sub" style="margin-top:4px"><b>为什么没有动作：</b>{C.esc(w["headline"])}</div>',
                unsafe_allow_html=True)
    with st.expander("原因与计算明细"):
        for t, x in w["items"]:
            st.markdown(f'<div class="aq-sub" style="margin:3px 0;overflow-wrap:anywhere"><b>{C.esc(t)}</b>：{C.esc(x)}</div>',
                        unsafe_allow_html=True)
        if w["calc"]:
            st.markdown('<div class="aq-sub" style="margin-top:6px"><b>计算明细</b></div>', unsafe_allow_html=True)
            for t, x in w["calc"]:
                st.markdown(f'<div class="aq-sub" style="margin:3px 0;overflow-wrap:anywhere">• <b>{C.esc(t)}</b>：{C.esc(x)}</div>',
                            unsafe_allow_html=True)
