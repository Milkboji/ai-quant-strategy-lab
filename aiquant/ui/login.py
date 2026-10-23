"""登录页、会话管理、只读模式（观察员）。

- 外网访问必须登录；本机访问默认免登录（视为管理员），可在用户管理 → 安全设置里改成本机也要登录。
- 每次页面运行都会复核会话：账号被停用、删除、改角色、重置密码或"退出所有设备"后，已登录的页面会立即退出。
- 管理员创建 / 重置的账号首次登录必须先修改密码。
- 观察员：所有按钮、开关、表单提交都被禁用（服务端忽略其操作），只能查看。
"""
from __future__ import annotations

import functools
import json

import streamlit as st
import streamlit.components.v1 as components

from .. import auth
from .components import esc

LOGIN_CSS = """
<style>
[data-testid="stHeader"], [data-testid="stSidebar"], [data-testid="stToolbar"]{display:none !important}
.block-container{padding-top:0 !important;max-width:1100px !important}
.aq-lg-wrap{min-height:8vh}
.aq-lg-hero{height:100%;min-height:520px;border-radius:20px;padding:44px 40px;color:#fff;
  background:radial-gradient(1200px 500px at -10% -20%,#60A5FA 0%,transparent 55%),
             radial-gradient(900px 600px at 120% 120%,#A78BFA 0%,transparent 55%),
             linear-gradient(135deg,#1D4ED8 0%,#6D28D9 100%);
  box-shadow:0 20px 50px rgba(37,99,235,.25);display:flex;flex-direction:column}
.aq-lg-logo{width:56px;height:56px;border-radius:15px;box-shadow:0 0 0 3px rgba(255,255,255,.28),0 8px 24px rgba(15,23,42,.25)}
.aq-lg-slogan{font-size:15px;font-weight:700;letter-spacing:.5px;color:#FDE68A;margin-top:2px}
.st-key-lang_login{display:flex;justify-content:flex-end;margin-top:14px}
.aq-lg-hero h1{color:#fff;font-size:30px;line-height:1.25;margin:22px 0 8px}
.aq-lg-hero .lead{opacity:.9;font-size:15px;line-height:1.7}
.aq-lg-feat{margin-top:26px;display:grid;gap:12px}
.aq-lg-feat div{background:rgba(255,255,255,.12);border:1px solid rgba(255,255,255,.22);border-radius:12px;
  padding:11px 14px;font-size:14px}
.aq-lg-foot{margin-top:auto;padding-top:26px;font-size:12px;opacity:.8}
.aq-lg-card{padding:8px 6px 0}
.aq-lg-card h2{margin:6px 0 2px;font-size:24px;color:#0F172A}
.aq-lg-card .sub{color:#64748B;font-size:13px;margin-bottom:14px}
.aq-lg-tip{color:#64748B;font-size:12px;line-height:1.7;margin-top:10px}
[data-testid="stForm"]{border:none !important;padding:0 !important}
.stTextInput input{height:42px !important;font-size:14px !important}
[data-testid="stFormSubmitButton"] button{height:44px !important;font-size:15px !important;font-weight:600 !important}
.stTextInput [data-baseweb="input"]{border:1px solid #D7DEE8 !important;border-radius:10px !important;background:#fff}
.stTextInput [data-baseweb="input"]:focus-within{border-color:#2563EB !important;box-shadow:0 0 0 3px rgba(37,99,235,.12)}
@media (max-width: 760px){.aq-lg-hero{min-height:auto;padding:20px 20px;border-radius:16px}
  .aq-lg-hero h1{font-size:22px;margin:12px 0 4px}.aq-lg-hero .lead,.aq-lg-feat,.aq-lg-foot{display:none}
  .aq-lg-logo{width:42px;height:42px}.aq-lg-wrap{min-height:0}.aq-lg-hero .aq-lg-slogan{font-size:13px}}
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


# ---------------------------------------------------------------------------
# 当前用户
# ---------------------------------------------------------------------------

def current_user() -> str | None:
    """已登录的账号名；本机免登录时为 None。"""
    return st.session_state.get("auth_user")


def effective_user() -> str:
    return current_user() or auth.LOCAL_USER


def role() -> str:
    return auth.role_of(current_user())


def is_admin() -> bool:
    return role() == "admin"


def read_only() -> bool:
    return role() == "viewer"


def _login(user: str, keep: bool) -> None:
    st.session_state["auth_user"] = user
    st.session_state["auth_gen"] = auth.user_gen(user)
    if keep:
        st.session_state["auth_set_cookie"] = auth.make_token(user)


def logout(all_devices: bool = False) -> None:
    u = current_user()
    if u and all_devices:
        auth.revoke_sessions(u, u)
    for k in ("auth_user", "auth_gen", "auth_set_cookie"):
        st.session_state.pop(k, None)
    st.session_state["auth_clear_cookie"] = True


def logout_button() -> None:
    if not current_user():
        return
    if st.button("退出登录", key="auth_logout", type="tertiary", icon=":material/logout:"):
        logout()
        st.rerun()


# ---------------------------------------------------------------------------
# 只读模式：观察员的按钮 / 开关 / 表单全部禁用，服务端也忽略其结果
# ---------------------------------------------------------------------------

_EXEMPT = ("auth_", "acct_", "force_pw_")   # 登录 / 退出 / 修改自己的密码 —— 观察员也可以用


def _ro(kw: dict | None = None) -> bool:
    k = str((kw or {}).get("key") or "")
    if k.startswith(_EXEMPT):
        return False
    try:
        return bool(st.session_state.get("_aq_readonly"))
    except Exception:  # noqa: BLE001
        return False


def _install_readonly_guard() -> None:
    if getattr(st, "_aq_ro_installed", False):
        return

    def wrap_button(fn):
        @functools.wraps(fn)
        def w(*a, **kw):
            if _ro(kw):
                kw["disabled"] = True
                fn(*a, **kw)
                return False
            return fn(*a, **kw)
        return w

    def wrap_bool(fn, value_pos: int = 1):
        @functools.wraps(fn)
        def w(*a, **kw):
            if _ro(kw):
                kw["disabled"] = True
                fn(*a, **kw)
                v = kw.get("value", a[value_pos] if len(a) > value_pos else False)
                return bool(v)
            return fn(*a, **kw)
        return w

    from streamlit.delta_generator import DeltaGenerator as DG
    for name in ("button", "form_submit_button"):
        setattr(st, name, wrap_button(getattr(st, name)))
        setattr(DG, name, wrap_button(getattr(DG, name)))
    for name in ("toggle", "checkbox"):
        setattr(st, name, wrap_bool(getattr(st, name)))
        setattr(DG, name, _dg_bool(getattr(DG, name)))
    st._aq_ro_installed = True


def _dg_bool(fn):
    @functools.wraps(fn)
    def w(self, *a, **kw):
        if _ro(kw):
            kw["disabled"] = True
            fn(self, *a, **kw)
            v = kw.get("value", a[1] if len(a) > 1 else False)
            return bool(v)
        return fn(self, *a, **kw)
    return w


# ---------------------------------------------------------------------------
# 登录门禁
# ---------------------------------------------------------------------------

def gate() -> bool:
    """返回 True = 可以显示系统；False = 已渲染登录 / 改密码 / 拒绝页面，调用方应 st.stop()。"""
    _install_readonly_guard()
    st.session_state["_aq_readonly"] = False   # 先关闭，确认身份后再按角色设置
    auth.ensure()
    info = auth.request_info(_headers())
    st.session_state["auth_info"] = info
    if st.session_state.pop("auth_clear_cookie", False):
        _set_cookie("", 0, info["https"])
    cf = auth.cf_access_problem(info)
    if cf:
        _denied("无法访问", cf)
        return False

    u = current_user()
    if u and not auth.session_valid(u, st.session_state.get("auth_gen")):
        logout()   # 被停用 / 删除 / 强制退出 / 改了密码或角色
        _set_cookie("", 0, info["https"])
        st.session_state["auth_notice"] = "登录状态已失效，请重新登录"
        u = None
    if not u:
        tu = auth.check_token(_cookies().get(auth.COOKIE))
        if tu:
            st.session_state["auth_user"] = tu
            st.session_state["auth_gen"] = auth.user_gen(tu)
            u = tu
    if not u and not auth.login_required(info):
        st.session_state["_aq_readonly"] = False
        return True                                  # 本机免登录 = 管理员
    if not u:
        _login_page(info)
        return False
    tok = st.session_state.pop("auth_set_cookie", None)
    if tok:
        _set_cookie(tok, int(auth.settings()["session_days"]) * 86400, info["https"])
    rec = auth.get_user(u) or {}
    if rec.get("must_change"):
        _change_password_page(u)
        return False
    st.session_state["_aq_readonly"] = rec.get("role") == "viewer"
    return True


def _denied(title: str, msg: str) -> None:
    from . import brand
    st.markdown(LOGIN_CSS + f'<div style="max-width:440px;margin:14vh auto 0;text-align:center">'
                f'{brand.mark_svg(56, "aq-lg-logo")}'
                f'<h2 style="margin:18px 0 6px">{esc(title)}</h2><div style="color:#64748B">{esc(msg)}</div></div>',
                unsafe_allow_html=True)


def _hero() -> None:
    from . import brand
    st.markdown(
        f'<div class="aq-lg-hero">{brand.mark_svg(56, "aq-lg-logo")}'
        f'<h1>AI Quant Strategy Lab</h1><div class="aq-lg-slogan">{esc(brand.slogan())}</div>'
        '<div class="lead">个人量化研究与模拟交易实验室。策略经过严格回测和多年历史检验，'
        '在模拟账户里自动运行、持续进化。</div>'
        '<div class="aq-lg-feat"><div>🧪　严格分区回测 · 计入全部交易成本</div>'
        '<div>🧬　冠军–挑战者自动进化 · 空仓时切换</div>'
        '<div>🎚️　交易节奏 · 布林带分析 · 长期体检</div></div>'
        '<div class="aq-lg-foot">仅连接模拟资金账户，不进行真实资金交易 · 不构成投资建议</div></div>',
        unsafe_allow_html=True)


def _lang_bar() -> None:
    from ..i18n import switcher
    _, r = st.columns([5, 1])
    with r:
        switcher("lang_login")


def _login_page(info: dict) -> None:
    st.markdown(LOGIN_CSS + '<div class="aq-lg-wrap"></div>', unsafe_allow_html=True)
    _lang_bar()
    if not auth.has_users():
        _denied("还没有设置登录账号",
                "为了安全，第一个账号只能在运行系统的电脑上创建：在那台电脑上打开 http://127.0.0.1:8501 → 账号 → 用户管理。")
        return
    left, right = st.columns([1.15, 1], gap="large", vertical_alignment="center")
    with left:
        _hero()
    with right:
        st.markdown('<div class="aq-lg-card"><h2>欢迎回来</h2><div class="sub">请使用管理员分配的账号登录</div></div>',
                    unsafe_allow_html=True)
        notice = st.session_state.pop("auth_notice", None)
        if notice:
            st.info(notice)
        with st.form("aq_login", border=False):
            user = st.text_input("用户名", key="login_user", autocomplete="username", placeholder="用户名")
            pw = st.text_input("密码", type="password", key="login_pw", autocomplete="current-password",
                               placeholder="密码")
            keep = st.checkbox(f"在这台设备上保持登录 {auth.settings()['session_days']} 天", value=True)
            ok = st.form_submit_button("登　录", type="primary", use_container_width=True)
        if ok:
            if not user or not pw:
                st.error("请输入用户名和密码")
            else:
                good, msg = auth.verify(user, pw, info["ip"])
                if good:
                    _login(user.strip(), keep)
                    st.rerun()
                else:
                    st.error(msg)
        st.markdown('<div class="aq-lg-tip">🔒 连续输错 5 次将锁定 15 分钟 · 忘记密码请联系管理员重置<br>'
                    f'访问来源 {esc(info["ip"] if info["remote"] else "本机")}'
                    f'{" · 已加密传输（HTTPS）" if info["https"] else ""}</div>', unsafe_allow_html=True)


def _change_password_page(u: str) -> None:
    st.markdown(LOGIN_CSS + '<div class="aq-lg-wrap"></div>', unsafe_allow_html=True)
    _lang_bar()
    _, mid, _ = st.columns([1, 1.2, 1])
    with mid:
        st.markdown('<div class="aq-lg-card"><h2>请先修改密码</h2>'
                    f'<div class="sub">账号 {esc(u)} 正在使用管理员设置的临时密码，修改后才能进入系统。</div></div>',
                    unsafe_allow_html=True)
        with st.form("aq_force_pw", border=False):
            p1 = st.text_input("新密码", type="password", autocomplete="new-password",
                               help="至少 10 位，同时包含字母和数字，不能包含用户名")
            p2 = st.text_input("再输入一次", type="password", autocomplete="new-password")
            ok = st.form_submit_button("保存并进入系统", type="primary", use_container_width=True)
        if ok:
            if p1 != p2:
                st.error("两次输入的密码不一致")
            else:
                try:
                    auth.set_password(u, p1, u)
                    _login(u, True)
                    st.rerun()
                except ValueError as e:
                    st.error(str(e))
        if st.button("退出登录", key="force_pw_logout", type="tertiary"):
            logout()
            st.rerun()
