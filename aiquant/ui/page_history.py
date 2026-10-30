"""历史记录：任务、回测、模型调用（原始响应）、交易明细。"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from .. import db, jobs, research
from . import components as C
from .common import auto_refresh


def render() -> None:
    auto_refresh()
    st.markdown('<div class="aq-section"><span class="bar"></span>历史记录</div>', unsafe_allow_html=True)
    t1, t2, t3, t4 = st.tabs(["任务", "回测结果", "DeepSeek 调用", "交易明细"])
    with t1:
        j = pd.DataFrame(db.q("SELECT id,kind,slot_no,version_id,status,stage,error,batch_id,is_test,created_at,started_at,"
                              "finished_at,result_id FROM jobs ORDER BY id DESC LIMIT 500"))
        if not j.empty:
            j["status"] = j["status"].map(lambda s: jobs.STATUS_CN.get(s, s))
        st.dataframe(j, use_container_width=True, hide_index=True, height=420)
    with t2:
        b = db.q("SELECT id,job_id,version_id,slot_no,dataset_id,dataset_hash,window_start,window_end,signal_tf,aux_tf,"
                 "split_label,metrics_json,is_test,created_at,cost_basis FROM backtests ORDER BY id DESC LIMIT 500")
        rows = []
        from .. import spread_model
        for x in b:
            m = json.loads(x.pop("metrics_json"))
            cb = x.pop("cost_basis")
            x["成本口径"] = spread_model.basis_label(cb) + ("（与当前不同）" if spread_model.current() and spread_model.is_old(cb) else "")
            x.update({"净收益%": m.get("net_return_pct"), "最大回撤%": m.get("max_drawdown_pct"), "交易": m.get("trades"),
                      "总成本": m.get("total_cost")})
            x["dataset_hash"] = x["dataset_hash"][:12]
            rows.append(x)
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True, height=420)
    with t3:
        calls = db.q("SELECT id,kind,slot_no,version_id,backtest_id,model,status,error,created_at,finished_at,"
                     "raw_response_path,request_path FROM llm_calls ORDER BY id DESC LIMIT 200")
        st.dataframe(pd.DataFrame(calls).drop(columns=["raw_response_path", "request_path"], errors="ignore"),
                     use_container_width=True, hide_index=True, height=260)
        if calls:
            cid = st.selectbox("查看原始响应", [c["id"] for c in calls], key="h_call")
            c = next(x for x in calls if x["id"] == cid)
            cc = st.columns(2)
            for col, key, title in ((cc[0], "request_path", "请求"), (cc[1], "raw_response_path", "原始响应")):
                with col:
                    st.markdown(f"**{title}**")
                    p = c.get(key)
                    if p and Path(p).exists():
                        st.code(Path(p).read_text(encoding="utf-8")[:20000], language="json")
                    else:
                        st.caption("无")
    with t4:
        bts = db.q("SELECT id, slot_no, version_id, split_label, is_test FROM backtests ORDER BY id DESC LIMIT 200")
        if bts:
            bid = st.selectbox("选择回测", [x["id"] for x in bts], key="h_bt",
                               format_func=lambda i: next(f"#{x['id']} 策略位{x['slot_no']} 版本{x['version_id']} "
                                                          f"{x['split_label']}{' 测试数据' if x['is_test'] else ''}"
                                                          for x in bts if x["id"] == i))
            t = research.get_trades(bid)
            st.dataframe(t, use_container_width=True, hide_index=True, height=380)
            st.download_button("下载 CSV", t.to_csv(index=False).encode("utf-8-sig"), f"trades_{bid}.csv", "text/csv")
