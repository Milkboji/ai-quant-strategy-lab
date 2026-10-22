"""数据管理：行情缓存、覆盖区间、数据版本、质量报告、只读补采。"""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone

import pandas as pd
import streamlit as st

from .. import actions, db, research
from ..data import market_data as md
from . import components as C
from .common import auto_refresh, flash, show_flash


def render() -> None:
    auto_refresh()
    st.markdown('<div class="aq-section"><span class="bar"></span>数据管理 <span class="aq-sub">Alpaca BTC/USD（us 场所）5分钟K线 · '
                'UTC · 本地聚合为 15m/30m/1h/4h/1d · 不补造虚假K线</span></div>', unsafe_allow_html=True)
    show_flash("data")
    c1, c2 = st.columns([1.2, 1], gap="medium")
    with c1:
        with st.container(border=True):
            C.section("本地行情缓存")
            s = md.cache_summary()
            st.markdown(C.kv([("来源", "Alpaca API v1beta3 crypto/us"), ("交易对", s["symbol"]), ("5分钟K线", f"{s['rows']:,} 根"),
                              ("最早", (s["first"] or "--")[:16]), ("最晚", (s["last"] or "--")[:16]),
                              ("最近更新", (s["last_update"] or {}).get("at", "--"))]), unsafe_allow_html=True)
            if s["coverage"]:
                st.dataframe(pd.DataFrame(s["coverage"], columns=["已采集开始(UTC)", "已采集结束(UTC)"]),
                             hide_index=True, use_container_width=True, height=150)
            t = md.cache_summary("TEST_SYNTHETIC")
            if t["rows"]:
                st.markdown(f'<div class="aq-warn">另有测试合成数据 {t["rows"]:,} 根（TEST_SYNTHETIC），仅用于自动化测试，'
                            '所有相关结果标记为测试数据，不进入正式研究。</div>', unsafe_allow_html=True)
    with c2:
        with st.container(border=True):
            C.section("只读历史补采")
            r = research.get_research()
            try:
                ws, we = research.resolve_window(r)
            except Exception:  # noqa: BLE001
                ws, we = datetime.now(timezone.utc) - timedelta(days=90), datetime.now(timezone.utc)
            d = st.columns(2)
            s0 = d[0].date_input("开始(UTC)", value=(ws - timedelta(days=md.DEFAULT_WARMUP_DAYS)).date(), key="d_s")
            e0 = d[1].date_input("结束(UTC)", value=min(we, datetime.now(timezone.utc)).date(), key="d_e")
            st.caption("只下载缓存中缺失的区间；已下载的数据不会重复下载。补采只调用 Alpaca 只读行情接口。")
            if st.button("从 Alpaca 补采历史数据", type="primary", key="d_fetch", use_container_width=True):
                jid, created = actions.submit_data_update(datetime.combine(s0, time(0), tzinfo=timezone.utc),
                                                          datetime.combine(e0, time(0), tzinfo=timezone.utc))
                flash("data", "ok", f"已提交补采任务 #{jid}" if created else f"补采任务 #{jid} 正在执行")
                st.rerun()
            j = db.q1("SELECT * FROM jobs WHERE kind='data_update' ORDER BY id DESC LIMIT 1")
            if j:
                st.caption(f"最近补采任务 #{j['id']}：{j['status']} · {j['stage']} {j['error'] or ''}")
            sp = research.get_split()
            st.markdown(C.kv([("验证集开始", sp["val_start"][:10]), ("测试集开始", sp["test_start"][:10]),
                              ("测试集结束", sp["test_end"][:10])]), unsafe_allow_html=True)
    with st.container(border=True):
        C.section("数据版本（不可变快照）")
        ds = db.q("SELECT * FROM datasets ORDER BY id DESC LIMIT 200")
        rows = []
        for x in ds:
            q = json.loads(x["quality_json"])
            rows.append({"ID": x["id"], "来源": x["source"], "场所": x["venue"], "开始(含预热)": x["start_utc"][:16],
                         "结束": x["end_utc"][:16], "K线": x["rows"], "覆盖率%": q.get("coverage_pct"),
                         "缺失5m": q.get("missing_5m_bars"), "最大缺口(分钟)": q.get("largest_gap_minutes"),
                         "质量": q.get("grade"), "采集时间": x["fetched_at"][:19], "SHA256": x["sha256"][:16],
                         "测试数据": bool(x["is_test"])})
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=300)
        st.caption("缺失的5分钟K线表示 Alpaca 在该时段无成交或未返回数据；聚合周期中不完整的最后一根K线不参与信号计算。")
