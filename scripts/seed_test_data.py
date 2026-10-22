"""仅供自动化测试：在独立目录写入合成行情（TEST_SYNTHETIC，is_test=1）。正式环境不要运行。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from datetime import timedelta

from aiquant import actions, research
from aiquant.data import market_data as md

actions.init()
sp = research.get_split()
n = md.install_synthetic_test_data(md.to_utc(sp["val_start"]) - timedelta(days=500), md.to_utc(sp["test_end"]))
research.set_research({"source": "test"})
print("seeded", n)
