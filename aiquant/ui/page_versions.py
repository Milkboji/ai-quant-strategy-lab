"""策略版本中心：v1 → v2 → v3 谱系、版本对比、验证集与独立测试。"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from .. import actions, db, research
from ..strategy import registry
from ..strategy.schema import describe, validate
from . import components as C
from .common import auto_refresh, flash, show_flash


def _bt_summary(vid: int) -> dict:
    out = {}
    for split in ("训练集", "验证集", "独立测试"):
        b = research.latest_backtest_for_version(vid, include_test=True, kind_split=split)
        out[split] = b
    return out


def render() -> None:
    auto_refresh()
    st.markdown('<div class="aq-section"><span class="bar"></span>策略版本中心 <span class="aq-sub">每次修改/优化都会创建新版本，'
                '已冻结版本与已完成研究记录不可覆盖</span></div>', unsafe_allow_html=True)
    show_flash("ver")
    tst = research.test_set_status()
    sp = research.get_split()
    st.markdown(f'<div class="aq-banner"><span>数据分区（首次启动冻结）：训练集 &lt; {sp["val_start"][:10]} ≤ 验证集 &lt; '
                f'{sp["test_start"][:10]} ≤ 独立测试集 &lt; {sp["test_end"][:10]}</span>'
                f'<span>独立测试集状态：{C.pill(tst["label"], "green" if tst["ok"] else "orange")}</span>'
                f'<span>验证集每条谱系最多 {research.VALIDATION_RUN_LIMIT} 次</span></div>', unsafe_allow_html=True)
    vers = registry.all_versions()
    if not vers:
        st.info("还没有任何策略版本。")
        return
    rows = []
    for v in vers:
        s = _bt_summary(v["id"])
        def r(k, f="net_return_pct"):
            b = s[k]
            return b["metrics"].get(f) if b else None
        rows.append({"ID": v["id"], "谱系": v["lineage_id"], "策略位": v["slot_no"], "版本": f"v{v['version_no']}",
                     "名称": v["name"], "父版本": v["parent_id"], "来源": v["source"], "修改摘要": v["change_summary"],
                     "创建时间": v["created_at"][:19], "Schema哈希": v["schema_hash"][:12],
                     "训练收益%": r("训练集"), "训练回撤%": r("训练集", "max_drawdown_pct"), "验证收益%": r("验证集"),
                     "独立测试%": r("独立测试"),
                     "数据版本": (s["训练集"]["dataset_hash"][:10] if s["训练集"] else "--"),
                     "测试数据": bool(s["训练集"] and s["训练集"]["is_test"])})
    df = pd.DataFrame(rows)
    lineages = sorted(df["谱系"].unique())
    with st.container(border=True):
        C.section("版本谱系")
        for lin in lineages:
            sub = df[df["谱系"] == lin].sort_values("ID")
            chain = " → ".join(f"{r['版本']}({C.pct(r['训练收益%'])})" for _, r in sub.iterrows())
            st.markdown(f'<span class="aq-tag">{C.esc(lin)}</span> 策略位 {sub.iloc[0]["策略位"]} · '
                        f'{C.esc(sub.iloc[-1]["名称"])}：{C.esc(chain)}', unsafe_allow_html=True)
        st.dataframe(df, use_container_width=True, hide_index=True, height=300,
                     column_config={c: st.column_config.NumberColumn(format="%+.2f%%") for c in
                                    ("训练收益%", "训练回撤%", "验证收益%", "独立测试%")})
    c1, c2 = st.columns([1.3, 1], gap="medium")
    ids = [v["id"] for v in vers]
    lab = {v["id"]: f"#{v['id']} 策略位{v['slot_no']} {v['name']} v{v['version_no']}" for v in vers}
    with c1:
        with st.container(border=True):
            C.section("版本对比")
            cc = st.columns(2)
            a = cc[0].selectbox("版本 A", ids, index=min(1, len(ids) - 1), format_func=lab.get, key="va")
            b = cc[1].selectbox("版本 B", ids, index=0, format_func=lab.get, key="vb")
            va, vb = registry.get_version(a), registry.get_version(b)
            d = registry.diff(va["schema"], vb["schema"])
            if d:
                st.dataframe(pd.DataFrame(d, columns=["字段", lab[a], lab[b]]), hide_index=True, use_container_width=True)
            else:
                st.caption("规则完全相同")
            ba, bb = (research.latest_backtest_for_version(x, include_test=True) for x in (a, b))
            m = pd.DataFrame({
                "指标": ["净收益率%", "净利润", "最大回撤%", "胜率%", "Profit Factor", "交易次数", "总成本", "数据分区", "窗口"],
                lab[a]: _mcol(ba), lab[b]: _mcol(bb)})
            st.dataframe(m, hide_index=True, use_container_width=True)
            series = {}
            if ba:
                series[f"A v{va['version_no']}"] = (ba["equity"], C.BLUE, ba["metrics"]["initial_cash"])
            if bb:
                series[f"B v{vb['version_no']}"] = (bb["equity"], C.PURPLE, bb["metrics"]["initial_cash"])
            if series:
                if ba and bb and (ba["window_start"], ba["window_end"]) != (bb["window_start"], bb["window_end"]):
                    st.markdown('<div class="aq-warn">两个结果的回测窗口不同，收益不可直接比较。</div>', unsafe_allow_html=True)
                st.plotly_chart(C.equity_compare(series, 260), use_container_width=True, key="ver_eq")
    with c2:
        with st.container(border=True):
            C.section("版本操作")
            sel = st.selectbox("选择版本", ids, format_func=lab.get, key="vsel")
            v = registry.get_version(sel)
            obj, _ = validate(v["schema"])
            st.markdown(C.kv(list(describe(obj).items())), unsafe_allow_html=True)
            slot_no = v["slot_no"] or 1
            if st.button(f"设为策略位 {slot_no} 当前版本", key="v_setcur", use_container_width=True):
                registry.set_current(slot_no, sel)
                registry.save_draft(slot_no, draft_schema=v["schema"], parse_info={
                    "status": "ok", "errors": [], "warnings": [], "unresolved": [], "method": f"应用版本 v{v['version_no']}",
                    "text": v["nl_text"]})
                st.session_state[f"txtrev_{slot_no}"] = st.session_state.get(f"txtrev_{slot_no}", 0) + 1
                flash("ver", "ok", "已设为当前版本")
                st.rerun()
            if st.button("在训练窗口回测该版本", key="v_train", type="primary", use_container_width=True):
                jid, msg = actions.submit_backtest(slot_no, version_id=sel)
                flash("ver", "ok" if jid else "err", msg); st.rerun()
            if st.button("验证集检验（有限次数）", key="v_val", use_container_width=True):
                jid, msg = actions.submit_backtest(slot_no, version_id=sel, kind="validation")
                flash("ver", "ok" if jid else "err", msg); st.rerun()
            if st.button("运行独立最终测试（每版本一次）", key="warn_final", use_container_width=True,
                         disabled=not tst["ok"], help=None if tst["ok"] else tst["label"]):
                jid, msg = actions.submit_backtest(slot_no, version_id=sel, kind="final_test")
                flash("ver", "ok" if jid else "err", msg); st.rerun()
            if not tst["ok"]:
                st.markdown(f'<div class="aq-warn">{C.esc(tst["label"])}：不得伪造达标结果。</div>', unsafe_allow_html=True)
            with st.expander("Schema（JSON）"):
                st.json(v["schema"])
            with st.expander("自然语言原文"):
                st.write(v["nl_text"] or "（无）")


def _mcol(b):
    if not b:
        return ["--"] * 9
    m = b["metrics"]
    return [C.pct(m.get("net_return_pct")), C.usd(m.get("net_profit"), True), C.pct(m.get("max_drawdown_pct")),
            C.pct(m.get("win_rate_pct"), False, 1), C.num(m.get("profit_factor")), C.num(m.get("trades"), 0),
            C.usd(m.get("total_cost")), b["split_label"] + (" · 测试数据" if b["is_test"] else ""),
            f"{b['window_start'][:10]}→{b['window_end'][:10]}"]
