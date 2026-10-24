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
    st.markdown('<div class="aq-title">策略总台</div>'
                '<div class="aq-sub">每个策略一行：实验室最新版本与模拟盘运行版本是否一致、挑战者进度、持仓和资金权重。'
                '<b>手动保存的新版本不会替换运行中的版本，需要在这里确认部署</b>；换版本和下线都只在该策略空仓、'
                '没有未完成订单时生效，否则先等待空仓。</div>', unsafe_allow_html=True)
    show_flash("console")
    n_run = sum(1 for r in rows if r["status"] in ("运行中", "暂停中"))
    n_mis = sum(1 for r in rows if r["mismatch"])
    n_ch = sum(1 for r in rows if r["challenger"])
    n_pend = sum(1 for r in rows if r["pending"] or r["pending_offline"])
    cells = [("策略总数", f"{len(rows)}", ""), ("模拟盘运行中", f"{n_run}", ""),
             ("版本不一致", f"{n_mis}", "color:#EA8A0C" if n_mis else ""), ("挑战者 / 待生效", f"{n_ch} / {n_pend}", "")]
    st.markdown('<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin:6px 0 10px">'
                + "".join(f'<div class="aq-m"><div class="l">{C.esc(l)}</div><div class="n" style="font-size:22px;{s}">'
                          f'{C.esc(v)}</div></div>' for l, v, s in cells) + "</div>", unsafe_allow_html=True)
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
            else:
                st.markdown(C.kv([("挑战者", "无")]), unsafe_allow_html=True)
        with cols[2]:
            st.markdown(C.kv([("持仓", f"{r['qty']:.6f} BTC" if dep else "--"),
                              ("市值", C.usd(r["value"]) if dep and r["value"] is not None else "--"),
                              ("资金权重", C.pct(r["weight_pct"], sign=False, digits=1) if r["weight_pct"] is not None else "--"),
                              ("额度", C.usd(r["budget"]) if dep else "--")]), unsafe_allow_html=True)
        with cols[3]:
            _actions(r, actor, rl)
        if r["mismatch"] and r["diff"]:
            with st.expander(f"规则差异：模拟盘 {_vlabel(run)} → 实验室 {_vlabel(lab)}（{len(r['diff'])} 项）",
                             expanded=focus):
                st.dataframe(pd.DataFrame([{"项目": k, f"模拟盘 {_vlabel(run)}": a, f"实验室 {_vlabel(lab)}": b}
                                           for k, a, b in r["diff"]]), hide_index=True, use_container_width=True)


def _actions(r: dict, actor: str, rl: str) -> None:
    key = r["key"]
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
