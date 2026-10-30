"""策略成绩单页面：每个部署「实际 / 回测 / 基准」三条曲线、前向运行天数和成交笔数、逐笔对比、偏差原因；点差模型。"""
from __future__ import annotations

from datetime import timedelta, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from .. import console, db, scorecard, spread_model
from . import components as C
from .common import flash, show_flash

BJ = timezone(timedelta(hours=8))
STATUS_TONE = {"一致": "green", "时间偏差": "orange", "漏单": "red", "多单": "purple"}


def _bj(ts: str | None) -> str:
    if not ts:
        return "--"
    try:
        return pd.Timestamp(ts).tz_convert(BJ).strftime("%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return str(ts)[:16]


def _curve(sc: dict) -> go.Figure:
    df = pd.DataFrame(sc["curve"], columns=["ts", "actual", "expected", "bench"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(BJ)
    fig = go.Figure()
    for col, name, color, dash in (("actual", "实际（模拟盘）", "#2563EB", None), ("expected", "回测（同一区间）", "#7C3AED", "dash"),
                                   ("bench", f"基准（{sc.get('symbol') or ''} 买入持有）", "#9CA3AF", "dot")):
        fig.add_trace(go.Scatter(x=df["ts"], y=df[col], mode="lines+markers" if len(df) < 15 else "lines", name=name,
                                 line=dict(width=1.8, color=color, dash=dash), hovertemplate="%{y:+.2f}%<extra></extra>"))
    fig.update_yaxes(ticksuffix="%")
    return C._layout(fig, 240, legend=True)


def _rows_table(sc: dict) -> pd.DataFrame:
    out = []
    for r in sc["rows"]:
        out.append({
            "结果": r["status"], "方向": "买入" if r["side"] == "buy" else "卖出",
            "回测（信号 / 成交，北京时间）": f"{_bj(r.get('bt_key'))} / {_bj(r.get('bt_ts'))}" if r.get("bt_ts") else "--",
            "模拟盘（北京时间）": _bj(r.get("paper_ts")),
            "时间差（分钟）": r.get("dt_min"),
            "回测价": r.get("bt_price"),
            "模拟盘价": r.get("paper_price"),
            "成交价偏差 bps": r.get("price_dev_bps"),
            "成本 实际 / 模型 bps": (f"{r['cost_act_bps']:.1f}{'（手续费为估计）' if r.get('fee_estimated') else ''} / "
                                f"{r['cost_model_bps']:.1f}") if r.get("cost_act_bps") is not None else "--",
            "原因分类": r.get("category") or "--",
        })
    return pd.DataFrame(out)


def _one(sc: dict) -> None:
    with st.container(border=True):
        if sc.get("error"):
            st.markdown(f"**#{sc['deployment_id']} {C.esc(sc.get('label') or '')}** "
                        + C.pill("成绩单生成失败", "gray"), unsafe_allow_html=True)
            st.caption(sc["error"])
            return
        tone = "gray" if sc["insufficient"] else ("green" if not sc["alerts"] else "orange")
        st.markdown(f'<div class="aq-slothead"><div><span class="aq-slotname">#{sc["deployment_id"]} {C.esc(sc["label"])}</span> '
                    f'<span class="aq-meta">{C.esc(sc.get("symbol") or "")} · 从 {_bj(sc["start"])} 起（北京时间）· '
                    f'{C.esc(sc.get("cost_basis_label") or "")}</span></div>'
                    f'<div>{C.pill(sc["verdict"], tone)}</div></div>', unsafe_allow_html=True)
        if sc["insufficient"]:
            th = sc.get("thresholds") or scorecard.DEFAULTS
            st.markdown(f'<div class="aq-warn">样本不足，暂不能下结论：前向运行 {sc["forward_days"]} {sc["day_unit"]}、模拟盘成交 '
                        f'{sc["paper_fills"]} 笔（需要至少 {th["min_days"]} {sc["day_unit"]}、{th["min_trades"]} 笔）。'
                        "下面的对比只作参考。</div>", unsafe_allow_html=True)
        cats = sc.get("categories") or {}
        st.markdown(C.kv([
            ("前向运行", f"{sc['forward_days']} {sc['day_unit']}"),
            ("成交笔数", f"模拟盘 {sc['paper_fills']} · 回测 {sc['bt_fills']}"),
            ("逐笔对比", f"一致 {sc['consistent']} · 时间偏差 {sc['matched'] - sc['consistent']} · 漏单 {sc['missed']} · 多单 {sc['extra']}"),
            ("累计收益", f"实际 {sc['return_actual_pct']:+.2f}% · 回测 {sc['return_expected_pct']:+.2f}% · "
                         f"基准 {sc['return_bench_pct']:+.2f}%（实际 − 回测 {sc['return_gap_pp']:+.2f} 个百分点）"),
            ("平均成本", (f"实际 {sc['avg_cost_act_bps']:.1f} bps · 模型 {sc['avg_cost_model_bps']:.1f} bps"
                         if sc.get("avg_cost_act_bps") is not None else "--")),
            ("原因分类", "；".join(f"{k} {v}" for k, v in cats.items()) or "--"),
        ]), unsafe_allow_html=True)
        st.plotly_chart(_curve(sc), use_container_width=True, key=f"sc_curve_{sc['deployment_id']}",
                        config={"displayModeBar": False})
        if sc["alerts"]:
            st.markdown("".join(f'<div class="aq-warn">{C.esc(a["text"])}</div>' for a in sc["alerts"][:6]),
                        unsafe_allow_html=True)
            if len(sc["alerts"]) > 6:
                st.caption(f"另有 {len(sc['alerts']) - 6} 条，见下方逐笔对比")
        if sc["rows"]:
            with st.expander(f"逐笔对比（{len(sc['rows'])} 行）"):
                st.dataframe(_rows_table(sc), hide_index=True, use_container_width=True)


def _scorecards(rl: str) -> None:
    last = scorecard.latest_all()
    run = db.get_setting(scorecard.RUN_KEY) or {}
    c = st.columns([3, 1])
    c[0].caption("每天北京时间早上 6 点后，用同一份行情、同一套成本和交易节奏，重新回测每个部署当前版本的实际运行区间，"
                 "和模拟盘逐笔对比。服务中断造成的漏单单独归为「中断导致」，不算策略偏差；偏差超过阈值时发通知。"
                 + (f" 上次生成：{_bj(run.get('at'))}（北京时间）。" if run.get("at") else ""))
    if c[1].button("立即重新生成", key="sc_run", use_container_width=True, disabled=rl not in console.EDIT_ROLES):
        try:
            console.require_role(rl)
            with st.spinner("正在重新回测并逐笔对比…"):
                scorecard.run_all()
            flash("sc", "ok", "成绩单已更新")
        except Exception as e:  # noqa: BLE001
            flash("sc", "err", str(e))
        st.rerun()
    show_flash("sc")
    if not last:
        st.info("还没有成绩单：每天北京时间早上 6 点后自动生成，也可以点「立即重新生成」。")
        return
    for did in sorted(last):
        _one(last[did])


def _spread(rl: str) -> None:
    cur = spread_model.current()
    st.caption("用 Alpaca 历史报价按时段估计真实点差（BTC/USD 按 24 个 UTC 小时；美股取 09:35 开盘点差和盘中点差），"
               "替换回测里的固定代理值。后台每轮最多请求 10 次，美股开盘前后暂停。切换需要管理员确认；"
               "切换前生成的回测结果不重算、不覆盖，标为「旧口径」。")
    st.markdown(C.kv([("当前口径", spread_model.basis_label(spread_model.current_name())
                       + (f"（{cur['enabled_at'][:16].replace('T', ' ')} UTC 由 {cur.get('enabled_by') or '--'} 切换）" if cur else "")),
                      ("采样进度", (lambda p: f"已采 {p['ok']} 个样本，待采 {p['left']} 个，失败 {p['failed']} 个；品种："
                                   + "、".join(p["symbols"]))(spread_model.progress()))]), unsafe_allow_html=True)
    rows = []
    for sym in spread_model.symbols():
        stt = spread_model.bucket_stats(sym)
        e = spread_model.estimate(sym)
        if not stt:
            rows.append({"品种": sym, "样本天数": 0, "中位点差（bps）": "--", "状态": "还没有样本"})
            continue
        med = sorted(v["median_bps"] for v in stt.values())
        rows.append({"品种": sym, "样本天数": min(v["days"] for v in stt.values()),
                     "中位点差（bps）": f"{med[0]:.2f} ~ {med[-1]:.2f}（{len(stt)} 个时段）",
                     "状态": "可用" if e else f"样本不足（每个时段需要 {spread_model.MIN_DAYS} 天）"})
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    c = st.columns(2)
    if c[0].button("生成新旧口径对比报告", key="sp_report", use_container_width=True, disabled=rl not in console.EDIT_ROLES):
        try:
            console.require_role(rl)
            from .login import effective_user
            with st.spinner("正在用旧口径和新口径分别回测每个部署的版本（训练期）…"):
                spread_model.build_report(effective_user())
            flash("sp", "ok", "对比报告已生成")
        except Exception as e:  # noqa: BLE001
            flash("sp", "err", str(e))
        st.rerun()
    rep = spread_model.latest_report()
    if rep:
        cand = rep["candidate"]
        same = cur and cur.get("hash") == cand["hash"]
        label = f"切换到实测点差 v{cand['version']}（管理员）" if not same else f"已在使用实测点差 v{cand['version']}"
        if c[1].button(label, key="sp_enable", type="primary", use_container_width=True,
                       disabled=bool(same) or rl != "admin"):
            try:
                from .login import effective_user
                flash("sp", "ok", spread_model.enable(rep["id"], actor=effective_user(), role=rl))
            except Exception as e:  # noqa: BLE001
                flash("sp", "err", str(e))
            st.rerun()
    show_flash("sp")
    if rep:
        with st.expander(f"对比报告 #{rep['id']}（{rep['created_at'][:16].replace('T', ' ')} UTC）", expanded=not cur):
            st.markdown(rep["markdown"])
            st.download_button("下载报告（Markdown）", rep["markdown"], file_name=f"点差模型对比报告_{rep['id']}.md",
                               mime="text/markdown", key="sp_dl")


def render() -> None:
    from .login import role
    rl = role()
    C.section("策略成绩单")
    t1, t2 = st.tabs(["成绩单", "点差模型"])
    with t1:
        _scorecards(rl)
    with t2:
        _spread(rl)
