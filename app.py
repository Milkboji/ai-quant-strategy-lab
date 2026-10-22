"""AI Quant Strategy Lab v1.0 — 本地入口：streamlit run app.py （默认 http://127.0.0.1:8501）"""
import logging

import streamlit as st

from aiquant import actions, config

logging.basicConfig(filename=str(config.LOG_DIR / "app.log"), level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

st.set_page_config(page_title="AI Quant Strategy Lab", page_icon="📈", layout="wide",
                   initial_sidebar_state="collapsed")

from aiquant.ui import style, common  # noqa: E402
from aiquant.ui import page_lab, page_paper, page_versions, page_history, page_data, page_settings  # noqa: E402
from aiquant.ui import page_live, page_evolve, page_home  # noqa: E402

actions.init()
style.inject()

pages = {
    "home": st.Page(page_home.render, title="总览", icon="🏠", url_path="home", default=True),
    "live": st.Page(page_live.render, title="实时看盘", icon="📈", url_path="live"),
    "paper": st.Page(page_paper.render, title="模拟盘交易", icon="💹", url_path="paper"),
    "evolve": st.Page(page_evolve.render, title="策略进化", icon="🧬", url_path="evolve"),
    "lab": st.Page(page_lab.render, title="策略实验室", icon="🧪", url_path="lab"),
    "versions": st.Page(page_versions.render, title="策略版本", icon="🗂️", url_path="versions"),
    "history": st.Page(page_history.render, title="历史记录", icon="🕘", url_path="history"),
    "data": st.Page(page_data.render, title="数据管理", icon="🗄️", url_path="data"),
    "settings": st.Page(page_settings.render, title="系统设置", icon="⚙️", url_path="settings"),
}
st.session_state["_pages"] = pages
# 常用页面直接显示；研究与管理类页面收进“研究与设置”下拉
nav = st.navigation({
    "": [pages[k] for k in ("home", "live", "paper", "evolve")],
    "研究与设置": [pages[k] for k in ("lab", "versions", "history", "data", "settings")],
}, position="top")
common.header()
nav.run()
