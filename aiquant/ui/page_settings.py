"""系统设置：密钥状态（掩码）、交易权限、端点审计、备份。"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from .. import config, db
from ..ai import deepseek
from ..data import http
from . import components as C
from .common import flash, show_flash


def render() -> None:
    st.markdown('<div class="aq-section"><span class="bar"></span>系统设置</div>', unsafe_allow_html=True)
    show_flash("set")
    c1, c2 = st.columns(2, gap="medium")
    with c1:
        with st.container(border=True):
            C.section("API 凭证（只从本地 .env 读取，仅显示掩码）")
            st.markdown(C.kv([
                ("行情Key", config.mask(config.secret("ALPACA_API_KEY_ID"))),
                ("Paper Key", config.mask(config.secret("ALPACA_PAPER_KEY_ID") or config.secret("ALPACA_API_KEY_ID"))),
                ("DeepSeek", deepseek.available()[1]),
                ("模型", config.DEEPSEEK_MODEL),
                (".env 路径", str(config.ROOT / ".env")),
            ]), unsafe_allow_html=True)
            st.caption("修改 .env 后请使用 stop.bat / start.bat 重启服务。密钥不会写入前端、日志或数据库。")
    with c2:
        with st.container(border=True):
            C.section("交易权限")
            st.markdown(C.kv([
                ("PAPER_ORDERS", "0（锁定）" if not http.paper_orders_unlocked() else "已解锁"),
                ("LIVE_ORDERS", "0（本项目永久不实现 Live 交易）"),
                ("Paper 端点", config.ALPACA_PAPER_URL),
                ("Live 端点", "api.alpaca.markets — 被 HTTP 层拒绝"),
                ("解锁条件", "代码常量 + 本地授权文件，二者同时满足（需用户未来单独授权）"),
            ]), unsafe_allow_html=True)
    with st.container(border=True):
        C.section("登录账号（外网访问需要登录）")
        st.markdown("账号、角色、登录记录和安全设置已移到顶部「账号 → 用户管理」。")
        st.page_link(st.session_state["_pages"]["users"], label="打开用户管理", icon="🛡️")
    with st.container(border=True):
        C.section("外部端点访问审计（不含密钥）")
        a = pd.DataFrame(db.q("SELECT ts,method,url,status FROM endpoint_audit ORDER BY id DESC LIMIT 500"))
        if not a.empty:
            live = a[a["url"].str.contains("//api.alpaca.markets")]
            orders = a[(a["method"] != "GET") & a["url"].str.contains("/orders")]
            st.markdown(f"Live 端点访问：**{len(live)}** 次 · 订单写请求：**{len(orders)}** 次")
        st.dataframe(a, use_container_width=True, hide_index=True, height=300)
    with st.container(border=True):
        C.section("备份")
        if st.button("立即备份数据库", key="set_backup"):
            from ..maintenance import backup
            p = backup()
            flash("set", "ok", f"已备份到 {p}")
            st.rerun()
