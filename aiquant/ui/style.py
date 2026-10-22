"""浅色中文主题样式（参考用户提供的浅色系长图）。"""
import streamlit as st

CSS = """
<style>
:root{
  --aq-bg:#F4F7FB; --aq-card:#FFFFFF; --aq-border:#E3E8F0; --aq-text:#1F2937; --aq-muted:#6B7280;
  --aq-blue:#2563EB; --aq-blue-50:#EFF4FF; --aq-purple:#7C3AED; --aq-purple-50:#F3EEFF;
  --aq-green:#16A34A; --aq-green-50:#ECFDF3; --aq-red:#DC2626; --aq-red-50:#FEF2F2;
  --aq-orange:#EA8A0C; --aq-orange-50:#FFF7E8; --aq-gray-50:#F3F4F6;
}
html, body, [data-testid="stAppViewContainer"], .stApp { background: var(--aq-bg) !important; color: var(--aq-text); }
[data-testid="stHeader"]{ background: rgba(255,255,255,.92); border-bottom:1px solid var(--aq-border); }
.block-container{ padding-top: 3.6rem !important; padding-left: 1.4rem !important; padding-right: 1.4rem !important; max-width: 100% !important; }
h1,h2,h3,h4{ color: var(--aq-text); letter-spacing: 0; }

/* 卡片：st.container(border=True) */
[data-testid="stVerticalBlockBorderWrapper"]{ background: var(--aq-card); border:1px solid var(--aq-border) !important;
  border-radius: 10px !important; box-shadow: 0 1px 3px rgba(15,23,42,.05); }

/* 统一输入控件尺寸 */
.stTextInput input, .stNumberInput input, .stDateInput input { height: 36px; font-size: 13px; }
div[data-baseweb="select"] > div { min-height: 36px; font-size: 13px; }
.stTextArea textarea { font-size: 13px; line-height: 1.55; min-height: 120px; }
label, .stMarkdown p { font-size: 13px; }
[data-testid="stWidgetLabel"] p { font-size: 12px !important; color: var(--aq-muted) !important; }

/* 按钮 */
.stButton > button, .stDownloadButton > button { border-radius: 7px; height: 34px; font-size: 13px; padding: 0 10px;
  border:1px solid var(--aq-border); background:#fff; color: var(--aq-text); width: 100%; white-space: nowrap; }
.stButton > button:hover, .stDownloadButton > button:hover { border-color: var(--aq-blue); color: var(--aq-blue); }
.stButton > button[kind="primary"]{ background: var(--aq-blue); border-color: var(--aq-blue); color:#fff; }
.stButton > button[kind="primary"]:hover{ background:#1D4ED8; color:#fff; }
[class*="st-key-ai_"] button{ background: var(--aq-purple-50) !important; color: var(--aq-purple) !important; border-color:#D9C8FF !important; }
[class*="st-key-ai_"] button[kind="primary"]{ background: var(--aq-purple) !important; color:#fff !important; border-color: var(--aq-purple) !important; }
[class*="st-key-deploy_"] button{ background: var(--aq-green-50) !important; color: var(--aq-green) !important; border-color:#BBF7D0 !important; }
[class*="st-key-danger_"] button{ background: var(--aq-red-50) !important; color: var(--aq-red) !important; border-color:#FECACA !important; }
[class*="st-key-warn_"] button{ background: var(--aq-orange-50) !important; color: var(--aq-orange) !important; border-color:#FDE3B5 !important; }
.stButton > button:disabled{ opacity: .55; }

/* Tabs */
.stTabs [data-baseweb="tab-list"]{ gap: 2px; border-bottom:1px solid var(--aq-border); }
.stTabs [data-baseweb="tab"]{ height: 32px; padding: 0 10px; font-size: 13px; }

/* 自定义元素 */
.aq-brand{ display:flex; align-items:center; gap:10px; }
.aq-logo{ width:30px; height:30px; border-radius:50%; background: radial-gradient(circle at 35% 35%, #60A5FA, #2563EB 60%, #1E40AF);
  box-shadow: 0 0 0 3px #DBEAFE; }
.aq-title{ font-size:20px; font-weight:700; color:#1E3A8A; }
.aq-ver{ font-size:11px; color:var(--aq-muted); border:1px solid var(--aq-border); border-radius:6px; padding:1px 6px; margin-left:4px; }
.aq-sub{ font-size:12px; color:var(--aq-muted); }
.aq-pill{ display:inline-block; font-size:12px; padding:2px 9px; border-radius:999px; font-weight:600; white-space:nowrap; }
.aq-pill.green{ background:var(--aq-green-50); color:var(--aq-green); }
.aq-pill.red{ background:var(--aq-red-50); color:var(--aq-red); }
.aq-pill.blue{ background:var(--aq-blue-50); color:var(--aq-blue); }
.aq-pill.purple{ background:var(--aq-purple-50); color:var(--aq-purple); }
.aq-pill.orange{ background:var(--aq-orange-50); color:var(--aq-orange); }
.aq-pill.gray{ background:var(--aq-gray-50); color:var(--aq-muted); }
.aq-section{ font-size:16px; font-weight:700; color:var(--aq-text); display:flex; align-items:center; gap:8px; margin: 2px 0 6px; }
.aq-section .bar{ width:4px; height:16px; background:var(--aq-blue); border-radius:2px; display:inline-block; }
.aq-section .bar.purple{ background:var(--aq-purple); }
.aq-slothead{ display:flex; align-items:center; justify-content:space-between; gap:6px; }
.aq-num{ display:inline-flex; align-items:center; justify-content:center; width:24px; height:24px; border-radius:6px;
  background:var(--aq-blue); color:#fff; font-weight:700; font-size:13px; }
.aq-slotname{ font-weight:700; font-size:15px; margin-left:6px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width: 170px; display:inline-block; vertical-align:middle;}
.aq-meta{ font-size:12px; color:var(--aq-muted); }
.aq-kv{ display:grid; grid-template-columns: 62px 1fr; gap:4px 8px; font-size:12px; margin: 4px 0; }
.aq-kv .k{ color:var(--aq-muted); }
.aq-kv .v{ background:#F8FAFC; border:1px solid var(--aq-border); border-radius:5px; padding:2px 6px; color:var(--aq-text);
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.aq-metrics{ display:grid; grid-template-columns: repeat(3, 1fr); gap:8px 6px; margin-top:4px; }
.aq-m .l{ font-size:11px; color:var(--aq-muted); }
.aq-m .n{ font-size:17px; font-weight:700; line-height:1.25; }
.aq-m .s{ font-size:11px; }
.aq-m .n.sm{ font-size:14px; }
.pos{ color:var(--aq-green) !important; } .neg{ color:var(--aq-red) !important; } .muted{ color:var(--aq-muted) !important; }
.aq-banner{ background: linear-gradient(90deg, #EEF4FF, #F7F9FF); border:1px solid #DCE6FB; border-radius:10px; padding:9px 14px;
  font-size:13px; display:flex; flex-wrap:wrap; gap:6px 18px; align-items:center; }
.aq-banner b{ color:#1E3A8A; }
.aq-warn{ background: var(--aq-orange-50); border:1px solid #FDE3B5; color:#9A5B05; border-radius:8px; padding:7px 11px; font-size:12px; }
.aq-err{ background: var(--aq-red-50); border:1px solid #FECACA; color:#991B1B; border-radius:8px; padding:7px 11px; font-size:12px; }
.aq-ok{ background: var(--aq-green-50); border:1px solid #BBF7D0; color:#166534; border-radius:8px; padding:7px 11px; font-size:12px; }
.aq-placeholder{ height:150px; border:1px dashed var(--aq-border); border-radius:8px; display:flex; align-items:center;
  justify-content:center; color:var(--aq-muted); font-size:12px; background:#FAFBFD; text-align:center; padding: 0 8px; }
.aq-big{ font-size:34px; font-weight:800; }
.aq-ai-li{ font-size:12px; margin: 2px 0; }
.aq-tag{ font-size:11px; color:var(--aq-muted); background:var(--aq-gray-50); border-radius:4px; padding:1px 5px; margin-right:4px; }
.aq-step{ display:flex; gap:14px; flex-wrap:wrap; font-size:13px; }
.aq-step span{ display:inline-flex; align-items:center; gap:6px; color:var(--aq-muted); }
.aq-step span i{ font-style:normal; width:22px; height:22px; border-radius:50%; background:var(--aq-gray-50); display:inline-flex;
  align-items:center; justify-content:center; font-weight:700; }
.aq-step span.on{ color:var(--aq-blue); font-weight:600; } .aq-step span.on i{ background:var(--aq-blue); color:#fff; }
[data-testid="stDataFrame"]{ font-size: 12px; }
/* 窄屏：五策略横向滚动，保证可读 */
@media (max-width: 1500px){
  .st-key-lanes [data-testid="stHorizontalBlock"]{ overflow-x:auto; flex-wrap:nowrap !important; }
  .st-key-lanes [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]{ min-width: 300px !important; }
}
</style>
"""


def inject() -> None:
    st.markdown(CSS, unsafe_allow_html=True)
