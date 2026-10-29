"""下载美股 ETF 日线（复权）到 data/etf_daily/*.csv，供多资产趋势研究使用。只读行情接口。

只保存已收盘的交易日；日常增量更新由模拟盘服务每个交易日自动完成（aiquant/data/us_daily.refresh）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiquant.data import us_daily  # noqa: E402

UNIVERSE = us_daily.UNIVERSE
OUT = us_daily.data_dir()


def main() -> None:
    res = us_daily.full_download()
    log = [f"{s}\t{v}" for s, v in res.items()]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "_fetch_log.txt").write_text("\n".join(log), encoding="utf-8")
    print("\n".join(log))


if __name__ == "__main__":
    main()
