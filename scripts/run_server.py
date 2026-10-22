"""服务入口：先在本进程里启动后台循环（模拟盘交易、任务队列、实时行情），再启动 Streamlit 网页。

这样即使没有人打开网页，自动交易、挑战者评估和定时进化也会照常运行；
之后打开网页时 actions.init() 发现已启动，不会重复启动。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def main() -> None:
    from aiquant import config
    logging.basicConfig(filename=str(config.LOG_DIR / "app.log"), level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from aiquant import actions
    actions.init()
    logging.getLogger("aiquant").info("后台循环已在服务启动时开启（无需打开网页）")

    from streamlit import config as st_config
    from streamlit.web import bootstrap
    script = str(ROOT / "app.py")
    flags = {"server.address": "127.0.0.1", "server.port": 8501, "server.headless": True}
    try:
        st_config._main_script_path = script   # 让 .streamlit/config.toml 按项目目录加载
    except Exception:  # noqa: BLE001
        pass
    bootstrap.load_config_options(flag_options=flags)
    bootstrap.run(script, False, [], flags)


if __name__ == "__main__":
    main()
