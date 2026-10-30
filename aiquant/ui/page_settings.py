"""系统设置：密钥状态（掩码）、交易权限、通知、端点审计、备份。"""
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
    notify_panel()
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



def notify_panel() -> None:
    from .. import notify
    from .login import effective_user, role
    cfg = notify.get_config()
    stt = notify.channel_status()
    with st.container(border=True):
        C.section("通知（邮件 / Telegram / 企业微信 / 飞书）")
        st.caption("账号只写在本机 config/secrets.local（不进代码仓库），这里只显示掩码。所有发送都走系统唯一的对外出口："
                   "主机白名单 + 审计（下方「外部端点访问审计」可查，token 和密码不会被记录）。"
                   "发送失败的通知会保留，后台每分钟重试，最多 5 次。修改 secrets.local 后需要重启。"
                   "启用的通道并行发送、互不影响；在国内建议启用企业微信或飞书（Telegram 需要代理）。")
        cols = st.columns(2)
        new_ch = {}
        for i, (ch, name) in enumerate(notify.CHANNELS.items()):
            with cols[i % 2]:
                s_ = stt[ch]
                st.markdown(f"**{name}** " + (C.pill("已配置", "green") if s_["configured"] else C.pill("未配置", "gray")),
                            unsafe_allow_html=True)
                st.caption(s_["detail"])
                new_ch[ch] = st.toggle(f"启用{name}通知", value=bool(cfg[ch]), key=f"nt_{ch}",
                                       disabled=not s_["configured"])
                if st.button(f"发送测试{name}", key=f"nt_test_{ch}", disabled=not s_["configured"]):
                    try:
                        notify.send_test(ch, actor=effective_user(), role=role())
                        flash("set", "ok", f"{name}测试消息已发送")
                    except Exception as e:  # noqa: BLE001
                        flash("set", "err", f"{name}测试失败：{e}")
                    st.rerun()
        st.markdown("**通知哪些事件**")
        ev_cols = st.columns(4)
        new_ev = {}
        for i, (k, name) in enumerate(notify.EVENTS.items()):
            new_ev[k] = ev_cols[i % 4].checkbox(name, value=bool(cfg["events"].get(k, True)), key=f"nt_ev_{k}")
        c = st.columns([1, 1, 2])
        hour = c[0].number_input("每日摘要时间（北京时间，点）", min_value=0, max_value=23, value=int(cfg["daily_hour"]),
                                 step=1, key="nt_hour")
        if c[1].button("保存通知设置", key="nt_save", type="primary", use_container_width=True):
            try:
                notify.set_config(actor=effective_user(), role=role(), email=new_ch["email"],
                                  telegram=new_ch["telegram"], wecom=new_ch["wecom"], feishu=new_ch["feishu"],
                                  events=new_ev, daily_hour=int(hour))
                flash("set", "ok", "通知设置已保存")
            except Exception as e:  # noqa: BLE001
                flash("set", "err", str(e))
            st.rerun()
        with st.expander("secrets.local 配置示例"):
            st.code("NOTIFY_SMTP_HOST=smtp.qq.com\nNOTIFY_SMTP_PORT=465\nNOTIFY_SMTP_USER=你的邮箱@qq.com\n"
                    "NOTIFY_SMTP_PASSWORD=邮箱授权码（不是登录密码）\nNOTIFY_EMAIL_TO=收件人1@x.com,收件人2@y.com\n"
                    "NOTIFY_TELEGRAM_BOT_TOKEN=123456:ABC...\nNOTIFY_TELEGRAM_CHAT_ID=你的 chat id\n"
                    "# 企业微信群机器人（群设置 → 群机器人 → 添加 → 复制 Webhook 地址）：\n"
                    "NOTIFY_WECOM_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...\n"
                    "# 飞书群机器人（可选；开了签名校验再填 SECRET）：\n"
                    "NOTIFY_FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/...\n# NOTIFY_FEISHU_SECRET=...\n"
                    "# 不在内置白名单里的邮件服务商：\n# NOTIFY_SMTP_ALLOW_HOST=smtp.example.com", language="ini")
        rec = notify.recent(30)
        if rec:
            st.markdown("**最近的通知**")
            st.dataframe(pd.DataFrame([{"时间（UTC）": r["ts"][:16].replace("T", " "),
                                        "事件": notify.EVENTS.get(r["event"], "测试" if r["event"] == "test" else r["event"]),
                                        "通道": {"none": "（无可用通道）"}.get(r["channel"], notify.CHANNELS.get(r["channel"], r["channel"])),
                                        "标题": r["title"],
                                        "状态": {"sent": "已发送", "pending": "待重试", "failed": "失败", "skipped": "未发送"}.get(r["status"], r["status"]),
                                        "重试": r["attempts"], "错误": r["last_error"] or ""} for r in rec]),
                         hide_index=True, use_container_width=True, height=240)
