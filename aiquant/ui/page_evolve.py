"""策略进化：定期自动搜索更稳健的策略版本，通过检验后自动替换模拟盘中的旧版本。"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from .. import db, evolve, jobs, research
from ..data import market_data as md
from ..strategy import registry
from . import components as C
from .common import flash, show_flash

DECISION_ICON = {"替换": "🟢 替换", "挑战者上线": "🟣 挑战者上线", "保留": "⚪ 保留", "等待新数据": "🕓 等待新数据", "待人工确认": "🟡 待确认",
                 "出错": "❗ 出错"}


def _bj(ts: str | None) -> str:
    if not ts:
        return "--"
    return (pd.Timestamp(md.to_utc(ts)) + pd.Timedelta(hours=8)).strftime("%m-%d %H:%M")


def render() -> None:
    evolve.ensure_tables()
    C.section("策略自动进化", right=C.pill("自动优化", "purple"))
    st.markdown(
        '<div class="aq-sub">系统会按设定周期，为每个正在模拟盘运行的策略自动寻找更稳健的版本：</div>'
        '<ol class="aq-sub" style="margin-top:4px">'
        '<li><b>生成候选</b>：在当前策略附近微调参数（均线周期、阈值、止损止盈、K线周期），两轮搜索，最多 120 个；可加 1 个 DeepSeek 建议。</li>'
        '<li><b>稳健性评分</b>：只用训练集（一年），切成 4 个季度。稳健分 = 季度平均收益 − 0.5×季度收益波动 − 0.25×最大回撤。'
        '成交太少的候选直接淘汰。只有比当前版本高出至少 1 分才进入下一步。</li>'
        '<li><b>留出检验</b>：在候选从没“见过”的数据上和当前版本比较（优先用最新行情，不够时用验证集，每条策略最多 3 次）。'
        '候选必须有成交，收益不比当前差、回撤不明显更深。独立测试集永远不参与。</li>'
        '<li><b>自动替换</b>：通过后冻结为新版本；模拟盘在<b>空仓时</b>切换，有持仓时等卖出后再切换。每一步都写入日志。</li>'
        '<li><b>挑战者模拟运行</b>：历史留出数据不够下结论时，候选不会被丢掉，而是作为“挑战者”从登记时刻起在实时行情上与现役版本并行运行，'
        '每小时评估一次；跑满最短天数且不差于现役 → 自动晋级并部署到模拟盘；明显更差 → 淘汰。美股 ETF 择时的参数也用同样方式进化。</li>'
        '</ol>'
        '<div class="aq-warn">提醒：历史上更稳健 ≠ 未来一定赚钱。这个流程的作用是持续淘汰明显更差的版本，'
        '并且每次替换都有可查的证据；它不能保证盈利。</div>', unsafe_allow_html=True)
    show_flash("evolve")

    cfg = evolve.get_config()
    with st.container(border=True):
        st.markdown("**设置**")
        c = st.columns([1, 1, 1, 1, 1.2])
        en = c[0].toggle("启用定时进化", value=bool(cfg["enabled"]), key="ev_en")
        opts = [1, 3, 7, 14, 30]
        iv = c[1].selectbox("间隔", opts, index=opts.index(int(cfg["interval_days"])) if int(cfg["interval_days"]) in opts else 2,
                            format_func=lambda d: f"每 {d} 天", key="ev_iv")
        ap = c[2].toggle("通过检验后自动替换", value=bool(cfg["auto_apply"]), key="ev_ap",
                         help="关闭后只给出建议（待确认），不改动模拟盘")
        ds = c[3].toggle("加入 DeepSeek 候选", value=bool(cfg["use_deepseek"]), key="ev_ds",
                         help="DeepSeek 只读取训练集摘要，提出 1 个候选，同样要通过全部检验")
        c2 = st.columns([1.2, 1, 1, 2])
        pl = c2[0].toggle("自动暂停长期亏损的策略", value=bool(cfg["pause_losers"]), key="ev_pl",
                          help="暂停后不再买入，已有持仓仍按卖出规则管理；进化出不亏的新版本后自动恢复")
        tl = c2[1].number_input("训练集一年亏损达到（%）", value=float(cfg["pause_train_loss_pct"]), max_value=0.0,
                                min_value=-90.0, step=5.0, key="ev_tl")
        ll = c2[2].number_input("模拟盘亏损达到额度的（%）", value=float(cfg["pause_live_loss_pct"]), max_value=0.0,
                                min_value=-90.0, step=5.0, key="ev_ll")
        c2[3].markdown('<div class="aq-sub" style="margin-top:28px">任一条件满足即暂停新开仓。你也可以在模拟盘页面手动恢复，'
                       '手动恢复的同一版本不会再被自动暂停。</div>', unsafe_allow_html=True)
        if c[4].button("保存设置", key="ev_save", use_container_width=True):
            evolve.set_config(enabled=en, interval_days=int(iv), auto_apply=ap, use_deepseek=ds,
                              pause_losers=pl, pause_train_loss_pct=float(tl), pause_live_loss_pct=float(ll))
            flash("evolve", "success", "设置已保存")
            st.rerun()
    status_panel()
    challengers_panel()
    results_panel()


@st.fragment(run_every=5.0)
def status_panel() -> None:
    last = db.get_setting("evolve_last_run")
    nxt = evolve.next_run_at()
    job = db.q1("SELECT * FROM jobs WHERE kind='evolve' AND is_test=0 ORDER BY id DESC LIMIT 1")
    running = job and job["status"] in ("queued", "running")
    c = st.columns([1, 1, 1, 1.3])
    c[0].metric("上次进化", _bj(last["at"]) if last else "尚未运行")
    c[1].metric("下次进化", "进行中" if running else (_bj(nxt.isoformat()) if nxt else "已关闭"))
    c[2].metric("上次替换", f"{last.get('replaced', 0)} / {last.get('strategies', 0)} 个策略" if last and "replaced" in last else "--")
    with c[3]:
        if running:
            st.info(f"⏳ 正在进化：{job['stage'] or '排队中'}")
        elif st.button("🧬 立即进化一次", key="ev_now", type="primary", use_container_width=True):
            jid, created = evolve.submit("手动")
            flash("evolve", "info", f"已提交进化任务 #{jid}，约需 2~5 分钟")
            st.rerun()
        if job and job["status"] == "failed":
            st.error(f"上次进化失败：{job['error']}")
    pend = []
    for d in db.q("SELECT * FROM paper_deployments WHERE status IN ('running','paused_entries')"):
        p = json.loads(d["config_json"]).get("pending_switch")
        if p:
            v = registry.get_version(p["version_id"])
            pend.append({"部署": f"#{d['id']}", "策略": json.loads(d["config_json"]).get("label"),
                         "新版本": registry.label(v), "提交时间": _bj(p["at"]), "说明": "有持仓，等卖出后切换"})
    paused = []
    for d in db.q("SELECT * FROM paper_deployments WHERE status='paused_entries'"):
        cfg_d = json.loads(d["config_json"])
        a = cfg_d.get("auto_pause")
        paused.append({"部署": f"#{d['id']}", "策略": cfg_d.get("label"),
                       "暂停方式": "自动（长期亏损）" if a else "手动 / 对账保护",
                       "原因": a["reason"] if a else "--", "暂停时间": _bj(a["at"]) if a else "--"})
    if paused:
        st.markdown("**已暂停新开仓的策略**（不再买入；已有持仓仍按规则卖出）")
        st.dataframe(pd.DataFrame(paused), hide_index=True, use_container_width=True)
    if pend:
        st.markdown("**等待切换**")
        st.dataframe(pd.DataFrame(pend), hide_index=True, use_container_width=True)


def _fmt(b: dict | None) -> str:
    if not b:
        return "--"
    sc = "成交不足" if b.get("score") is None else f"{b['score']:.2f}"
    return f"稳健分 {sc} · 收益 {b['return_pct']:+.1f}% · 回撤 {b['max_dd_pct']:.1f}% · {b['trades']} 笔"


def results_panel() -> None:
    rs = evolve.runs(20)
    if not rs:
        st.caption("还没有进化记录。点击『立即进化一次』或等待定时触发。")
        return
    labels = {r["id"]: f"第 {r['id']} 轮 · {_bj(r['started_at'])} · {r['trigger']} · "
                       f"{'进行中' if r['status'] == 'running' else '完成' if r['status'] == 'done' else r['status']}"
              for r in rs}
    rid = st.selectbox("查看进化记录", list(labels), format_func=lambda k: labels[k], key="ev_run")
    rows = evolve.results_of(rid)
    if not rows:
        st.caption("这一轮还没有结果。")
        return
    tbl = []
    for r in rows:
        nv = registry.get_version(r["new_version_id"]) if r["new_version_id"] else None
        tbl.append({"策略": r["label"], "决定": DECISION_ICON.get(r["decision"], r["decision"]),
                    "测试候选": r["candidates"], "当前版本（训练集）": _fmt(r["current"]),
                    "最佳候选（训练集）": _fmt(r["best"]),
                    "改动": (r["best"] or {}).get("change", "--"),
                    "新版本": registry.label(nv) if nv else "--", "原因": r["reason"]})
    st.dataframe(pd.DataFrame(tbl), hide_index=True, use_container_width=True,
                 column_config={"原因": st.column_config.TextColumn(width="large"),
                                "改动": st.column_config.TextColumn(width="medium")})
    st.caption("稳健分越高越好；“替换”的策略会在空仓时自动切换到新版本，切换记录可在模拟盘日志中查看。")


STATUS_CN = {"running": "🟣 模拟运行中", "promoted": "🟢 已晋级上线", "rejected": "⚪ 已淘汰", "expired": "⚪ 已作废",
             "replaced": "⚪ 被更好的替换"}


@st.fragment(run_every=60.0)
def challengers_panel() -> None:
    from .. import challenger
    rows = challenger.all_rows(40)
    with st.container(border=True):
        run_n = sum(1 for r in rows if r["status"] == "running")
        C.section("挑战者：新版本在实时行情上模拟运行", purple=True, right=C.pill(f"{run_n} 个运行中", "purple"))
        st.caption("① 快速检验（立即）：BTC 候选在训练期之前的更早历史（2021 年起，按年分段）和 ETH 上与现役比较；"
                   "美股候选在标普 500（SPY→SSO）三个时期比较。全部不差 → 立即晋级上线；明显更差 → 立即淘汰。"
                   "② 证据不足时转入前向模拟：用登记之后的真实新行情比较，满 1/3 时间且成交 ≥3 笔、领先 ≥2 个百分点可提前晋级，"
                   "落后 ≥3 个百分点提前淘汰；否则跑满 BTC 7~30 天 / 美股 60 个交易日再定。")
        if not rows:
            st.caption("目前没有挑战者。进化找到明显更好、但历史数据不足以下结论的候选时，会自动出现在这里。")
            return
        tbl = []
        for r in rows:
            ev = r.get("last_eval") or {}
            chv, cmv = ev.get("challenger") or {}, ev.get("champion") or {}
            tbl.append({
                "编号": f"#{r['id']}", "策略": r["label"], "状态": STATUS_CN.get(r["status"], r["status"]),
                "改动": r["change"], "开始": _bj(r["registered_at"]),
                "进度": (f"{ev.get('days', 0):.1f} / {ev.get('need_days', '--')} 天" if r["kind"] == "btc"
                         else f"{ev.get('days', 0)} / {ev.get('need_days', '--')} 交易日") if ev else "等待首次评估",
                "快速检验": {"pass": "✅ 通过", "fail": "❌ 更差", "inconclusive": "⏳ 证据不足，转前向"}.get(
                    ((r.get("train") or {}).get("fast") or {}).get("verdict"), "--"),
                "挑战者前向": f"{chv['return_pct']:+.2f}%（{chv['trades']} 笔）" if chv else "--",
                "现役前向": f"{cmv['return_pct']:+.2f}%（{cmv['trades']} 笔）" if cmv else "--",
                "结论": r.get("reason") or "",
            })
        st.dataframe(pd.DataFrame(tbl), hide_index=True, use_container_width=True,
                     column_config={"结论": st.column_config.TextColumn(width="large"),
                                    "改动": st.column_config.TextColumn(width="medium")})
