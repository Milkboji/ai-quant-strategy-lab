"""进化记录：每次晋级、淘汰、拒绝的完整证据。"""
from __future__ import annotations

from datetime import timedelta, timezone

import pandas as pd
import streamlit as st

from .. import evolog
from . import components as C

BJ = timezone(timedelta(hours=8))
EVENT_TONE = {"晋级": "green", "淘汰": "gray", "拒绝": "red"}
TRIGGER_TONE = {"自动": "purple", "人工批准": "blue", "人工拒绝": "orange"}


def _bj(ts: str | None) -> str:
    if not ts:
        return "--"
    try:
        return pd.Timestamp(ts).tz_convert(BJ).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return str(ts)[:16]


def _score_cell(b: dict | None) -> str:
    if not b:
        return "--"
    sc = b.get("score")
    sc = "成交不足" if sc is None else (f"{sc:.2f}" if isinstance(sc, (int, float)) else str(sc))
    parts = [f"稳健分 {sc}"]
    if b.get("return_pct") is not None:
        parts.append(f"收益 {b['return_pct']:+.1f}%")
    if b.get("max_dd_pct") is not None:
        parts.append(f"回撤 {b['max_dd_pct']:.1f}%")
    segs = b.get("segments")
    if segs:
        parts.append("季度 " + " / ".join(f"{x:+.1f}%" for x in segs))
    return " · ".join(parts)


def _lines(items: list[tuple[str, str]]) -> str:
    """可换行的键值列表（长说明不被截断）。"""
    return "".join(f'<div class="aq-sub" style="margin:2px 0;overflow-wrap:anywhere"><b>{C.esc(k)}</b>：{C.esc(v)}</div>'
                   for k, v in items)


def _etf_exposed() -> bool:
    try:
        from .. import challenger, holdout
        return bool(holdout.exposure(challenger.etf_lineage()))
    except Exception:  # noqa: BLE001
        return False


def render() -> None:
    st.markdown('<div class="aq-title">进化记录</div><div class="aq-sub">每一次晋级、淘汰、拒绝都留下完整证据：'
                '旧版本 → 新版本、参数差异、季度稳健评分、留出检验、前向测试期间双方的收益和回撤，以及是自动还是人工决定的。</div>',
                unsafe_allow_html=True)
    rows_all = evolog.rows(500)
    c = st.columns([2, 2, 1])
    evs = c[0].multiselect("事件", list(evolog.EVENTS), default=list(evolog.EVENTS), key="el_ev")
    labels = sorted({(r["strategy_key"] or "", r["label"] or r["strategy_key"] or "--") for r in rows_all},
                    key=lambda x: x[1])
    keys = ["全部"] + [k for k, _ in labels if k]
    names = {k: n for k, n in labels}
    key = c[1].selectbox("策略", keys, key="el_key", format_func=lambda k: "全部策略" if k == "全部" else names.get(k, k))
    rows = [r for r in rows_all if r["event"] in evs and (key == "全部" or r["strategy_key"] == key)]
    c[2].metric("记录数", f"{len(rows)}")
    if not rows:
        st.info("还没有符合条件的记录。自动进化或挑战者有结论后，会自动出现在这里。")
        return
    for r in rows[:100]:
        _card(r)
    if len(rows) > 100:
        st.caption(f"只显示最近 100 条（共 {len(rows)} 条）")


def _card(r: dict) -> None:
    with st.container(border=True):
        pills = C.pill(r["event"], EVENT_TONE.get(r["event"], "gray")) + " " + \
            C.pill(r["trigger"], TRIGGER_TONE.get(r["trigger"], "gray"))
        if r["strategy_key"] == "etf" and _etf_exposed():
            pills += " " + C.pill("验证/测试数据已暴露", "orange")
        who = f" · {r['actor']}" if r["actor"] and r["actor"] != "系统" else ""
        st.markdown(f'<div class="aq-slothead"><div><span class="aq-slotname">{C.esc(r["label"] or r["strategy_key"] or "--")}</span> '
                    f'<span class="aq-meta">{_bj(r["ts"])} 北京时间{C.esc(who)}</span></div><div>{pills}</div></div>'
                    f'<div style="font-weight:600;margin:4px 0">{C.esc(evolog.describe(r))}</div>', unsafe_allow_html=True)
        cols = st.columns(3)
        sc = r.get("score") or {}
        with cols[0]:
            st.markdown("**季度稳健评分（训练集）**")
            st.markdown(_lines([("现役", _score_cell(sc.get("champion"))), ("挑战者", _score_cell(sc.get("challenger")))]),
                        unsafe_allow_html=True)
        with cols[1]:
            st.markdown("**留出 / 快速检验**")
            ho = r.get("holdout") or {}
            items = []
            h = ho.get("holdout") if "holdout" in ho or "fast" in ho else ho
            if h and h.get("kind"):
                items.append(("留出检验", f"{h['kind']} · {'通过' if h.get('passed') else '未通过'}"))
                if h.get("reason"):
                    items.append(("说明", h["reason"]))
            elif h and h.get("reason"):
                items.append(("留出检验", h["reason"]))
            f = ho.get("fast") if isinstance(ho, dict) else None
            if f:
                items.append(("快速检验", {"pass": "通过", "fail": "明显更差", "inconclusive": "证据不足"}.get(f.get("verdict"), "--")))
                if f.get("reason"):
                    items.append(("说明", f["reason"]))
            st.markdown(_lines(items or [("结果", "--")]), unsafe_allow_html=True)
        with cols[2]:
            st.markdown("**前向测试（同一时段对比）**")
            fw = r.get("forward") or {}
            ch, cm = fw.get("challenger"), fw.get("champion")
            if ch and cm:
                st.dataframe(pd.DataFrame([
                    {"": "挑战者", "收益": f"{ch.get('return_pct', 0):+.2f}%", "回撤": f"{ch.get('max_dd_pct', 0):.1f}%",
                     "成交": ch.get("trades")},
                    {"": "现役", "收益": f"{cm.get('return_pct', 0):+.2f}%", "回撤": f"{cm.get('max_dd_pct', 0):.1f}%",
                     "成交": cm.get("trades")}]), hide_index=True, use_container_width=True)
                st.caption(f"前向 {fw.get('days', '--')} / {fw.get('need_days', '--')} 天")
            else:
                st.caption("没有前向测试（直接由历史检验决定）" if r["event"] == "晋级" else "没有前向数据")
        if r.get("reason"):
            st.markdown(f'<div class="aq-sub">原因：{C.esc(r["reason"])}</div>', unsafe_allow_html=True)
        diff = r.get("diff_plain") or []
        with st.expander(f"参数差异（{len(diff)} 项）"):
            if diff:
                st.dataframe(pd.DataFrame([{"项目": a, "旧值": b, "新值": c} for a, b, c in diff]),
                             hide_index=True, use_container_width=True)
            if r.get("diff_raw"):
                st.markdown("原始规则 diff")
                st.code(r["diff_raw"], language="diff")
            if not diff and not r.get("diff_raw"):
                st.caption("没有可比较的规则（新版本未生成）")
