"""我的账号：资料、修改密码、退出所有设备、我的登录记录。"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from .. import auth
from . import components as C
from .login import current_user, logout


def _bj(ts: str | None) -> str:
    if not ts:
        return "--"
    return (pd.Timestamp(ts) + pd.Timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")


def render() -> None:
    C.section("我的账号")
    u = current_user()
    if not u:
        st.info("你正在本机访问（免登录，拥有管理员权限）。外网访问时需要用账号登录；账号在「用户管理」里创建。")
        return
    rec = auth.get_user(u) or {}
    with st.container(border=True):
        c = st.columns(4)
        c[0].metric("账号", u)
        c[1].metric("角色", rec.get("role_cn", "--"))
        c[2].metric("上次登录", _bj(rec.get("last_login_at")))
        c[3].metric("密码修改于", _bj(rec.get("pw_changed_at")))
        st.caption(auth.ROLE_DESC.get(rec.get("role"), ""))

    l, r = st.columns(2, gap="medium")
    with l:
        with st.container(border=True):
            C.section("修改密码")
            with st.form("acct_pw", border=False):
                old = st.text_input("当前密码", type="password", autocomplete="current-password")
                p1 = st.text_input("新密码", type="password", autocomplete="new-password",
                                   help="至少 10 位，同时包含字母和数字，不能包含用户名")
                p2 = st.text_input("再输入一次", type="password", autocomplete="new-password")
                ok = st.form_submit_button("保存新密码", type="primary", key="acct_pw_submit")
            if ok:
                info = st.session_state.get("auth_info") or {}
                good, msg = auth.verify(u, old, info.get("ip", ""))
                if not good:
                    st.error("当前密码不正确" if "不正确" in msg else msg)
                elif p1 != p2:
                    st.error("两次输入的新密码不一致")
                else:
                    try:
                        auth.set_password(u, p1, u)
                        st.session_state["auth_gen"] = auth.user_gen(u)
                        st.session_state["auth_set_cookie"] = auth.make_token(u)
                        st.success("密码已修改。其他设备上的登录已全部失效。")
                    except ValueError as e:
                        st.error(str(e))
    with r:
        with st.container(border=True):
            C.section("登录安全")
            st.markdown("如果你在别人的电脑上登录过，或者怀疑密码泄露，可以让所有设备（包括这台）立即退出。")
            if st.button("退出所有设备", key="acct_revoke_all", icon=":material/devices_off:"):
                logout(all_devices=True)
                st.rerun()
            st.markdown("**我最近的登录记录**")
            rows = auth.login_log(20, u)
            if rows:
                st.dataframe(pd.DataFrame([{"时间": _bj(x["ts"]), "来源 IP": x["ip"], "结果": "成功" if x["ok"] else
                                            "失败：" + (x["note"] or "")} for x in rows]),
                             hide_index=True, use_container_width=True, height=260)
