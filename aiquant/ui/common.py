"""页面共用：顶部品牌栏、Paper 真实连接状态、任务轮询。"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import streamlit as st

from .. import db, jobs
from ..data import alpaca
from .components import esc, pill


@st.cache_data(ttl=60, show_spinner=False, max_entries=4)
def paper_status_cached(_nonce: int = 0) -> dict:
    return alpaca.paper_connection_status()


def header() -> None:
    from . import brand
    from .. import auth as _auth
    from ..data import http as _http
    from ..i18n import switcher, tr
    from .login import current_user, logout_button, role
    stt = paper_status_cached(st.session_state.get("paper_nonce", 0))
    t = "green" if stt["ok"] else ("orange" if "未连接" in stt["label"] else "red")
    checked = stt.get("checked_at", "")[11:19]
    test_mode = os.environ.get("AIQUANT_DEEPSEEK_MOCK") == "1"
    info = st.session_state.get("auth_info") or {}
    where = ("外网访问 · " + esc(info.get("host", ""))) if info.get("remote") else "本地运行 · 127.0.0.1"
    left, right = st.columns([3, 2], vertical_alignment="center")
    left.markdown(
        f'<div class="aq-brand">{brand.mark_svg(40)}<div><span class="aq-title">AI Quant Strategy Lab</span>'
        f'<span class="aq-ver">v1.0</span><div class="aq-slogan">{esc(brand.slogan())}</div>'
        f'<div class="aq-sub aq-hdr-detail">{where}</div></div></div>',
        unsafe_allow_html=True)
    perm = pill("模拟盘自动交易中", "green") if _http.paper_orders_unlocked() else pill("模拟下单未授权", "gray")
    mock = (" " + pill("DeepSeek 测试替身模式", "orange")) if test_mode else ""
    with right:
        a, b = st.columns([4, 2], vertical_alignment="center")
        user = ""
        if current_user():
            user = (f'👤 <b>{esc(current_user())}</b> '
                    f'{pill(_auth.ROLES.get(role(), role()), "purple" if role() == "admin" else "gray")}'
                    f'{" " + pill("只读", "orange") if role() == "viewer" else ""} ')
        a.markdown(
            f'<div class="aq-hdr-right">{user}{perm}{mock} {pill(stt["label"], t)} {pill("真实资金交易：关闭", "gray")}'
            f'<div class="aq-sub aq-hdr-detail" title="{esc(stt.get("detail", ""))}">券商连接检查 {checked} UTC · '
            f'{esc(tr(stt.get("detail", ""))[:90])}</div></div>', unsafe_allow_html=True)
        with b:
            switcher()
            logout_button()


def job_signature() -> str:
    r = db.q1("SELECT COUNT(*) AS n, COALESCE(MAX(id),0) AS mx, "
              "COALESCE(GROUP_CONCAT(id || status || stage, '|'),'') AS s FROM "
              "(SELECT id,status,stage FROM jobs ORDER BY id DESC LIMIT 30)")
    return f"{r['n']}:{r['mx']}:{hash(r['s'])}"


def auto_refresh() -> None:
    """有活动任务时每 2 秒检查一次任务状态，变化时刷新页面（状态全部来自后端）。"""
    if jobs.active_count() == 0:
        st.session_state["job_sig"] = job_signature()
        return

    @st.fragment(run_every=2.0)
    def _watch():
        sig = job_signature()
        if st.session_state.get("job_sig") != sig:
            st.session_state["job_sig"] = sig
            st.rerun()
        n = jobs.active_count()
        st.caption(f"⏳ 后台任务进行中：{n} 个（页面自动刷新状态）")

    _watch()


def flash(key: str, kind: str, msg: str) -> None:
    st.session_state[f"flash::{key}"] = (kind, msg)


def show_flash(key: str) -> None:
    v = st.session_state.pop(f"flash::{key}", None)
    if v:
        kind, msg = v
        cls = {"ok": "aq-ok", "warn": "aq-warn", "err": "aq-err"}.get(kind, "aq-warn")
        st.markdown(f'<div class="{cls}">{esc(msg)}</div>', unsafe_allow_html=True)
