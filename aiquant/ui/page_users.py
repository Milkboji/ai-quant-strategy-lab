"""用户管理（仅管理员）：账号列表、新建、改角色、重置密码、停用 / 启用、强制退出、删除；登录记录；操作审计；安全设置。"""
from __future__ import annotations

import time

import pandas as pd
import streamlit as st

from .. import auth
from . import components as C
from .login import effective_user, is_admin


def _bj(ts: str | None) -> str:
    if not ts:
        return "--"
    return (pd.Timestamp(ts) + pd.Timedelta(hours=8)).strftime("%m-%d %H:%M")


def _ip() -> str:
    return (st.session_state.get("auth_info") or {}).get("ip", "")


def _show_secret(user: str, pw: str, what: str) -> None:
    st.session_state["users_secret"] = {"user": user, "pw": pw, "what": what, "at": time.time()}


def render() -> None:
    if not is_admin():
        st.error("只有管理员可以管理用户。")
        return
    me = effective_user()
    C.section("用户管理", right=C.pill("仅管理员可见", "purple"))

    users = auth.list_users()
    since = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=24)
    fails = [x for x in auth.login_log(500) if not x["ok"] and pd.Timestamp(x["ts"]) >= since]
    c = st.columns(4)
    c[0].metric("账号总数", len(users))
    c[1].metric("管理员", sum(1 for u in users if u["role"] == "admin" and not u["disabled"]))
    c[2].metric("已停用", sum(1 for u in users if u["disabled"]))
    c[3].metric("24 小时内登录失败", len(fails))

    sec = st.session_state.get("users_secret")
    if sec and time.time() - sec["at"] < 600:
        with st.container(border=True):
            st.markdown(f"**{sec['what']}：{C.esc(sec['user'])}** — 临时密码只显示这一次，请复制后安全地交给对方。"
                        "对方首次登录必须修改密码。", unsafe_allow_html=True)
            st.code(sec["pw"], language=None)
            if st.button("我已保存，隐藏密码", key="users_hide_secret"):
                st.session_state.pop("users_secret", None)
                st.rerun()

    t_users, t_new, t_log, t_audit, t_sec = st.tabs(["账号列表", "新建账号", "登录记录", "操作审计", "安全设置"])

    with t_users:
        if not users:
            st.info("还没有账号。请在「新建账号」里创建第一个管理员。")
        else:
            st.dataframe(pd.DataFrame([{
                "账号": u["username"], "显示名": u["display_name"], "角色": u["role_cn"],
                "状态": "⛔ 已停用" if u["disabled"] else ("🔑 待改密码" if u["must_change"] else "✅ 正常"),
                "上次登录": _bj(u["last_login_at"]), "登录 IP": u["last_login_ip"] or "--",
                "创建": f"{_bj(u['created_at'])} · {u['created_by'] or '--'}", "备注": u["note"] or ""}
                for u in users]), hide_index=True, use_container_width=True)
            st.markdown("**管理账号**")
            names = [u["username"] for u in users]
            sel = st.selectbox("选择账号", names, key="users_sel",
                               format_func=lambda n: f"{n}（{next(u['role_cn'] for u in users if u['username'] == n)}）")
            u = next(x for x in users if x["username"] == sel)
            box = st.container(border=True)
            with box:
                a, b = st.columns(2, gap="medium")
                with a:
                    roles = list(auth.ROLES)
                    nr = st.selectbox("角色", roles, index=roles.index(u["role"]), key=f"users_role_{sel}",
                                      format_func=lambda r: f"{auth.ROLES[r]} — {auth.ROLE_DESC[r]}")
                    if st.button("保存角色", key=f"users_saverole_{sel}", disabled=nr == u["role"]):
                        _do(lambda: auth.set_role(sel, nr, me), f"已把 {sel} 改为{auth.ROLES[nr]}，该账号需重新登录")
                    dn = st.text_input("显示名", value=u["display_name"] or "", key=f"users_dn_{sel}")
                    nt = st.text_input("备注", value=u["note"] or "", key=f"users_note_{sel}")
                    if st.button("保存资料", key=f"users_saveprof_{sel}"):
                        _do(lambda: auth.update_profile(sel, me, dn, nt), "已保存")
                with b:
                    if st.button("重置密码（生成临时密码）", key=f"users_reset_{sel}", icon=":material/key:"):
                        try:
                            pw = auth.reset_password(sel, me)
                            _show_secret(sel, pw, "已重置密码")
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))
                    if st.button("强制退出所有设备", key=f"users_revoke_{sel}", icon=":material/logout:"):
                        _do(lambda: auth.revoke_sessions(sel, me), f"{sel} 的所有登录已失效")
                    if u["disabled"]:
                        if st.button("启用账号", key=f"users_enable_{sel}", icon=":material/check_circle:"):
                            _do(lambda: auth.set_disabled(sel, False, me), f"已启用 {sel}")
                    elif st.button("停用账号", key=f"warn_users_disable_{sel}", icon=":material/block:"):
                        _do(lambda: auth.set_disabled(sel, True, me), f"已停用 {sel}，该账号立即无法使用")
                    conf = st.checkbox(f"确认删除 {sel}（不可恢复）", key=f"users_delconf_{sel}")
                    if st.button("删除账号", key=f"danger_users_del_{sel}", disabled=not conf, icon=":material/delete:"):
                        _do(lambda: auth.delete_user(sel, me), f"已删除 {sel}")
            flash = st.session_state.pop("users_flash", None)
            if flash:
                (st.success if flash[0] else st.error)(flash[1])

    with t_new:
        with st.form("users_new", border=False):
            c1, c2, c3 = st.columns(3)
            nu = c1.text_input("用户名", help="2–32 位：字母、数字、中文、下划线、点、横线")
            nd = c2.text_input("显示名（可选）")
            nr = c3.selectbox("角色", list(auth.ROLES), index=2 if users else 0,
                              format_func=lambda r: auth.ROLES[r])
            mode = st.radio("密码", ["自动生成临时密码（推荐）", "手动设置"], horizontal=True)
            pw = st.text_input("手动设置的密码", type="password", autocomplete="new-password",
                               help="选择「手动设置」时填写：至少 10 位，含字母和数字")
            must = st.checkbox("首次登录必须修改密码", value=True)
            note = st.text_input("备注（可选）")
            st.caption("角色说明：" + "；".join(f"{auth.ROLES[k]}：{v}" for k, v in auth.ROLE_DESC.items()))
            ok = st.form_submit_button("创建账号", type="primary")
        if ok:
            try:
                manual = mode == "手动设置"
                real = auth.create_user(nu, pw if manual else None, nr, me, nd, must_change=must or not manual,
                                        note=note)
                if not manual:
                    _show_secret(nu.strip(), real, "已创建账号")
                st.session_state["users_flash"] = (True, f"已创建账号 {nu.strip()}（{auth.ROLES[nr]}）")
                st.rerun()
            except ValueError as e:
                st.error(str(e))

    with t_log:
        rows = auth.login_log(300)
        if rows:
            df = pd.DataFrame([{"时间": _bj(x["ts"]), "账号": x["username"], "来源 IP": x["ip"],
                                "结果": "✅ 成功" if x["ok"] else "❌ 失败", "说明": x["note"] or ""} for x in rows])
            only_fail = st.toggle("只看失败", key="users_onlyfail")
            st.dataframe(df[df["结果"].str.contains("失败")] if only_fail else df, hide_index=True,
                         use_container_width=True, height=420)
        else:
            st.caption("还没有登录记录。")

    with t_audit:
        rows = auth.audit_log(300)
        if rows:
            st.dataframe(pd.DataFrame([{"时间": _bj(x["ts"]), "操作人": x["actor"], "操作": x["action"],
                                        "对象": x["target"], "说明": x["detail"]} for x in rows]),
                         hide_index=True, use_container_width=True, height=420)
        else:
            st.caption("还没有管理操作记录。")

    with t_sec:
        s = auth.settings()
        rl = st.toggle("本机访问也要登录", value=bool(s["require_local"]), key="users_rl",
                       help="默认本机（127.0.0.1）免登录并拥有管理员权限，方便桌面图标直接打开")
        days = st.select_slider("保持登录天数", options=[1, 3, 7, 14, 30], value=int(s["session_days"]),
                                key="users_days")
        rc = st.toggle("外网访问必须先通过 Cloudflare Access 邮箱验证码", value=bool(s["require_cf_access"]),
                       key="users_rc", help="需要先在 Cloudflare Zero Trust 开通 Access（需要绑定付款方式）")
        em = st.text_input("允许的 Cloudflare 验证邮箱（逗号分隔，留空 = 通过验证即可）",
                           value=", ".join(s["cf_allowed_emails"]), key="users_em")
        if st.button("保存安全设置", key="users_savesec", type="primary"):
            if rl and not users:
                st.error("请先创建账号，再开启本机登录")
            else:
                auth.update_settings(me, require_local=rl, require_cf_access=rc, session_days=int(days),
                                     cf_allowed_emails=[e.strip() for e in em.split(",") if e.strip()])
                st.success("已保存")
        st.markdown(
            "**当前防护**\n"
            "- 外网地址先经过 Cloudflare 真人验证（Managed Challenge），再到本系统登录页\n"
            f"- 密码 PBKDF2-SHA256 {auth.ITER:,} 次迭代 + 独立随机盐；不保存明文\n"
            f"- 同一账号或 IP 15 分钟内输错 {auth.MAX_FAILS} 次锁定 15 分钟；停用账号无法登录\n"
            "- 改密码、重置、停用、改角色、强制退出都会让该账号已有登录立即失效\n"
            "- 观察员所有按钮和开关在服务端被禁用（只读）")


def _do(fn, ok_msg: str) -> None:
    try:
        fn()
        st.session_state["users_flash"] = (True, ok_msg)
    except ValueError as e:
        st.session_state["users_flash"] = (False, str(e))
    st.rerun()
