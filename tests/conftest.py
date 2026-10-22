import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="aiquant_test_")
os.environ["AIQUANT_ROOT"] = _tmp
os.environ["AIQUANT_DEEPSEEK_MOCK"] = "1"
os.environ["AIQUANT_DISABLE_PAPER_LOOP"] = "1"
os.environ["AIQUANT_DISABLE_LIVE_FEED"] = "1"
for k in ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY", "ALPACA_PAPER_KEY_ID", "ALPACA_PAPER_SECRET_KEY",
          "DEEPSEEK_API_KEY"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def env():
    from datetime import timedelta

    from aiquant import actions, research
    from aiquant.data import market_data as md
    actions.init()
    sp = research.get_split()
    md.install_synthetic_test_data(md.to_utc(sp["val_start"]) - timedelta(days=200), md.to_utc(sp["test_end"]))
    research.set_research({"source": "test"})
    return sp
