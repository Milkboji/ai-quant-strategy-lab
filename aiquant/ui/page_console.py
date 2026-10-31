"""策略总台：每个策略一行卡片 —— 来源、状态、实验室版本 vs 运行版本、挑战者、持仓与资金权重；单个策略的部署 / 下线。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from .. import console
from ..strategy import registry
from . import components as C
from .common import flash, show_flash

BJ = timezone(timedelta(hours=8))
STATUS_TONE = {"草稿": "gray", "已回测": "blue", "已验证": "purple", "已部署": "blue", "运行中": "green",
               "暂停中": "orange", "已下线": "gray"}
TF_CN = {15: "15分钟", 30: "30分钟", 60: "1小时", 240: "4小时", 1440: "日线"}


def _bj(iso: str | None) -> str:
    if not iso:
        return "--"
    try:
        return pd.Timestamp(iso).tz_convert(BJ).strftime("%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return str(iso)[5:16]


def _vlabel(v: dict | None) -> str:
    return f"v{v['version_no']}" if v else "--"


def _price() -> float | None:
    try:
        from ..live.feed import FEED
        return float(FEED.snapshot()["mid"])
    except Exception:  # noqa: BLE001
        return None


def render() -> None:
    from .login import effective_user, role
    rows = console.rows(price=_price())
    eq = None
    try:
        snap = console.db.q1("SELECT equity FROM account_snapshots ORDER BY ts DESC LIMIT 1")
        eq = float(snap["equity"]) if snap else None
    except Exception:  # noqa: BLE001
        pass
    try:
        etf = console.etf_row(eq)
    except Exception:  # noqa: BLE001
        console.log.exception("etf row failed")
        etf = None
    st.markdown('<div class="aq-title">策略总台</div>', unsafe_allow_html=True)
    _rules_panel(effective_user(), role())
    show_flash("console")
    allr = rows + ([etf] if etf else [])
    n_run = sum(1 for r in allr if r["status"] in ("运行中", "暂停中", "等待空仓后下线"))
    n_mis = sum(1 for r in allr if r["mismatch"])
    n_ch = sum(1 for r in allr if r["challenger"])
    n_pend = len(console.awaiting())
    cells = [("策略总数", f"{len(allr)}", ""), ("模拟盘运行中", f"{n_run}", ""),
             ("版本不一致", f"{n_mis}", "color:#EA8A0C" if n_mis else ""), ("挑战者 / 待确认", f"{n_ch} / {n_pend}", "color:#7C3AED" if n_pend else "")]
    st.markdown('<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin:6px 0 10px">'
                + "".join(f'<div class="aq-m"><div class="l">{C.esc(l)}</div><div class="n" style="font-size:22px;{s}">'
                          f'{C.esc(v)}</div></div>' for l, v, s in cells) + "</div>", unsafe_allow_html=True)
    _queue_panel(effective_user(), role())
    _capital_panel(eq, effective_user(), role())
    _deploy_all_bar(effective_user(), role())
    focus = st.session_state.pop("console_focus", None)
    for r in rows:
        _card(r, effective_user(), role(), focus == r["key"])
    if etf:
        _etf_card(etf, effective_user(), role(), focus == "etf")
    _my_system_cards(effective_user(), role())
    _audit_panel()


def _deploy_all_bar(actor: str, rl: str) -> None:
    from ..paper import portfolio as pf
    with st.container(border=True):
        cols = st.columns([1.4, 3])
        if cols[0].button("一键部署全部", key="cs_deploy_all", type="primary", use_container_width=True):
            try:
                res = pf.deploy_all(actor=actor, role=rl)
                flash("console", "ok", " ｜ ".join(res))
            except Exception as e:  # noqa: BLE001
                flash("console", "err", str(e))
            st.rerun()
        cols[1].caption("部署所有未下线、尚未运行的策略；已在运行的策略只调整资金额度，保持原版本。"
                        "在本页下线的策略不会被重新部署。")


def _card(r: dict, actor: str, rl: str, focus: bool) -> None:
    lab, run, dep = r["lab"], r["run"], r["deployment"]
    with st.container(border=True):
        pills = C.pill(r["status"], STATUS_TONE.get(r["status"], "gray"))
        if r["mismatch"]:
            pills += " " + C.pill("版本不一致", "orange")
        if r["pending"]:
            pills += " " + C.pill("等待空仓后切换", "purple")
        if r["pending_offline"]:
            pills += " " + C.pill("等待空仓后下线", "orange")
        if r["lock"]:
            pills += " " + C.pill("已锁定", "blue")
        if r["awaiting"]:
            pills += " " + C.pill("挑战者胜出，等待确认", "purple")
        if focus:
            pills += " " + C.pill("刚从实验室跳转", "blue")
        if r.get("market") == "us_equity":
            pills += " " + C.pill(f"美股日线 · {r.get('trade_symbol') or ''}", "blue")
        if dep and r.get("trial"):
            pills += " " + C.pill(r["trial"], "orange" if r["trial"].startswith("试用") else "green")
        st.markdown(f'<div class="aq-slothead"><div><span class="aq-slotname">{C.esc(r["name"])}</span> '
                    f'<span class="aq-meta">{C.esc(r["origin"])} · 来源：{C.esc(r["source_cn"])}</span></div>'
                    f'<div>{pills}</div></div>', unsafe_allow_html=True)
        if dep:
            _scorecard_line(dep["id"])
        cols = st.columns([1.5, 1.2, 1.0, 1.4])
        with cols[0]:
            tf = r["signal_tf"]
            st.markdown(C.kv([("实验室", f"{_vlabel(lab)} · {registry.label(lab)}" if lab else "尚未保存版本"),
                              ("模拟盘", f"{_vlabel(run)} · {registry.label(run)}" if run else "未部署"),
                              ("K线周期", TF_CN.get(int(tf), f"{tf}分钟") if tf else "--")]),
                        unsafe_allow_html=True)
            if r["mismatch"]:
                side = ("实验室有更新的版本，尚未部署" if r["lab_newer"]
                        else "模拟盘已自动进化到更新的版本")
                st.markdown(f'<div class="aq-warn">版本不一致：实验室 {_vlabel(lab)} ≠ 模拟盘 {_vlabel(run)}。{side}</div>',
                            unsafe_allow_html=True)
            if r["pending"]:
                p = r["pending"]
                st.markdown(f'<div class="aq-sub">待切换到 {C.esc(registry.label(p["version"]))}'
                            f'（{C.esc(console.TRIGGER_CN.get(p["trigger"], p["trigger"]))}，{_bj(p["at"])} 提交）：'
                            '该策略空仓且没有未完成订单时生效</div>', unsafe_allow_html=True)
            if r["status"] == "草稿" and not lab and r["draft"]:
                st.caption("只有草稿，请先在策略实验室保存版本并回测")
        with cols[1]:
            ch = r["challenger"]
            if ch:
                prog = (f"{float(ch['days'] or 0):.1f} / {ch['need_days']} 天" if ch.get("need_days")
                        else "等待首次评估")
                fast = {"pass": "通过", "fail": "更差", "inconclusive": "证据不足，转前向"}.get(ch.get("fast"), "--")
                st.markdown(C.kv([("挑战者", f"#{ch['id']}"), ("改动", ch["change"] or "--"),
                                  ("前向进度", prog), ("快速检验", fast)]), unsafe_allow_html=True)
                cv, mv = ch.get("challenger") or {}, ch.get("champion") or {}
                if cv and mv:
                    st.caption(f"前向收益：挑战者 {cv.get('return_pct', 0):+.2f}% · 现役 {mv.get('return_pct', 0):+.2f}%")
            elif not r["awaiting"]:
                st.markdown(C.kv([("挑战者", "无")]), unsafe_allow_html=True)
            if r["awaiting"]:
                a = r["awaiting"]
                st.markdown(f'<div class="aq-sub" style="color:#6D28D9">挑战者 #{a["id"]} 胜出，等待确认：'
                            f'{C.esc(a["change"] or "")}。请在上方「待确认」里批准或拒绝。</div>', unsafe_allow_html=True)
        with cols[2]:
            unit = r.get("trade_symbol") if r.get("market") == "us_equity" else "BTC"
            qfmt = f"{r['qty']:,.4f} {unit}" if r.get("market") == "us_equity" else f"{r['qty']:.6f} BTC"
            st.markdown(C.kv([("持仓", qfmt if dep else "--"),
                              ("市值", C.usd(r["value"]) if dep and r["value"] is not None else "--"),
                              ("资金权重", C.pct(r["weight_pct"], sign=False, digits=1) if r["weight_pct"] is not None else "--"),
                              ("额度", C.usd(r["budget"]) if dep else "--")]), unsafe_allow_html=True)
        with cols[3]:
            _actions(r, actor, rl)
        if r.get("why"):
            why_block(r["why"], key=r["key"])
        if r["mismatch"] and r["diff"]:
            with st.expander(f"规则差异：模拟盘 {_vlabel(run)} → 实验室 {_vlabel(lab)}（{len(r['diff'])} 项）",
                             expanded=focus):
                st.dataframe(pd.DataFrame([{"项目": k, f"模拟盘 {_vlabel(run)}": a, f"实验室 {_vlabel(lab)}": b}
                                           for k, a, b in r["diff"]]), hide_index=True, use_container_width=True)


def _synced_toggle(container, label: str, db_on: bool, key: str, help_: str) -> bool:
    """锁定开关：数据库里的锁定状态在别处变了（例如手动部署后自动锁定 7 天），先把控件同步过来，
    只有用户真的拨动开关时才返回不同的值（10-31：部署后页面重跑，控件旧值把刚加的锁解除了）。"""
    seen = f"{key}__db"
    if st.session_state.get(seen) != db_on:
        st.session_state[key] = db_on
        st.session_state[seen] = db_on
    on = container.toggle(label, key=key, help=help_)
    if on != db_on:
        st.session_state[seen] = on      # 用户的改动随后写进数据库
    return on


def _lock_toggle(r: dict, actor: str, rl: str) -> None:
    key, lk = r["key"], r["lock"]
    if lk and lk.get("until"):
        why = ("手动部署后自动锁定" if lk.get("kind") == "manual_deploy" else "已锁定") + f"，至 {_bj(lk['until'])}"
    elif lk:
        why = f"{lk.get('by') or ''} 手动锁定，不会自动到期"
    else:
        why = "未锁定：挑战者胜出后按规则自动晋级" if not r["block"] else f"未锁定（{r['block']}）"
    on = _synced_toggle(st, "锁定版本", bool(lk), f"cs_lock_{key}", "锁定后，自动进化仍会生成并前向测试挑战者，"
                        "但胜出后不会自动替换，只进入待确认队列")
    st.caption(why)
    if on != bool(lk):
        try:
            res = console.set_lock(key, on, actor=actor, role=rl)
            flash("console", "ok", f"{r['name']}：{res['message']}")
        except Exception as e:  # noqa: BLE001
            flash("console", "err", f"{r['name']}：{e}")
        st.rerun()


def _actions(r: dict, actor: str, rl: str) -> None:
    key = r["key"]
    if r["deployment"]:
        _lock_toggle(r, actor, rl)
    vs = console.versions_of(key)
    if not vs:
        st.caption("还没有可部署的版本")
        return
    ids = [v["id"] for v in vs]
    pick_key = f"console_pick::{key}"
    want = st.session_state.get(pick_key) or (r["lab"] or {}).get("id")
    idx = ids.index(want) if want in ids else 0
    tags = {}
    for v in vs:
        t = []
        if r["lab"] and v["id"] == r["lab"]["id"]:
            t.append("实验室")
        if r["run"] and v["id"] == r["run"]["id"]:
            t.append("运行中")
        tags[v["id"]] = f"v{v['version_no']} · {console.SOURCE_CN.get(v['source'], v['source'])}" + \
            (f"（{'、'.join(t)}）" if t else "")
    vid = st.selectbox("选择版本", ids, index=idx, key=f"cs_v_{key}", format_func=lambda i: tags.get(i, str(i)))
    if st.button("部署此版本", key=f"cs_dep_{key}", type="primary", use_container_width=True):
        try:
            res = console.deploy_version(key, int(vid), actor=actor, role=rl)
            flash("console", "ok" if res["result"] != "等待空仓后切换" else "warn", f"{r['name']}：{res['message']}")
        except Exception as e:  # noqa: BLE001
            flash("console", "err", f"{r['name']}：{e}")
        st.session_state.pop(pick_key, None)
        st.rerun()
    if r["deployment"] and not r["pending_offline"]:
        conf = st.checkbox("确认下线", key=f"cs_offc_{key}",
                           help="空仓时立即停止；有持仓时先暂停新开仓，持仓按规则卖出后自动下线。不会主动平仓。")
        if st.button("下线", key=f"cs_off_{key}", use_container_width=True, disabled=not conf):
            try:
                res = console.take_offline(key, actor=actor, role=rl)
                flash("console", "ok" if res["result"] == "已下线" else "warn", f"{r['name']}：{res['message']}")
            except Exception as e:  # noqa: BLE001
                flash("console", "err", f"{r['name']}：{e}")
            st.rerun()


def _my_system_cards(actor: str, rl: str) -> None:
    """我的交易系统 v1：每个市场一张卡片（状态、试用期、各品种分批止损、为什么）+ 一个参数面板（改参数 → 冻结新版本 → 部署）。"""
    from .. import allocation, my_system as S
    from ..engine import my_system as ms
    from ..paper import my_system_exec as mx
    for mkey, mname in (("crypto", "加密货币"), ("us_equity", "美股")):
        deps = S.deployments(mkey)
        dep = deps[0] if deps else None
        vers = S.versions(mkey)
        run_v = S.get_version(dep["version_id"]) if dep else None
        base = run_v or (vers[0] if vers else None)
        params = ms.Params.from_dict(base["params"] if base else S.default_params(mkey))
        with st.container(border=True):
            pills = C.pill({"running": "运行中", "paused_entries": "暂停中"}.get(dep["status"], dep["status"]) if dep else "未部署",
                           "green" if dep and dep["status"] == "running" else "gray") + " " + C.pill(f"{mname} · 日线", "blue")
            if dep:
                ts = allocation.trial_status(dep)
                pills += " " + C.pill(ts["text"], "orange" if ts["trial"] else "green")
                if dep["config"].get("pending_switch"):
                    pills += " " + C.pill("等待空仓后切换", "purple")
                if dep["config"].get("pending_offline"):
                    pills += " " + C.pill("等待空仓后下线", "orange")
            st.markdown(f'<div class="aq-slothead"><div><span class="aq-slotname">{C.esc(S.NAME)} v1（{mname}）</span> '
                        f'<span class="aq-meta">{C.esc("、".join(S.universe(mkey)))} · 只做多 · 分批建仓</span></div>'
                        f'<div>{pills}</div></div>', unsafe_allow_html=True)
            if dep:
                snap = mx.snapshot(dep)
                rows = [("模拟盘版本", f"v{run_v['version_no']}" if run_v else "--"),
                        ("额度", C.usd(dep["config"].get("budget_usd")) + "（试用期从现金层出）"),
                        ("台账持仓", "；".join(f"{k} {v:.6g}" for k, v in snap["positions"].items()) or "空仓")]
                for sym, e in (snap["engine"] or {}).items():
                    rows.append((sym, "；".join(f"{t['kind']} 止损 {t['stop']:,.4g}{'（保本）' if t['breakeven'] else ''}"
                                                for t in e["tranches"]) or "--"))
                st.markdown(C.kv(rows), unsafe_allow_html=True)
                why = snap.get("why")
                if why:
                    st.caption(f"为什么：{why['text']}")
            else:
                st.caption("还没有部署。研究结论和部署条件见 docs/my_system.md；下面的参数面板可以冻结版本并部署（进入试用期）。")
            with st.expander("规则（大白话）"):
                st.markdown("\n\n".join(C.esc(x) for x in ms.describe(params, mkey, S.universe(mkey))))
            with st.expander("参数面板（改完冻结为新版本并部署；持仓中则空仓后切换）"):
                cols = st.columns(4)
                new = {}
                for i, (k, (name, unit, (lo, hi))) in enumerate(ms.PARAM_LABELS.items()):
                    cur = getattr(params, k)
                    step = 1 if isinstance(cur, int) else 0.5
                    new[k] = cols[i % 4].number_input(f"{name}（{unit}）", min_value=type(cur)(lo), max_value=type(cur)(hi),
                                                      value=cur, step=type(cur)(step), key=f"ms_{mkey}_{k}",
                                                      disabled=rl not in console.EDIT_ROLES)
                if st.button("冻结为新版本并部署", key=f"ms_save_{mkey}", type="primary",
                             disabled=rl not in console.EDIT_ROLES):
                    try:
                        console.require_role(rl)
                        v = S.freeze(mkey, new, actor=actor, note="策略总台参数面板")
                        flash("console", "ok", S.deploy(v["id"], actor=actor, role=rl)["message"])
                    except Exception as e:  # noqa: BLE001
                        flash("console", "err", str(e))
                    st.rerun()
                if vers:
                    st.caption("版本：" + "；".join(f"v{v['version_no']}（{v['created_at'][:10]}，{v['created_by'] or '--'}）"
                                                     for v in vers[:5]))
            if dep and st.button("下线", key=f"ms_stop_{mkey}", disabled=rl not in console.EDIT_ROLES):
                try:
                    flash("console", "ok", S.stop(dep["id"], actor=actor, role=rl))
                except Exception as e:  # noqa: BLE001
                    flash("console", "err", str(e))
                st.rerun()


def _scorecard_line(did: int) -> None:
    """策略成绩单摘要（每天重新回测实际运行区间、逐笔对比模拟盘）：一致 / 偏差原因分类 / 样本不足。"""
    from .. import scorecard
    try:
        cs = scorecard.card_summary(did)
    except Exception:  # noqa: BLE001
        cs = None
    if not cs:
        st.markdown(f'<div class="aq-meta">{C.pill("成绩单：明天早上 6 点后生成", "gray")}</div>', unsafe_allow_html=True)
        return
    det = f' <span class="aq-meta">{C.esc(cs["detail"])}</span>' if cs.get("detail") else ""
    st.markdown(f'<div>{C.pill(cs["text"], cs["tone"])}{det}</div>', unsafe_allow_html=True)


def _etf_card(r: dict, actor: str, rl: str, focus: bool) -> None:
    """美股 ETF 择时卡片：与 BTC 策略同样的版本 / 锁定 / 待确认 / 上下线 / 审计，参数版本来自策略实验室。"""
    from ..paper import etf_versions as ev
    run, lab = r["run"], r["lab"]
    tone = {"运行中": "green", "暂停中": "orange", "等待空仓后下线": "orange", "已下线": "gray"}
    with st.container(border=True):
        pills = C.pill(r["status"], tone.get(r["status"], "gray")) + " " + C.pill("美股 · 日线", "blue")
        if r["mismatch"]:
            pills += " " + C.pill("版本不一致", "orange")
        if r["lock"]:
            pills += " " + C.pill("已锁定", "blue")
        if r["awaiting"]:
            pills += " " + C.pill("挑战者胜出，等待确认", "purple")
        if r.get("exposed"):
            pills += " " + C.pill("验证/测试数据已暴露", "orange")
        if focus:
            pills += " " + C.pill("刚从实验室跳转", "blue")
        pills += " " + C.pill("正式", "green")
        st.markdown(f'<div class="aq-slothead"><div><span class="aq-slotname">{C.esc(r["name"])}</span> '
                    f'<span class="aq-meta">{C.esc(r["signal_symbol"])} → {C.esc(r["trade_symbol"])} · 来源：策略实验室参数版本'
                    f'</span></div><div>{pills}</div></div>',
                    unsafe_allow_html=True)
        try:
            from ..paper import us_exec
            _ed = us_exec.etf_deployment()
            if _ed:
                _scorecard_line(_ed["id"])
        except Exception:  # noqa: BLE001
            pass
        cols = st.columns([1.5, 1.2, 1.0, 1.4])
        with cols[0]:
            st.markdown(C.kv([("实验室最新", ev.label(lab) if lab else "--"), ("模拟盘", ev.label(run)),
                              ("K线周期", "日线（美东收盘）")]), unsafe_allow_html=True)
            if r["mismatch"]:
                st.markdown(f'<div class="aq-warn">版本不一致：实验室 v{lab["version_no"]} ≠ 模拟盘 v{run["version_no"]}。'
                            "实验室有更新的版本，尚未部署</div>", unsafe_allow_html=True)
        with cols[1]:
            ch = r["challenger"]
            if ch:
                prog = (f"{int(ch['days'] or 0)} / {ch['need_days']} 个交易日" if ch.get("need_days") else "等待首次评估")
                fast = {"pass": "通过", "fail": "更差", "inconclusive": "证据不足，转前向"}.get(ch.get("fast"), "--")
                st.markdown(C.kv([("挑战者", f"#{ch['id']}"), ("改动", ch["change"] or "--"), ("前向进度", prog),
                                  ("快速检验", fast)]), unsafe_allow_html=True)
                if ch.get("split_note"):
                    st.caption(ch["split_note"])
            elif not r["awaiting"]:
                st.markdown(C.kv([("挑战者", "无")]), unsafe_allow_html=True)
            if r["awaiting"]:
                a = r["awaiting"]
                st.markdown(f'<div class="aq-sub" style="color:#6D28D9">挑战者 #{a["id"]} 胜出，等待确认：'
                            f'{C.esc(a["change"] or "")}。请在上方「待确认」里批准或拒绝。</div>', unsafe_allow_html=True)
        with cols[2]:
            st.markdown(C.kv([("持仓", f"{r['qty']:,.4f} {r['trade_symbol']}" if r["value"] is not None else "--"),
                              ("市值", C.usd(r["value"]) if r["value"] is not None else "--"),
                              ("浮动盈亏", f"{r['unrealized']:+,.0f}" if r["unrealized"] is not None else "--"),
                              ("资金比例", f"{r['budget_pct']:g}%（固定）")]), unsafe_allow_html=True)
        with cols[3]:
            lk = r["lock"]
            if lk and lk.get("until"):
                why = ("手动部署后自动锁定" if lk.get("kind") == "manual_deploy" else "已锁定") + f"，至 {_bj(lk['until'])}"
            elif lk:
                why = f"{lk.get('by') or ''} 手动锁定，不会自动到期"
            else:
                why = "未锁定：挑战者胜出后按规则自动晋级" if not r["block"] else f"未锁定（{r['block']}）"
            on = _synced_toggle(st, "锁定版本", bool(lk), "cs_lock_etf", "锁定后，自动进化仍会生成并前向测试挑战者，"
                                "但胜出后不会自动替换，只进入待确认队列")
            st.caption(why)
            if on != bool(lk):
                try:
                    res = ev.set_lock(on, actor=actor, role=rl)
                    flash("console", "ok", f"{r['name']}：{res['message']}")
                except Exception as e:  # noqa: BLE001
                    flash("console", "err", f"{r['name']}：{e}")
                st.rerun()
            vs = r["versions"]
            if vs:
                ids = [v["id"] for v in vs]
                want = (lab or {}).get("id")
                tags = {v["id"]: f"v{v['version_no']} · {v['sma']} 日 ±{float(v['band']):.0%}"
                        + ("（运行中）" if v["id"] == run["id"] else "") for v in vs}
                vid = st.selectbox("选择版本", ids, index=ids.index(want) if want in ids else 0, key="cs_v_etf",
                                   format_func=lambda i: tags.get(i, str(i)))
                if st.button("部署此版本", key="cs_dep_etf", type="primary", use_container_width=True):
                    try:
                        res = ev.deploy(int(vid), confirmed=True, actor=actor, role=rl)
                        flash("console", "ok", f"{r['name']}：{res['message']}")
                    except Exception as e:  # noqa: BLE001
                        flash("console", "err", f"{r['name']}：{e}")
                    st.rerun()
            if r["enabled"] and not r["pending_offline"]:
                conf = st.checkbox("确认下线", key="cs_offc_etf",
                                   help="空仓时立即下线；有持仓时先停止新开仓，按信号卖出后自动下线。不会主动平仓。")
                if st.button("下线", key="cs_off_etf", use_container_width=True, disabled=not conf):
                    try:
                        res = ev.set_online(False, actor=actor, role=rl)
                        flash("console", "ok" if res["result"] == "已下线" else "warn", f"{r['name']}：{res['message']}")
                    except Exception as e:  # noqa: BLE001
                        flash("console", "err", f"{r['name']}：{e}")
                    st.rerun()
            elif not r["enabled"] or r["pending_offline"]:
                if st.button("重新上线", key="cs_on_etf", use_container_width=True):
                    try:
                        res = ev.set_online(True, actor=actor, role=rl)
                        flash("console", "ok", f"{r['name']}：{res['message']}")
                    except Exception as e:  # noqa: BLE001
                        flash("console", "err", f"{r['name']}：{e}")
                    st.rerun()
        if r.get("deployment_id"):
            st.caption(f"运行在通用美股日线执行器上：部署 #{r['deployment_id']}（与实验室美股版本同一套执行规则、分账台账）")
        with st.expander(f"美股目标比例：{r['budget_pct']:g}%（资金第一层；美股内部按实力公式分配）"):
            c2 = st.columns([1, 1, 2])
            pct = c2[0].number_input("资金比例 %", min_value=ev.BUDGET_RANGE[0], max_value=ev.BUDGET_RANGE[1],
                                     value=float(r["budget_pct"]), step=5.0, key="cs_pct_etf")
            if c2[1].button("保存资金比例", key="cs_pct_etf_save", use_container_width=True):
                try:
                    res = ev.set_budget_pct(pct, actor=actor, role=rl)
                    flash("console", "ok", f"{r['name']}：{res['message']}")
                except Exception as e:  # noqa: BLE001
                    flash("console", "err", f"{r['name']}：{e}")
                st.rerun()
            c2[2].caption("美股目标比例（第一层）。持仓偏离目标超过再平衡阈值时，收盘后记下、下一个交易日开盘后调整。"
                          "BTC 策略可分配的资金 = 总权益 − 预留 − max(美股目标 − 借出部分, 美股持仓市值占比)。")
            c3 = st.columns([1, 1, 2])
            lp = c3[0].number_input("空仓时借给 BTC 的比例 %", min_value=0.0, max_value=100.0,
                                    value=float(r.get("lend_pct", 50.0)), step=10.0, key="cs_lend_etf")
            if c3[1].button("保存借出比例", key="cs_lend_etf_save", use_container_width=True):
                try:
                    res = ev.set_lend_pct(lp, actor=actor, role=rl)
                    flash("console", "ok", f"{r['name']}：{res['message']}")
                except Exception as e:  # noqa: BLE001
                    flash("console", "err", f"{r['name']}：{e}")
                st.rerun()
            c3[2].caption("美股空仓（看空）时，BTC 最多临时使用这部分额度；美股发出买入信号时收回：BTC 暂停新开仓，"
                          "持仓按规则卖出、现金够买后美股买入，额度归还。全过程写审计。"
                          f"当前状态：{ {'none': '未借出', 'lent': '已借出', 'reclaim': '正在收回'}.get(r.get('lend_state'), '--') }。")
        if r.get("exposed"):
            st.markdown(f'<div class="aq-warn">验证/测试数据已暴露：{C.esc(r["exposed"]["reason"])}。这条谱系的验证检验只作参考、'
                        f'不能作为晋级依据；留出检验改用上线日 {C.esc(r["exposed"]["forward_from"])} 之后的前向数据。'
                        f'验证集计次 {r["validation_used"]}/3（与策略实验室同一套计次）。</div>', unsafe_allow_html=True)
        why_block(r["why"], key="etf")
        if r["mismatch"] and r["diff"]:
            with st.expander(f"参数差异：模拟盘 v{run['version_no']} → 实验室 v{lab['version_no']}（{len(r['diff'])} 项）",
                             expanded=focus):
                st.dataframe(pd.DataFrame([{"项目": k, f"模拟盘 v{run['version_no']}": a, f"实验室 v{lab['version_no']}": b}
                                           for k, a, b in r["diff"]]), hide_index=True, use_container_width=True)


def _capital_panel(eq: float | None, actor: str, rl: str) -> None:
    """资金两层分配：第一层（市场之间的上下限）与第二层（市场内部按实力公式）的计算过程，以及第一层设置。"""
    from .. import allocation
    try:
        ex = allocation.explain(eq or 0)
    except Exception as e:  # noqa: BLE001
        console.log.exception("capital explain failed")
        st.caption(f"资金分配读取失败：{e}")
        return
    l1 = ex["layer1"]
    with st.expander(f"资金两层分配：加密货币 {l1['crypto_pct']:.1f}% · 美股 {l1['us_deduct_pct']:.1f}% · "
                     f"现金 {l1['cash_pct']:.1f}%（账户权益 {C.usd(eq) if eq else '--'}）"):
        st.markdown("**第一层：市场之间（上下限可配置）**")
        st.markdown("".join(f"<div>• {C.esc(x)}</div>" for x in l1["steps"]), unsafe_allow_html=True)
        if l1.get("trial_hint"):
            st.info(l1["trial_hint"])
        st.markdown("**第二层：市场内部按实力公式**（只在「正式」策略之间分配：长周期 年均收益 / 年均回撤，没有长周期结果时用训练集 "
                    "收益 / 回撤；权重有上下限）。新部署的策略先「试用」：固定额度从现金层出，满期后转正式")
        if ex["us"]:
            st.dataframe(pd.DataFrame([{"美股部署": f"#{x['deployment']} {x['label']}", "状态": x.get("status") or "--",
                                        "额度": C.usd(x["budget"]),
                                        "美股内占比": C.pct(x["share_pct"], sign=False, digits=1) if x["share_pct"] is not None else "--",
                                        "目标 / 实际（占权益）": (f"{x['target_pct']:.1f}% / {x['actual_pct']:.1f}%"
                                                         if x["target_pct"] is not None else "--"),
                                        "依据": x["why"]} for x in ex["us"]]), hide_index=True, use_container_width=True)
        if ex["crypto"]:
            st.dataframe(pd.DataFrame([{"加密货币部署": f"#{x['deployment']} {x['label']}", "状态": x.get("status") or "--",
                                        "额度": C.usd(x["budget"]),
                                        "加密内占比": C.pct(x["share_pct"], sign=False, digits=1) if x.get("share_pct") is not None else "--",
                                        "依据": x["why"]} for x in ex["crypto"]]), hide_index=True, use_container_width=True)
        st.caption(f"额度调整不强制卖出已有持仓，新的目标在下一次开仓时生效；美股持仓中目标比例提高 {l1['add_pp']:g} 个百分点以上时，"
                   "下一个交易日按差额加仓（只加不减，价格涨跌不调整）。"
                   f"单个策略持仓超过账户权益的 {l1['position_cap_pct']:g}% 时减到上限（美股和 BTC 都适用）。")
        c = allocation.get_config()
        cols = st.columns(3)
        us_min = cols[0].number_input("美股下限 %", 0.0, 100.0, float(c["us_equity"]["min"]), 5.0, key="cap_us_min")
        us_max = cols[1].number_input("美股上限 %", 0.0, 100.0, float(c["us_equity"]["max"]), 5.0, key="cap_us_max")
        app = cols[2].number_input("加仓阈值（百分点）", 0.5, 50.0, float(c["add_pp"]), 0.5, key="cap_add_pp")
        cols = st.columns(3)
        cr_min = cols[0].number_input("加密货币下限 %", 0.0, 100.0, float(c["crypto"]["min"]), 5.0, key="cap_cr_min")
        cr_max = cols[1].number_input("加密货币上限 %", 0.0, 100.0, float(c["crypto"]["max"]), 5.0, key="cap_cr_max")
        pcap = cols[2].number_input("单个策略持仓上限 %", 5.0, 100.0, float(c["position_cap_pct"]), 5.0, key="cap_pos")
        cols = st.columns(3)
        tp = cols[0].number_input("试用额度（每个）%", 0.0, 20.0, float(c["trial_pct"]), 1.0, key="cap_trial")
        tt = cols[1].number_input("试用额度合计上限 %", 0.0, 50.0, float(c["trial_total_pct"]), 5.0, key="cap_trial_total")
        td = cols[2].number_input("试用期（交易日 / 天）", 1, 365, int(c["trial_days"]), 5, key="cap_trial_days")
        if st.button("保存资金分层", key="cap_save", use_container_width=True):
            try:
                res = allocation.set_config(actor=actor, role=rl, us_min=us_min, us_max=us_max, crypto_min=cr_min,
                                            crypto_max=cr_max, add_pp=app, position_cap_pct=pcap, trial_pct=tp,
                                            trial_total_pct=tt, trial_days=td)
                flash("console", "ok", res["message"])
            except Exception as e:  # noqa: BLE001
                flash("console", "err", str(e))
            st.rerun()


def _audit_panel() -> None:
    log = console.audit_log(60)
    with st.expander(f"部署 / 下线审计日志（最近 {len(log)} 条）"):
        if not log:
            st.caption("还没有记录")
            return
        lab = {}

        def vl(i):
            if not i:
                return "--"
            if i not in lab:
                lab[i] = registry.label(registry.get_version(i))
            return lab[i]
        st.dataframe(pd.DataFrame([{"时间（北京）": _bj(x["ts"]), "操作人": x["actor"], "操作": x["action"],
                                    "策略": x["strategy_name"] or x["strategy_key"] or "--",
                                    "部署": f"#{x['deployment_id']}" if x["deployment_id"] else "--",
                                    "原版本": vl(x["from_version_id"]), "新版本": vl(x["to_version_id"]),
                                    "结果": x["result"], "说明": x["detail"] or ""} for x in log]),
                     hide_index=True, use_container_width=True)
        st.caption(f"更新时间 {datetime.now(BJ):%m-%d %H:%M} 北京时间")


def _rules_panel(actor: str, rl: str) -> None:
    pc = console.promotion_config()
    days = float(pc["manual_lock_days"])
    rc = bool(pc["require_confirm"])
    lock_txt = (f"你手动部署某个版本后，这个策略会自动锁定 <b>{days:g} 天</b>" if days > 0
                else "手动部署后不自动锁定（锁定天数设为 0）")
    rules = [
        "<b>你说了算</b>：实验室保存新版本不会替换模拟盘正在跑的版本，只有你在这里点「部署此版本」才会换。",
        f"<b>手动部署优先</b>：{lock_txt}，期间自动进化不会把它换掉。你也可以随时打开或关闭每个策略的「锁定版本」。"
        "美股 ETF 在策略实验室部署新版本后同样锁定。",
        "<b>锁定时</b>：自动进化照常找更好的版本、照常让挑战者在实时行情上跑前向测试；但挑战者赢了也不会自动上线，"
        "只标记为「挑战者胜出，等待确认」，由你批准或拒绝。",
        "<b>没锁定时</b>：维持原来的规则，挑战者通过检验后自动晋级。" + " " +
        ("<b>当前已开启「晋级需人工确认」：所有策略（包括美股 ETF）的晋级都要你确认。</b>" if rc
         else "如果开启「晋级需人工确认」，所有策略（包括美股 ETF）的晋级都要你确认。"),
        "<b>批准 / 拒绝</b>：批准后按正常流程上线；拒绝的挑战者会被淘汰并记下原因。现役版本换过之后，旧的待确认挑战者自动作废。",
        "<b>何时生效</b>：任何换版本（手动部署、批准晋级、自动晋级）和下线，都只在这个策略空仓、没有未完成订单时生效，"
        "否则先等待空仓。",
    ]
    with st.container(border=True):
        C.section("晋级与部署规则", purple=True,
                  right=C.pill("晋级需人工确认：开" if rc else "自动晋级（锁定的策略除外）", "purple" if rc else "gray"))
        st.markdown('<ol class="aq-sub" style="margin:2px 0 6px 18px;padding:0;line-height:1.75">'
                    + "".join(f"<li>{x}</li>" for x in rules) + "</ol>", unsafe_allow_html=True)
        c = st.columns([1.2, 1, 0.8, 2])
        new_rc = c[0].toggle("晋级需人工确认", value=rc, key="cs_rc")
        new_days = c[1].number_input("手动部署后锁定天数", min_value=0, max_value=365, value=int(round(days)), step=1,
                                     key="cs_days", help="0 = 手动部署后不自动锁定")
        if c[2].button("保存规则", key="cs_rules_save", use_container_width=True):
            try:
                console.set_promotion_config(actor=actor, role=rl, require_confirm=new_rc, manual_lock_days=new_days)
                flash("console", "ok", "规则已保存")
            except Exception as e:  # noqa: BLE001
                flash("console", "err", str(e))
            st.rerun()
        c[3].caption("「晋级需人工确认」与策略进化页的同名开关是同一个设置。")


def _queue_panel(actor: str, rl: str) -> None:
    q = console.awaiting()
    if not q:
        return
    with st.container(border=True):
        C.section(f"待确认：{len(q)} 个胜出的挑战者", purple=True)
        for ch in q:
            cols = st.columns([3, 1.6])
            ev = ch.get("last_eval") or {}
            cv, mv = ev.get("challenger") or {}, ev.get("champion") or {}
            extra = (f"前向：挑战者 {cv.get('return_pct', 0):+.2f}% · 现役 {mv.get('return_pct', 0):+.2f}%"
                     if cv and mv else "")
            name = "美股 ETF 择时" if ch["kind"] == "etf" else (ch["label"] or f"部署 #{ch['deployment_id']}")
            cols[0].markdown(f'<div><b>#{ch["id"]} · {C.esc(name)}</b> <span class="aq-meta">{_bj(ch["decided_at"])}</span></div>'
                             f'<div class="aq-sub">改动：{C.esc(ch["change"] or "--")}</div>'
                             f'<div class="aq-sub">{C.esc(ch["reason"] or "")}</div>'
                             + (f'<div class="aq-sub">{C.esc(extra)}</div>' if extra else ""), unsafe_allow_html=True)
            with cols[1]:
                b = st.columns(2)
                if b[0].button("批准", key=f"cs_ap_{ch['id']}", type="primary", use_container_width=True):
                    try:
                        res = console.approve(ch["id"], actor=actor, role=rl)
                        flash("console", "ok", res["message"])
                    except Exception as e:  # noqa: BLE001
                        flash("console", "err", str(e))
                    st.rerun()
                reason = st.text_input("拒绝原因", key=f"cs_rr_{ch['id']}", placeholder="例如：回撤太大")
                if b[1].button("拒绝", key=f"cs_rj_{ch['id']}", use_container_width=True):
                    try:
                        res = console.reject(ch["id"], reason, actor=actor, role=rl)
                        flash("console", "ok", res["message"])
                    except Exception as e:  # noqa: BLE001
                        flash("console", "err", str(e))
                    st.rerun()


def why_block(w: dict, key: str = "") -> None:
    """“为什么没有动作”：一句话结论 + 原因明细 + 计算明细（资金权重、波动率定仓、当前仓位）。"""
    if not w:
        return
    st.markdown(f'<div class="aq-sub" style="margin-top:4px"><b>为什么没有动作：</b>{C.esc(w["headline"])}</div>',
                unsafe_allow_html=True)
    with st.expander("原因与计算明细"):
        for t, x in w["items"]:
            st.markdown(f'<div class="aq-sub" style="margin:3px 0;overflow-wrap:anywhere"><b>{C.esc(t)}</b>：{C.esc(x)}</div>',
                        unsafe_allow_html=True)
        if w["calc"]:
            st.markdown('<div class="aq-sub" style="margin-top:6px"><b>计算明细</b></div>', unsafe_allow_html=True)
            for t, x in w["calc"]:
                st.markdown(f'<div class="aq-sub" style="margin:3px 0;overflow-wrap:anywhere">• <b>{C.esc(t)}</b>：{C.esc(x)}</div>',
                            unsafe_allow_html=True)
