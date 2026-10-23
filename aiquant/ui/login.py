"""登录页（外网访问必须登录；本机访问默认免登录）。"""
from __future__ import annotations

import json

import streamlit as st
import streamlit.components.v1 as components

from .. import auth
from .components import esc

CSS = """
<style>
.aq-login{max-width:400px;margin:8vh auto 0;padding:30px 30px 18px;background:#fff;border:1px solid #E5E7EB;
  border-radius:16px;box-shadow:0 10px 30px rgba(15,23,42,.06)}
.aq-login h2{margin:10px 0 2px;font-size:22px;color:#0F172A}
.aq-login .sub{color:#64748B;font-size:13px;margin-bottom:6px}
.aq-login .logo{width:38px;height:38px;border-radius:10px;background:linear-gradient(135deg,#2563EB,#7C3AED)}
</style>
"""


def _headers() -> dict:
    try:
        return dict(st.context.headers)
    except Exception:  # noqa: BLE001
        return {}


def _cookies() -> dict:
    try:
        return dict(st.context.cookies)
    except Exception:  # noqa: BLE001
        return {}


def _set_cookie(value: str, max_age: int, secure: bool) -> None:
    attrs = f"path=/; max-age={max_age}; samesite=strict" + ("; secure" if secure else "")
    components.html(f"<script>window.parent.document.cookie = {json.dumps(auth.COOKIE + '=' + value + '; ' + attrs)};"
                    "</script>", height=0)


def current_user() -> str | None:
    return st.session_state.get("auth_user")


def logout_button() -> None:
    info = st.session_state.get("auth_info") or {}
    if not current_user():
        return
    if st.button(f"退出登录（{current_user()}）", key="auth_logout", type="tertiary"):
        st.session_state.pop("auth_user", None)
        st.session_state["auth_clear_cookie"] = True
        st.rerun()


def gate() -> bool:
    """返回 True = 可以显示系统；False = 已渲染登录 / 拒绝页面，调用方应 st.stop()。"""
    info = auth.request_info(_headers())
    st.session_state["auth_info"] = info
    if st.session_state.pop("auth_clear_cookie", False):
        _set_cookie("", 0, info["https"])
    cf = auth.cf_access_problem(info)
    if cf:
        st.markdown(CSS + f'<div class="aq-login"><div class="logo"></div><h2>无法访问</h2><div class="sub">{esc(cf)}</div></div>',
                    unsafe_allow_html=True)
        return False
    if not auth.login_required(info):
        return True
    if current_user():
        tok = st.session_state.pop("auth_set_cookie", None)
        if tok:
            _set_cookie(tok, auth.SESSION_DAYS * 86400, info["https"])
        return True
    u = auth.check_token(_cookies().get(auth.COOKIE))
    if u:
        st.session_state["auth_user"] = u
        return True
    st.markdown(CSS, unsafe_allow_html=True)
    if not auth.has_users():
        st.markdown('<div class="aq-login"><div class="logo"></div><h2>还没有设置登录账号</h2>'
                    '<div class="sub">为了安全，登录账号只能在运行系统的电脑上创建：在那台电脑上打开 '
                    'http://127.0.0.1:8501 → 研究与设置 → 系统设置 → 登录账号。</div></div>', unsafe_allow_html=True)
        return False
    _, mid, _ = st.columns([1, 1.25, 1])
    with mid:
        st.markdown('<div class="aq-login" style="margin-bottom:-18px;border-bottom:none;border-radius:16px 16px 0 0">'
                    '<div class="logo"></div><h2>AI Quant Strategy Lab</h2>'
                    '<div class="sub">请登录后继续 · 连续输错 5 次将锁定 15 分钟</div></div>', unsafe_allow_html=True)
        with st.form("aq_login", border=True):
            user = st.text_input("用户名", key="login_user", autocomplete="username")
            pw = st.text_input("密码", type="password", key="login_pw", autocomplete="current-password")
            keep = st.checkbox(f"在这台设备上保持登录 {auth.SESSION_DAYS} 天", value=True)
            ok = st.form_submit_button("登录", type="primary", use_container_width=True)
        if ok:
            good, msg = auth.verify(user, pw, info["ip"])
            if good:
                st.session_state["auth_user"] = user.strip()
                if keep:
                    st.session_state["auth_set_cookie"] = auth.make_token(user.strip())
                st.rerun()
            else:
                st.error(msg)
    return False


def settings_panel() -> None:
    """系统设置页里的“登录账号”管理（只允许本机或已登录用户修改）。"""
    info = st.session_state.get("auth_info") or {"remote": False}
    if info.get("remote") and not current_user():
        st.caption("请先登录。")
        return
    s = auth.settings()
    users = list(auth._load()["users"])
    st.markdown(f"已有账号：**{'、'.join(users) if users else '无'}** · 外网访问一律需要登录"
                + ("" if s["require_local"] else "；本机（127.0.0.1）访问免登录"))
    with st.form("auth_set", border=False):
        c = st.columns(3)
        u = c[0].text_input("用户名", value=current_user() or (users[0] if users else "admin"))
        p1 = c[1].text_input("新密码（至少 10 位，含字母和数字）", type="password")
        p2 = c[2].text_input("再输一次", type="password")
        if st.form_submit_button("保存账号 / 修改密码", type="primary"):
            if p1 != p2:
                st.error("两次输入的密码不一致")
            else:
                try:
                    auth.set_user(u, p1)
                    st.success(f"已保存账号 {u}（修改密码后，其他设备上的登录状态会失效）")
                except ValueError as e:
                    st.error(str(e))
    c = st.columns(2)
    rl = c[0].toggle("本机访问也要登录", value=bool(s["require_local"]), key="auth_rl")
    rc = c[1].toggle("外网访问必须先通过 Cloudflare 邮箱验证码", value=bool(s["require_cf_access"]), key="auth_rc",
                     help="开启后，没有经过 Cloudflare Access 验证的外网请求会被拒绝")
    em = st.text_input("允许的 Cloudflare 验证邮箱（逗号分隔，留空 = 只要通过验证即可）",
                       value=", ".join(s["cf_allowed_emails"]), key="auth_em")
    if st.button("保存登录设置", key="auth_save"):
        if rl and not users:
            st.error("请先创建账号，再开启本机登录")
        else:
            auth.update_settings(require_local=rl, require_cf_access=rc,
                                 cf_allowed_emails=[e.strip() for e in em.split(",") if e.strip()])
            st.success("已保存")
