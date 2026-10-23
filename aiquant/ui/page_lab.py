"""策略实验室：全局研究设置 + 五策略并行实验台 + 五策略对比分析 + DeepSeek AI 优化中心。"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, time, timedelta, timezone

import pandas as pd
import streamlit as st

from .. import actions, config, db, jobs, research
from ..ai import deepseek, optimizer
from ..data import market_data as md
from ..paper import runner as paper_runner
from ..strategy import parser, registry
from ..strategy.schema import describe, schema_hash, validate
from ..strategy.templates import TEMPLATES
from . import components as C
from .common import auto_refresh, flash, show_flash

TF_OPTS = list(config.TIMEFRAMES.values())
TF_LABEL = {v: k for k, v in config.TIMEFRAMES.items()}
SRC_LABEL = {"alpaca": "Alpaca API（只读，实时/历史）", "cache": "本地行情缓存", "test": "测试合成数据（仅测试，不可用于正式研究）"}


@st.cache_data(show_spinner=False, max_entries=64)
def _chart_data(bt_id: int):
    b = research.get_backtest(bt_id)
    try:
        return research.chart_data(b)
    except (FileNotFoundError, OSError, ValueError):   # 数据快照文件缺失或损坏：不画图，页面其余部分照常显示
        import pandas as pd
        return pd.DataFrame(), pd.DataFrame()


# ---------------------------------------------------------------------------
# 全局研究设置
# ---------------------------------------------------------------------------

def _save_research():
    ss = st.session_state
    upd = {
        "source": ss["g_src"], "timeframe_min": ss["g_tf"], "window_preset": ss["g_preset"],
        "window_anchor": "latest" if ss["g_anchor"] == "最新数据" else "train_end",
        "initial_cash": float(ss["g_cash"]), "max_alloc_pct": float(ss["g_alloc"]),
        "fee_bps": float(ss["g_fee"]), "slippage_bps": float(ss["g_slip"]), "spread_bps": float(ss["g_spread"]),
    }
    if ss["g_preset"] == "自定义" and ss.get("g_cs") and ss.get("g_ce"):
        upd["custom_start"] = datetime.combine(ss["g_cs"], time(0), tzinfo=timezone.utc).isoformat()
        upd["custom_end"] = datetime.combine(ss["g_ce"], time(0), tzinfo=timezone.utc).isoformat()
    research.set_research(upd)


def global_settings() -> dict:
    r = research.get_research()
    srcs = ["alpaca", "cache"] + (["test"] if (os.environ.get("AIQUANT_ALLOW_TEST_DATA") == "1" or r["source"] == "test")
                                  else [])
    with st.container(border=True):
        C.section("全局研究设置")
        left, right = st.columns([7.2, 1.5], gap="medium")
        with left:
            c = st.columns([1.05, 1.5, 1.0, 1.0, 1.15, 1.0, 0.85, 0.85, 0.8, 0.8])
            c[0].selectbox("交易品种", ["BTC/USD 现货"], key="g_sym", help="本版本只研究 BTC/USD 现货（Alpaca us 场所）")
            c[1].selectbox("行情来源", srcs, index=srcs.index(r["source"]) if r["source"] in srcs else 0,
                           format_func=lambda x: SRC_LABEL[x], key="g_src", on_change=_save_research)
            c[2].selectbox("K线周期（全局）", TF_OPTS, index=TF_OPTS.index(int(r["timeframe_min"])),
                           format_func=lambda x: TF_LABEL[x], key="g_tf", on_change=_save_research)
            presets = ["30天", "90天", "180天", "365天", "自定义"]
            c[3].selectbox("回测天数", presets, index=presets.index(r["window_preset"]), key="g_preset",
                           on_change=_save_research)
            anchors = ["训练集末尾（推荐）", "最新数据"]
            c[4].selectbox("窗口结束于", anchors, index=1 if r["window_anchor"] == "latest" else 0, key="g_anchor",
                           on_change=_save_research,
                           help="默认窗口位于训练集，避免消耗验证集/独立测试集。选择『最新数据』会触及测试集，系统将拒绝运行。")
            c[5].number_input("初始资金 USD", min_value=100.0, max_value=10_000_000.0, step=1000.0,
                              value=float(r["initial_cash"]), key="g_cash", on_change=_save_research)
            c[6].number_input("最大仓位%", min_value=1.0, max_value=100.0, step=5.0, value=float(r["max_alloc_pct"]),
                              key="g_alloc", on_change=_save_research)
            c[7].number_input("手续费 bps/边", min_value=0.0, max_value=200.0, step=1.0, value=float(r["fee_bps"]),
                              key="g_fee", on_change=_save_research)
            c[8].number_input("滑点 bps", min_value=0.0, max_value=100.0, step=0.5, value=float(r["slippage_bps"]),
                              key="g_slip", on_change=_save_research)
            c[9].number_input("点差代理 bps", min_value=0.0, max_value=100.0, step=0.5, value=float(r["spread_bps"]),
                              key="g_spread", on_change=_save_research,
                              help="Alpaca 历史逐笔报价未纳入本版本，采用冻结并披露的点差代理模型（全价差，每笔成交计一半）")
            if r["window_preset"] == "自定义":
                d = st.columns([1, 1, 4])
                s0, e0 = research.resolve_window(r) if r.get("custom_start") else (
                    md.to_utc(research.get_split()["val_start"]) - timedelta(days=90),
                    md.to_utc(research.get_split()["val_start"]))
                d[0].date_input("开始日期(UTC)", value=s0.date(), key="g_cs", on_change=_save_research)
                d[1].date_input("结束日期(UTC)", value=e0.date(), key="g_ce", on_change=_save_research)
            try:
                s, e = research.resolve_window(research.get_research())
                lab = research.split_label(s, e)
                warn = "" if lab == "训练集" else " ⚠ 窗口不完全位于训练集"
                st.caption(f"回测窗口：{s:%Y-%m-%d %H:%M} → {e:%Y-%m-%d %H:%M} UTC（{(e - s).days} 天，{lab}）{warn}　·　"
                           f"K线周期 ≠ 回测天数：信号按所选周期在整个窗口内执行；另加 {md.DEFAULT_WARMUP_DAYS} 天仅用于指标预热。")
            except Exception as ex:  # noqa: BLE001
                st.caption(f"窗口设置无效：{ex}")
        with right:
            if st.button("▶ 同步运行5个策略", type="primary", key="btn_run_all", use_container_width=True):
                res = []
                for n in range(1, 6):
                    ok, msg = ensure_frozen(n)
                    if not ok:
                        res.append(f"策略{n}: {msg}")
                        continue
                    jid, m = actions.submit_backtest(n)
                    res.append(f"策略{n}: {m}")
                flash("global", "ok", "；".join(res))
                st.rerun()
            b1, b2 = st.columns(2)
            if b1.button("停止全部", key="btn_stop_all", use_container_width=True):
                n = jobs.cancel_all()
                flash("global", "warn", f"已请求停止 {n} 个任务")
                st.rerun()
            b2.download_button("导出结果", data=_export_bytes(), file_name="aiquant_results.zip",
                               mime="application/zip", key="btn_export", use_container_width=True)
            if st.button("✦ 批量 AI 优化", type="primary", key="ai_batch_top", use_container_width=True):
                jid, msg = actions.submit_ai_batch()
                flash("global", "ok" if jid else "err", msg)
                st.rerun()
    show_flash("global")
    return research.get_research()


def _export_bytes() -> bytes:
    sig = db.q1("SELECT COALESCE(MAX(id),0) AS m FROM backtests")["m"]
    try:
        return _export_cached(sig)
    except Exception as e:  # noqa: BLE001
        return f"导出失败: {e}".encode("utf-8")


@st.cache_data(show_spinner=False, max_entries=4)
def _export_cached(sig: int) -> bytes:
    return actions.export_zip()


# ---------------------------------------------------------------------------
# 研究目标横幅
# ---------------------------------------------------------------------------

def target_banner(r: dict) -> None:
    rows = db.q("SELECT b.split_label, b.metrics_json, b.is_test, v.name, v.version_no, b.slot_no FROM backtests b "
                "JOIN strategy_versions v ON v.id=b.version_id WHERE b.is_test=0")
    best = {"训练集": None, "验证集": None, "独立测试": None}
    for x in rows:
        m = json.loads(x["metrics_json"])
        sp = x["split_label"]
        if sp in best and (best[sp] is None or m["net_return_pct"] > best[sp][0]):
            best[sp] = (m["net_return_pct"], f"{x['name']} v{x['version_no']}")
    tst = research.test_set_status()
    tgt = config.DEFAULTS.target_return_pct
    def f(k):
        v = best[k]
        return f'<b class="{C.tone(v[0])}">{C.pct(v[0])}</b> <span class="aq-tag">{C.esc(v[1])}</span>' if v else "<b>--</b>"
    gap = f"{tgt - best['训练集'][0]:+.2f}%" if best["训练集"] else "--"
    test_txt = f(("独立测试")) if best["独立测试"] else f'<b>--</b> <span class="aq-tag">{C.esc(tst["label"])}</span>'
    act = jobs.active_count()
    ds = db.q1("SELECT * FROM datasets ORDER BY id DESC LIMIT 1")
    dq = json.loads(ds["quality_json"]) if ds else None
    data_txt = (f'数据：{C.esc(ds["source"])} · {ds["start_utc"][:10]}→{ds["end_utc"][:10]} · 覆盖率 {dq["coverage_pct"]}% '
                f'({C.esc(dq["grade"])}) · 采集 {ds["fetched_at"][:16]} · 哈希 {ds["sha256"][:10]}'
                + (' · <b class="neg">测试数据</b>' if ds["is_test"] else "")) if ds else "数据：尚未加载（首次回测会自动从所选来源获取）"
    st.markdown(
        f'<div class="aq-banner"><span>🎯 <b>研究目标：90天净收益 ≥ 30%</b>（BTC/USD 现货 | 初始资金 {r["initial_cash"]:,.0f} USD | '
        f'最大仓位 {r["max_alloc_pct"]:.0f}% | 仅做多 | 1倍 | 不使用杠杆）</span>'
        f'<span>最佳训练收益 {f("训练集")}</span><span>最佳验证收益 {f("验证集")}</span>'
        f'<span>独立测试收益 {test_txt}</span><span>距离目标 <b>{gap}</b>（训练集结果不代表目标达成）</span>'
        f'<span>研究状态 {C.pill("运行中 " + str(act), "blue") if act else C.pill("空闲", "gray")}</span>'
        f'<span class="aq-sub" style="flex-basis:100%">{data_txt}</span></div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# 单个策略位
# ---------------------------------------------------------------------------

def txt_key(n: int) -> str:
    return f"txt_{n}_{st.session_state.get(f'txtrev_{n}', 0)}"


def cur_text(n: int) -> str:
    return st.session_state.get(txt_key(n), registry.get_slot(n)["draft_text"])


def set_text(n: int, text: str) -> None:
    """程序化修改策略描述：写入数据库并刷新输入框。"""
    registry.save_draft(n, text=text)
    st.session_state[f"txtrev_{n}"] = st.session_state.get(f"txtrev_{n}", 0) + 1


def _draft_name(n: int, schema: dict | None) -> str:
    if schema and schema.get("name") and schema["name"] != "自定义策略":
        return schema["name"]
    return f"策略{n}"


def do_parse(n: int, use_llm: bool = False) -> tuple[bool, str]:
    text = cur_text(n)
    slot = registry.get_slot(n)
    old = slot["draft_fields"].get("schema") or {}
    if use_llm:
        ok, why = deepseek.available()
        if not ok:
            return False, why
        try:
            schema, errs, row = optimizer.parse_with_deepseek(text)
        except Exception as e:  # noqa: BLE001
            return False, f"DeepSeek 解析失败：{e}"
        info = {"status": "ok" if schema and not errs else ("incomplete" if schema else "error"),
                "errors": errs, "warnings": [], "unresolved": [],
                "method": f"DeepSeek 模型调用 #{row['id']}（原始响应已保存{'，复用历史结果' if row.get('reused') else ''}）",
                "text": text}
        if schema:
            schema["name"] = old.get("name") or _draft_name(n, schema)
        registry.save_draft(n, text=text, draft_schema=schema, parse_info=info)
        return info["status"] == "ok", "解析完成" if info["status"] == "ok" else "；".join(errs)
    res = parser.parse(text, old.get("name") or f"策略{n}")
    sch = res.schema
    if sch is not None:
        sch["name"] = old.get("name") or f"策略{n}"
        if old.get("hypothesis"):
            sch["hypothesis"] = old["hypothesis"]
    info = {"status": res.status, "errors": res.errors, "warnings": res.warnings, "unresolved": res.unresolved,
            "method": res.method, "text": text}
    registry.save_draft(n, text=text, draft_schema=sch, parse_info=info)
    if res.status == "ok":
        return True, "解析完成：已转换为结构化 Strategy Schema"
    return False, "；".join(res.errors + [f"未识别：{u}" for u in res.unresolved])


def ensure_frozen(n: int) -> tuple[bool, str]:
    """运行回测前：校验草稿 → 冻结版本（相同内容不重复创建）。"""
    slot = registry.get_slot(n)
    text = cur_text(n)
    info = slot["draft_fields"].get("parse") or {}
    sch = slot["draft_fields"].get("schema")
    if sch is None or (info.get("text") is not None and info.get("text") != text and info.get("method") != "示例模板"
                       and not str(info.get("method", "")).startswith("参数设置")):
        ok, msg = do_parse(n)
        if not ok:
            return False, f"策略解析未完成：{msg}"
        slot = registry.get_slot(n)
        info = slot["draft_fields"].get("parse") or {}
        sch = slot["draft_fields"].get("schema")
    if info.get("status") != "ok":
        return False, "策略存在待补充/错误项，请修正后再冻结：" + "；".join(info.get("errors", []))
    try:
        v = registry.freeze(n, sch, nl_text=text, source="manual" if "DeepSeek" not in info.get("method", "") else "deepseek_parse")
    except registry.FreezeError as e:
        return False, f"校验失败：{e}"
    return True, f"已冻结 {v['name']} v{v['version_no']}（{v['schema_hash'][:10]}）"


def _on_text_change(n: int):
    registry.save_draft(n, text=st.session_state[txt_key(n)])


def _on_tf_change(n: int):
    v = st.session_state[f"tf_{n}"]
    registry.save_draft(n, timeframe_mode="follow" if v == "follow" else str(v))


def _on_aux_change(n: int):
    v = st.session_state[f"aux_{n}"]
    registry.save_draft(n, aux_tf=None if v == "schema" else int(v))


def _fill_template(n: int):
    name = st.session_state[f"tpl_{n}"]
    t = TEMPLATES[name]
    set_text(n, t["text"])
    sch = json.loads(json.dumps(t["schema"]))
    obj, _ = validate(sch)
    registry.save_draft(n, text=t["text"], draft_schema=obj.model_dump(),
                        parse_info={"status": "ok", "errors": [], "warnings": [], "unresolved": [],
                                    "method": "示例模板", "text": t["text"]})
    flash(f"slot{n}", "ok", f"已填充模板『{name}』（只影响策略{n}）")


@st.dialog("K线与交易明细", width="large")
def kline_dialog(bt_id: int, title: str):
    b = research.get_backtest(bt_id)
    k, trades = _chart_data(bt_id)
    st.markdown(f"**{C.esc(title)}** · 信号周期 {TF_LABEL.get(b['signal_tf'])} · 窗口 {b['window_start'][:16]} → "
                f"{b['window_end'][:16]} UTC · 数据版本 #{b['dataset_id']} `{b['dataset_hash'][:12]}`"
                + (" · **测试数据**" if b["is_test"] else ""))
    n = st.slider("显示最近K线数量", 100, max(100, min(len(k), 5000)), min(len(k), 600), step=50, key=f"dlg_n_{bt_id}")
    st.plotly_chart(C.kline_figure(k, trades, height=460, last_n=n), use_container_width=True, key=f"dlg_ch_{bt_id}")
    if trades.empty:
        st.info("该回测没有成交，图中不显示任何买卖点。")
    else:
        t = trades.rename(columns={"entry_signal_ts": "入场信号时间", "entry_fill_ts": "入场成交时间", "entry_price": "买入价格",
                                   "exit_signal_ts": "退出信号时间", "exit_fill_ts": "退出成交时间", "exit_price": "卖出价格",
                                   "qty": "BTC数量", "fee": "手续费", "spread_cost": "点差", "slippage_cost": "滑点",
                                   "exit_reason": "退出原因", "pnl": "每笔盈亏", "equity_after": "累计净值"})
        st.dataframe(t.drop(columns=["id", "backtest_id"]), use_container_width=True, height=300,
                     column_config={c: st.column_config.NumberColumn(format="%.2f") for c in
                                    ("买入价格", "卖出价格", "手续费", "点差", "滑点", "每笔盈亏", "累计净值")} |
                                   {"BTC数量": st.column_config.NumberColumn(format="%.6f")})
    with st.expander("回测假设（统一且明确披露）"):
        for a in b["assumptions"]["rules"]:
            st.markdown(f"- {a}")
        st.json(b["assumptions"]["cost_model"])


def slot_card(n: int, r: dict) -> None:
    slot = registry.get_slot(n)
    cur = registry.get_version(slot["current_version_id"])
    draft = slot["draft_fields"].get("schema")
    info = slot["draft_fields"].get("parse") or {}
    st_ = actions.slot_status(n)
    res = actions.slot_result(n)
    name = cur["name"] if cur else _draft_name(n, draft)
    ver = f" v{cur['version_no']}" if cur else ""
    with st.container(border=True, key=f"card_{n}"):
        st.markdown(f'<div class="aq-slothead"><div><span class="aq-num">{n}</span><span class="aq-slotname" '
                    f'title="{C.esc(name)}">{C.esc(name)}{ver}</span></div>{C.pill(st_["label"], st_["tone"])}</div>',
                    unsafe_allow_html=True)
        tf_eff = None
        try:
            tf_eff, aux_eff = research.effective_tf(slot, (cur or {}).get("schema") or draft or {}, r)
        except ValueError:
            aux_eff = None
        st.markdown(f'<div class="aq-meta">BTC/USD · {TF_LABEL.get(tf_eff, "--")}'
                    f'{" + 辅助 " + TF_LABEL[aux_eff] if aux_eff else ""}</div>', unsafe_allow_html=True)
        # K线缩略图（与回测数据版本一致）
        if res:
            k, trades = _chart_data(res["id"])
            if k.empty:
                st.markdown('<div class="aq-placeholder">K线数据快照缺失，无法绘图（回测结果不受影响）</div>',
                            unsafe_allow_html=True)
            else:
                st.plotly_chart(C.kline_figure(k, trades, height=165, last_n=150), use_container_width=True,
                                key=f"mini_{n}_{res['id']}", config={"displayModeBar": False})
            if not k.empty and st.button("🔍 放大查看K线与交易明细", key=f"zoom_{n}", use_container_width=True):
                kline_dialog(res["id"], f"策略{n} {name}{ver}")
        else:
            st.markdown('<div class="aq-placeholder">暂无回测结果<br>运行回测后显示真实K线与买卖点</div>',
                        unsafe_allow_html=True)
        # 周期
        cc = st.columns(2)
        mode = slot["timeframe_mode"]
        tf_opts = ["follow"] + TF_OPTS
        cc[0].selectbox("信号周期", tf_opts, index=tf_opts.index("follow" if mode == "follow" else int(mode)),
                        format_func=lambda x: "跟随全局" if x == "follow" else TF_LABEL[x], key=f"tf_{n}",
                        on_change=_on_tf_change, args=(n,))
        aux_opts = ["schema"] + TF_OPTS[1:]
        auxv = slot["aux_timeframe_min"]
        cc[1].selectbox("辅助周期", aux_opts, index=aux_opts.index(auxv) if auxv in aux_opts else 0,
                        format_func=lambda x: "按策略定义" if x == "schema" else TF_LABEL[x], key=f"aux_{n}",
                        on_change=_on_aux_change, args=(n,), help="多周期策略：主周期产生信号，辅助周期（更大）判断趋势")
        t1, t2, t3 = st.tabs(["策略输入", "参数设置", "示例模板"])
        with t1:
            st.text_area("策略描述（自然语言）", value=slot["draft_text"], key=txt_key(n), height=130, max_chars=1500,
                         on_change=_on_text_change, args=(n,),
                         placeholder="例如：当BTC的EMA20向上突破EMA60时买入，反向交叉时平仓，固定止损2%，盈利3%后启动移动止损。")
            pc = st.columns(2)
            if pc[0].button("解析策略", key=f"parse_{n}", use_container_width=True):
                ok, msg = do_parse(n)
                flash(f"slot{n}", "ok" if ok else "err", msg)
                st.rerun()
            if pc[1].button("DeepSeek解析", key=f"ai_parse_{n}", use_container_width=True,
                            help="这是一次 DeepSeek 模型调用；原始响应会保存，结果必须通过 Schema 校验"):
                with st.spinner("DeepSeek 模型调用中…"):
                    ok, msg = do_parse(n, use_llm=True)
                flash(f"slot{n}", "ok" if ok else "err", msg)
                st.rerun()
        with t2:
            params_form(n, draft)
        with t3:
            st.selectbox("选择模板", list(TEMPLATES), key=f"tpl_{n}")
            st.caption(TEMPLATES[st.session_state.get(f"tpl_{n}", "趋势跟踪")]["text"])
            st.button("填充到本策略位", key=f"tplbtn_{n}", on_click=_fill_template, args=(n,), use_container_width=True)
        show_flash(f"slot{n}")
        # 固定格式字段
        obj = None
        if draft:
            obj, errs = validate(draft)
        stat = info.get("status")
        if stat:
            tone = {"ok": "green", "incomplete": "orange", "error": "red"}.get(stat, "gray")
            lab = {"ok": "已解析", "incomplete": "待补充", "error": "错误"}.get(stat, stat)
            dirty = ""
            if obj and cur and schema_hash(obj) != cur["schema_hash"]:
                dirty = C.pill("草稿未冻结", "orange")
            elif obj and cur:
                dirty = C.pill(f"= 已冻结 v{cur['version_no']}", "blue")
            st.markdown(f'{C.pill(lab, tone)} {dirty} <span class="aq-sub">{C.esc(info.get("method", ""))}</span>',
                        unsafe_allow_html=True)
            for w in info.get("warnings", [])[:3]:
                st.markdown(f'<div class="aq-warn">{C.esc(w)}</div>', unsafe_allow_html=True)
            if stat != "ok":
                for e in (info.get("errors", []) + [f"未识别：{u}" for u in info.get("unresolved", [])])[:4]:
                    st.markdown(f'<div class="aq-err">{C.esc(e)}</div>', unsafe_allow_html=True)
        if obj:
            d = describe(obj)
            st.markdown(C.kv([("策略名称", d["策略名称"]), ("入场规则", d["入场规则"]), ("出场规则", d["退出规则"]),
                              ("止损规则", d["止损规则"])]), unsafe_allow_html=True)
            with st.expander("展开更多参数"):
                st.markdown(C.kv([(k, d[k]) for k in ("止盈规则", "移动止损", "资金配置", "指标", "参数", "主周期",
                                                     "辅助周期", "最长持仓", "研究假设")]), unsafe_allow_html=True)
        # 操作按钮
        b = st.columns(2)
        running = st_["job"] and st_["job"]["status"] in ("queued", "running")
        if b[0].button("▶ 运行回测", key=f"run_{n}", type="primary", use_container_width=True, disabled=bool(running)):
            ok, msg = ensure_frozen(n)
            if ok:
                jid, m2 = actions.submit_backtest(n)
                flash(f"slot{n}", "ok" if jid else "err", f"{msg}；{m2}")
            else:
                flash(f"slot{n}", "err", msg)
            st.rerun()
        if b[1].button("停止", key=f"stop_{n}", use_container_width=True, disabled=not running):
            actions.stop_slot(n)
            flash(f"slot{n}", "warn", "已请求停止")
            st.rerun()
        b = st.columns(3)
        if b[0].button("保存版本", key=f"save_{n}", use_container_width=True):
            ok, msg = ensure_frozen(n)
            flash(f"slot{n}", "ok" if ok else "err", msg)
            st.rerun()
        with b[1].popover("复制策略", use_container_width=True):
            if cur:
                tgt = st.selectbox("复制到策略位", [i for i in range(1, 6) if i != n], key=f"cpt_{n}")
                if st.button("确认复制", key=f"cpb_{n}"):
                    nv = registry.copy_to_slot(cur["id"], tgt)
                    set_text(tgt, nv["nl_text"])
                    registry.save_draft(tgt, draft_schema=nv["schema"], parse_info={
                        "status": "ok", "errors": [], "warnings": [], "unresolved": [],
                        "method": f"复制自策略{n} v{cur['version_no']}", "text": nv["nl_text"]})
                    flash(f"slot{tgt}", "ok", f"已从策略{n}复制 {cur['name']} v{cur['version_no']}")
                    st.rerun()
            else:
                st.caption("请先保存（冻结）版本")
        if b[2].button("✦ AI优化", key=f"ai_opt_{n}", use_container_width=True):
            jid, msg = actions.submit_ai_analyze(n)
            flash(f"slot{n}", "ok" if jid else "err", msg)
            st.rerun()
        if st.button("⇪ 部署到模拟盘", key=f"deploy_{n}", use_container_width=True):
            if not cur:
                flash(f"slot{n}", "err", "无法部署：尚未冻结策略版本")
            else:
                ok, checks = paper_runner.deploy_gate(cur["id"])
                if ok:
                    st.session_state["deploy_version"] = cur["id"]
                    st.switch_page(st.session_state["_pages"]["paper"])
                else:
                    flash(f"slot{n}", "err", "暂不能部署：" + "；".join(f"{nm}（{dt}）" for nm, p, dt in checks if not p))
            st.rerun()
        # 任务状态
        j = st_["job"]
        if j and j["status"] in ("queued", "running"):
            st.caption(f"任务 #{j['id']}：{j['stage']}")
        elif j and j["status"] == "failed":
            st.markdown(f'<div class="aq-err">任务 #{j["id"]} 失败：{C.esc(j["error"] or "")}</div>', unsafe_allow_html=True)
        # 结果
        m = res["metrics"] if res else None
        if res:
            st.markdown(f'<div class="aq-sub">结果：v{registry.get_version(res["version_id"])["version_no"]} · '
                        f'{res["split_label"]}{" · <b class=neg>测试数据</b>" if res["is_test"] else ""} · '
                        f'{res["window_start"][:10]}→{res["window_end"][:10]}</div>', unsafe_allow_html=True)
        st.markdown(C.metrics_grid(m), unsafe_allow_html=True)


def params_form(n: int, draft: dict | None) -> None:
    if not draft:
        st.caption("请先在『策略输入』中解析策略，或从『示例模板』填充。")
        return
    obj, errs = validate(draft)
    if errs:
        st.markdown(f'<div class="aq-err">当前草稿未通过校验：{C.esc("；".join(errs))}</div>', unsafe_allow_html=True)
        return
    with st.form(f"pf_{n}", border=False):
        name = st.text_input("策略名称", value=obj.name, max_chars=60)
        c = st.columns(2)
        sl = c[0].number_input("止损 %（0=无）", 0.0, 50.0, float(obj.stop_loss_pct or 0), 0.1)
        tp = c[1].number_input("止盈 %（0=无）", 0.0, 500.0, float(obj.take_profit_pct or 0), 0.5)
        ta = c[0].number_input("移动止损启动 %（0=不用）", 0.0, 100.0, float(obj.trailing.activate_pct if obj.trailing else 0), 0.5)
        tt = c[1].number_input("移动止损回撤 %", 0.0, 50.0, float(obj.trailing.trail_pct if obj.trailing else 0), 0.1)
        al = c[0].number_input("资金配置 %", 1.0, 100.0, float(obj.alloc_pct), 5.0)
        mh = c[1].number_input("最长持仓K线（0=不限）", 0, 100000, int(obj.max_hold_bars or 0), 1)
        new_params = {}
        for ind in obj.indicators:
            cols = st.columns(len(ind.params))
            for i, (k, v) in enumerate(ind.params.items()):
                new_params[(ind.id, k)] = cols[i].number_input(f"{ind.id}.{k}", value=float(v), step=1.0 if k != "std" else 0.1,
                                                               key=f"p_{n}_{ind.id}_{k}")
        hyp = st.text_area("研究假设", value=obj.hypothesis, height=68)
        if st.form_submit_button("应用参数到草稿", use_container_width=True):
            d = obj.model_dump()
            d.update({"name": name, "stop_loss_pct": sl or None, "take_profit_pct": tp or None,
                      "alloc_pct": al, "max_hold_bars": int(mh) or None, "hypothesis": hyp,
                      "trailing": {"activate_pct": ta, "trail_pct": tt} if tt > 0 else None})
            for ind in d["indicators"]:
                for k in ind["params"]:
                    ind["params"][k] = new_params[(ind["id"], k)]
            o2, e2 = validate(d)
            if e2:
                flash(f"slot{n}", "err", "参数未通过校验：" + "；".join(e2))
            else:
                registry.save_draft(n, draft_schema=o2.model_dump(), parse_info={
                    "status": "ok", "errors": [], "warnings": [], "unresolved": [], "method": "参数设置（手动修改）",
                    "text": cur_text(n)})
                flash(f"slot{n}", "ok", "参数已应用到草稿；运行回测或保存版本时会冻结为新版本")
            st.rerun()


# ---------------------------------------------------------------------------
# 五策略对比分析
# ---------------------------------------------------------------------------

def comparison(r: dict) -> None:
    lb = actions.leaderboard()
    try:
        ws, we = research.resolve_window(r)
        ref_window = (ws.isoformat(), we.isoformat())
    except Exception:  # noqa: BLE001
        ref_window = None
    cm = research.cost_model(r).as_dict()

    def comparable(row) -> bool:
        if row["_bt"] is None or pd.isna(row["_bt"]):
            return False
        b = research.get_backtest(int(row["_bt"]))
        return (row["_window"] == ref_window and b["assumptions"].get("cost_model") == cm
                and float(b["assumptions"].get("initial_cash")) == float(r["initial_cash"])
                and float(b["assumptions"].get("max_alloc_pct")) == float(r["max_alloc_pct"]))

    lb["可比"] = lb.apply(comparable, axis=1)
    done = lb[lb["可比"]].sort_values("净收益率%", ascending=False)
    lb["排名"] = None
    for i, idx in enumerate(done.index, 1):
        lb.at[idx, "排名"] = i
    lb = lb.sort_values(["排名"], na_position="last")
    bm = None
    if len(done):
        bm = research.benchmark_for(research.get_backtest(int(done.iloc[0]["_bt"])))

    top_l, top_r = st.columns([2.2, 1], gap="medium")
    with top_l:
        with st.container(border=True):
            C.section("五策略对比分析", right=C.pill(f"可比结果 {len(done)}/5", "blue"))
            tabs = st.tabs(["核心指标对比", "胜率对比", "交易次数对比"])
            with tabs[0]:
                show = lb.drop(columns=["_bt", "_window", "可比"]).copy()
                st.dataframe(show, use_container_width=True, hide_index=True, height=230,
                             column_order=["排名", "策略位", "策略名称", "版本", "K线周期", "状态", "数据分区", "净收益率%",
                                           "净利润", "最大回撤%", "胜率%", "Profit Factor", "交易次数", "手续费", "滑点", "点差"],
                             column_config={
                                 "净收益率%": st.column_config.NumberColumn(format="%+.2f%%"),
                                 "最大回撤%": st.column_config.NumberColumn(format="%.2f%%"),
                                 "胜率%": st.column_config.NumberColumn(format="%.1f%%"),
                                 "净利润": st.column_config.NumberColumn(format="%+,.0f"),
                                 "Profit Factor": st.column_config.NumberColumn(format="%.2f"),
                                 "手续费": st.column_config.NumberColumn(format="%,.0f"),
                                 "滑点": st.column_config.NumberColumn(format="%,.0f"),
                                 "点差": st.column_config.NumberColumn(format="%,.0f"),
                             })
                st.caption("只有与当前全局设置（相同窗口、初始资金、成本、仓位）一致且已完成的回测参与排名；可点击表头排序。")
            with tabs[1]:
                st.plotly_chart(C.bar_compare([f"策略{i}" for i in done["策略位"]], done["胜率%"].tolist(), height=230,
                                              colors=[C.SLOT_COLORS[i] for i in done["策略位"]]),
                                use_container_width=True, key="cmp_win")
            with tabs[2]:
                st.plotly_chart(C.bar_compare([f"策略{i}" for i in done["策略位"]], done["交易次数"].tolist(), height=230,
                                              suffix="", colors=[C.SLOT_COLORS[i] for i in done["策略位"]]),
                                use_container_width=True, key="cmp_trades")
    with top_r:
        with st.container(border=True):
            C.section("净收益率对比")
            labels = [f"策略{i}" for i in done["策略位"]]
            vals = done["净收益率%"].tolist()
            if bm:
                labels.append("BTC买入持有")
                vals.append(bm["metrics"]["net_return_pct"])
            if labels:
                st.plotly_chart(C.bar_compare(labels, vals, height=268), use_container_width=True, key="cmp_ret")
            else:
                st.markdown('<div class="aq-placeholder">暂无可比的已完成回测</div>', unsafe_allow_html=True)

    c = st.columns([1.35, 1, 1, 1], gap="small")
    with c[0]:
        with st.container(border=True):
            C.section("权益曲线对比（累计收益率）")
            series = {}
            for _, row in done.iterrows():
                b = research.get_backtest(int(row["_bt"]))
                series[f"策略{row['策略位']}"] = (b["equity"], C.SLOT_COLORS[row["策略位"]], float(r["initial_cash"]))
            if bm:
                series["BTC买入持有"] = (bm["equity"], C.SLOT_COLORS["BH"], float(r["initial_cash"]))
            if series:
                st.plotly_chart(C.equity_compare(series, height=250), use_container_width=True, key="cmp_eq")
            else:
                st.markdown('<div class="aq-placeholder">暂无数据</div>', unsafe_allow_html=True)
    with c[1]:
        with st.container(border=True):
            C.section("最大回撤对比")
            labels = [f"策略{i}" for i in done["策略位"]]
            vals = done["最大回撤%"].tolist()
            if bm:
                labels.append("BTC买入持有"); vals.append(bm["metrics"]["max_drawdown_pct"])
            if labels:
                st.plotly_chart(C.bar_compare(labels, vals, height=250, horizontal=True,
                                              colors=[C.RED] * len(vals)), use_container_width=True, key="cmp_dd")
            else:
                st.markdown('<div class="aq-placeholder">暂无数据</div>', unsafe_allow_html=True)
    with c[2]:
        with st.container(border=True):
            C.section("交易成本对比（USD）")
            if len(done):
                st.plotly_chart(C.stacked_cost([f"策略{i}" for i in done["策略位"]], done["手续费"].tolist(),
                                               done["点差"].tolist(), done["滑点"].tolist(), height=250),
                                use_container_width=True, key="cmp_cost")
            else:
                st.markdown('<div class="aq-placeholder">暂无数据</div>', unsafe_allow_html=True)
    with c[3]:
        with st.container(border=True):
            C.section("BTC 买入持有基准")
            if bm:
                m = bm["metrics"]
                best = done.iloc[0]
                diff = best["净收益率%"] - m["net_return_pct"]
                st.markdown(
                    f'<div class="aq-sub">{bm["window_start"][:10]} → {bm["window_end"][:10]} · 相同成本模型'
                    f'{" · <b class=neg>测试数据</b>" if bm["is_test"] else ""}</div>'
                    f'<div class="aq-big {C.tone(m["net_return_pct"])}">{C.pct(m["net_return_pct"])}</div>'
                    f'<div class="{C.tone(m["net_profit"])}">{C.usd(m["net_profit"], True)}（扣费后；原始涨跌 '
                    f'{C.pct(m["raw_price_change_pct"])}）</div>'
                    + C.kv([("当前最优", f"策略{best['策略位']} {best['策略名称']} {C.pct(best['净收益率%'])}"),
                            ("超额收益", C.pct(diff)), ("跑赢持有", "是" if diff > 0 else "否"),
                            ("持有回撤", C.pct(m["max_drawdown_pct"]))]), unsafe_allow_html=True)
            else:
                st.markdown('<div class="aq-placeholder">完成至少一个可比回测后显示</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# DeepSeek AI 优化中心
# ---------------------------------------------------------------------------

def _candidates_for(call_id: int, slot_no: int) -> list[dict]:
    rows = db.q("SELECT id FROM strategy_versions WHERE llm_call_id=? AND slot_no=? ORDER BY id", (call_id, slot_no))
    return [registry.get_version(x["id"]) for x in rows]


def _apply_version(slot_no: int, vid: int):
    v = registry.get_version(vid)
    registry.set_current(slot_no, vid)
    registry.save_draft(slot_no, draft_schema=v["schema"], parse_info={
        "status": "ok", "errors": [], "warnings": [], "unresolved": [],
        "method": f"应用版本 v{v['version_no']}", "text": cur_text(slot_no)})
    flash(f"slot{slot_no}", "ok", f"已应用 {v['name']} v{v['version_no']}；需重新回测后才有结果")


def ai_center() -> None:
    ok, why = deepseek.available()
    with st.container(border=True):
        h = st.columns([5, 1.2, 1.2])
        h[0].markdown(f'<div class="aq-section"><span class="bar purple"></span>DeepSeek AI 优化中心 '
                      f'<span class="aq-sub">基于训练集真实回测结果分析；模型只提出候选 Schema，不执行代码、不下单 · {C.esc(why)}</span></div>',
                      unsafe_allow_html=True)
        if h[1].button("✦ 生成下一轮5个优化策略", key="ai_batch_center", type="primary", use_container_width=True):
            jid, msg = actions.submit_ai_batch()
            flash("ai", "ok" if jid else "err", msg)
            st.rerun()
        if h[2].button("全部分析一次", key="ai_all", use_container_width=True):
            msgs = [f"策略{i}: {actions.submit_ai_analyze(i)[1]}" for i in range(1, 6)]
            flash("ai", "ok", "；".join(msgs))
            st.rerun()
        show_flash("ai")
        cols = st.columns([1, 1, 1, 1, 1, 1.25], gap="small")
        for i in range(1, 6):
            with cols[i - 1]:
                ai_card(i)
        with cols[5]:
            with st.container(border=True):
                C.section("全局 AI 总结与下一步建议", purple=True)
                b = optimizer.latest_batch()
                if b is None:
                    st.caption("尚未进行五策略横向分析。完成回测后点击『生成下一轮5个优化策略』。")
                elif b["status"] == "running":
                    st.caption("分析中…")
                elif b["status"] == "failed":
                    st.markdown(f'<div class="aq-err">失败：{C.esc(b["error"])}</div>', unsafe_allow_html=True)
                else:
                    p = b["parsed"]
                    st.markdown(f'<div class="aq-sub">调用 #{b["id"]} · {C.esc(b["model"])} · {b["created_at"][:16]}</div>',
                                unsafe_allow_html=True)
                    st.markdown(C.esc(str(p.get("summary", "")))[:1200])
                    for s in (p.get("next_steps") or [])[:6]:
                        st.markdown(f'<div class="aq-ai-li">• {C.esc(s)}</div>', unsafe_allow_html=True)
                    cv = p.get("_created_versions") or []
                    if cv:
                        st.markdown(f'<div class="aq-ok">已校验并保存 {len(cv)} 个候选版本（未替换任何策略位，需应用后重新回测）</div>',
                                    unsafe_allow_html=True)
                        for vid in cv:
                            v = registry.get_version(vid)
                            cc = st.columns([3, 1.3])
                            cc[0].caption(f"策略{v['slot_no']} → {v['name']} v{v['version_no']}")
                            if cc[1].button("应用", key=f"ai_apply_b_{vid}"):
                                _apply_version(v["slot_no"], vid)
                                st.rerun()
                    for e in p.get("_errors") or []:
                        st.markdown(f'<div class="aq-err">{C.esc(e)}</div>', unsafe_allow_html=True)


def ai_card(n: int) -> None:
    a = optimizer.latest_analysis(n)
    with st.container(border=True):
        head = {"done": ("已完成", "green"), "running": ("正在分析", "blue"), "failed": ("失败", "red")}
        lab, t = head.get(a["status"], ("--", "gray")) if a else ("未分析", "gray")
        st.markdown(f'<div class="aq-slothead"><b style="font-size:13px">策略{n} AI 分析建议</b>{C.pill(lab, t)}</div>',
                    unsafe_allow_html=True)
        if not a:
            st.caption("完成训练集回测后点击卡片上的『AI优化』。")
            return
        if a["status"] == "failed":
            st.markdown(f'<div class="aq-err">{C.esc(a["error"] or "")[:300]}</div>', unsafe_allow_html=True)
            return
        if a["status"] != "done":
            st.caption("模型调用进行中…")
            return
        p = a["parsed"] or {}
        st.markdown(f'<div class="aq-sub">调用 #{a["id"]} · {C.esc(a["model"])} · 基于版本 #{a["version_id"]} / 回测 #{a["backtest_id"]}</div>',
                    unsafe_allow_html=True)
        for o in (p.get("observations") or [])[:3]:
            if isinstance(o, dict):
                st.markdown(f'<div class="aq-ai-li"><span class="aq-tag">事实</span>{C.esc(o.get("fact", ""))}</div>',
                            unsafe_allow_html=True)
        for h in (p.get("hypotheses") or [])[:2]:
            if isinstance(h, dict):
                h = h.get("hypothesis") or h.get("text") or "；".join(str(v) for v in h.values())
            st.markdown(f'<div class="aq-ai-li"><span class="aq-tag">假设</span>{C.esc(h)}</div>', unsafe_allow_html=True)
        st.markdown(f'<div class="aq-ai-li"><span class="aq-tag">建议</span>{C.esc(p.get("change_summary", ""))}</div>',
                    unsafe_allow_html=True)
        cands = _candidates_for(a["id"], n)
        parent = registry.get_version(a["version_id"])
        if not cands:
            nxt = (max(x["version_no"] for x in registry.lineage_versions(parent["lineage_id"])) + 1) if parent else 2
            if st.button(f"生成 v{nxt} 版本", key=f"ai_gen_{n}_{a['id']}", type="primary", use_container_width=True):
                try:
                    v = optimizer.create_candidate(a["id"])
                    flash("ai", "ok", f"已生成并冻结 {v['name']} v{v['version_no']}（未标记为盈利，需重新回测）")
                except Exception as e:  # noqa: BLE001
                    flash("ai", "err", str(e))
                st.rerun()
        for v in cands:
            st.markdown(f'<div class="aq-ok">候选 {C.esc(v["name"])} v{v["version_no"]} · {v["schema_hash"][:8]}</div>',
                        unsafe_allow_html=True)
            with st.popover("查看修改差异", use_container_width=True):
                d = registry.diff(parent["schema"], v["schema"]) if parent else []
                if d:
                    st.dataframe(pd.DataFrame(d, columns=["字段", f"v{parent['version_no']}", f"v{v['version_no']}"]),
                                 hide_index=True, use_container_width=True)
                else:
                    st.caption("无差异")
                st.json({k: p.get(k) for k in ("rule_changes", "param_changes", "validation_method", "kill_criteria")})
            cc = st.columns(2)
            cur_id = registry.get_slot(n)["current_version_id"]
            if cc[0].button("应用至策略位", key=f"ai_apply_{n}_{v['id']}", disabled=cur_id == v["id"], use_container_width=True):
                _apply_version(n, v["id"])
                st.rerun()
            with cc[1].popover("复制至…", use_container_width=True):
                tgt = st.selectbox("目标策略位", [i for i in range(1, 6) if i != n], key=f"ai_cpt_{n}_{v['id']}")
                if st.button("确认", key=f"ai_cpb_{n}_{v['id']}"):
                    nv = registry.copy_to_slot(v["id"], tgt)
                    set_text(tgt, nv["nl_text"])
                    flash(f"slot{tgt}", "ok", f"已复制 {v['name']} v{v['version_no']}")
                    st.rerun()


# ---------------------------------------------------------------------------

def render() -> None:
    auto_refresh()
    r = global_settings()
    target_banner(r)
    st.write("")
    with st.container(key="lanes"):
        cols = st.columns(5, gap="small")
        for i in range(1, 6):
            with cols[i - 1]:
                slot_card(i, r)
    st.write("")
    comparison(r)
    st.write("")
    ai_center()
