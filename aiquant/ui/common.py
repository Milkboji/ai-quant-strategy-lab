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
    update_banner()


def update_banner() -> None:
    """全站顶部：磁盘上有新代码待生效 / 安全重启进行中。管理员可发起安全重启。"""
    try:
        from .. import build, safe_restart
        pu, sr = build.pending_update(), safe_restart.state()
    except Exception:  # noqa: BLE001
        return
    if not pu and not sr:
        return
    from .login import effective_user, role
    admin = role() == "admin"
    if sr and sr.get("phase") in ("waiting", "restarting"):
        if sr["phase"] == "waiting":
            txt = (f"安全重启进行中：已暂停 {len(sr.get('paused') or [])} 个策略的新开仓，等待未完成订单结束"
                   f"（现在 {sr.get('open_orders', 0)} 个，最长等待 {sr.get('max_wait_min', 30):g} 分钟），之后备份数据库并重启。"
                   "守护进程每分钟检查一次。")
        else:
            txt = f"安全重启中：{sr.get('note') or ''}；系统正在重启，约 1 分钟后恢复，请稍后刷新页面。"
        c = st.columns([5, 1], vertical_alignment="center")
        c[0].markdown(f'<div class="aq-warn">🔄 {esc(txt)}</div>', unsafe_allow_html=True)
        if admin and sr["phase"] == "waiting" and c[1].button("取消安全重启", key="sr_cancel", use_container_width=True):
            try:
                safe_restart.cancel(actor=effective_user(), role=role())
            except Exception as e:  # noqa: BLE001
                st.toast(str(e))
            st.rerun()
        return
    cnt = pu.get("count")
    head = f"有更新待生效（{cnt} 个提交）" if cnt else "有更新待生效"
    txt = (f"{head}：磁盘上的新版本 {pu.get('disk_short')}（{pu.get('disk_subject') or ''}），"
           f"当前运行 {pu.get('running_short') or '未知版本'}。重启后生效。")
    c = st.columns([5, 1], vertical_alignment="center")
    c[0].markdown(f'<div class="aq-warn">⬆️ {esc(txt)}</div>', unsafe_allow_html=True)
    if admin:
        with c[1].popover("安全重启", use_container_width=True):
            cfg = safe_restart.get_config()
            st.markdown("**安全重启会依次：**\n1. 暂停所有策略的新开仓（持仓保留，照常按规则卖出）\n"
                        "2. 等所有未完成订单结束\n3. 备份数据库\n4. 停止并重新启动系统（外网约中断 1 分钟）\n"
                        "5. 恢复新开仓，并在动态里记录“已更新到 commit …”")
            mw = st.number_input("最长等待未完成订单（分钟）", min_value=1, max_value=240,
                                 value=int(cfg["max_wait_min"]), step=5, key="sr_wait",
                                 help="超过这个时间仍有未完成订单，也会继续重启；重启后系统会自动对账")
            if st.button("确认安全重启", key="sr_go", type="primary", use_container_width=True):
                try:
                    safe_restart.request(actor=effective_user(), role=role(), max_wait_min=float(mw))
                except Exception as e:  # noqa: BLE001
                    st.toast(str(e))
                st.rerun()


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
