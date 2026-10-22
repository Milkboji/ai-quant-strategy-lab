"""下载 BTC/USD 与 ETH/USD 的 5 分钟历史（2021 年起）到 data/history/，供“快速进化”的更早历史与跨币种检验使用。
单独存放，不改动服务正在使用的主行情缓存。只读行情接口。"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from aiquant.data import alpaca  # noqa: E402

OUT = ROOT / "data" / "history"
JOBS = [("BTC/USD", datetime(2021, 1, 1, tzinfo=timezone.utc)), ("ETH/USD", datetime(2021, 1, 1, tzinfo=timezone.utc))]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    log = []
    for sym, start in JOBS:
        parts = []
        cur = start
        while cur < end:   # 按季度分段下载，避免单次请求过大
            nxt = min(pd.Timestamp(cur) + pd.DateOffset(months=3), pd.Timestamp(end)).to_pydatetime()
            try:
                df = alpaca.fetch_bars_5m(sym, cur, nxt)
                parts.append(df)
                print(f"{sym} {cur:%Y-%m-%d} ~ {nxt:%Y-%m-%d}: {len(df)} 根", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"{sym} {cur:%Y-%m-%d} 失败: {e}", flush=True)
            cur = nxt
        df = pd.concat(parts).drop_duplicates("ts").sort_values("ts") if parts else pd.DataFrame()
        p = OUT / f"{sym.replace('/', '')}_5m.csv.gz"
        df.to_csv(p, index=False, compression="gzip", float_format="%.8f")
        first = df["ts"].iloc[0] if len(df) else ""
        last = df["ts"].iloc[-1] if len(df) else ""
        log.append(f"{sym}\t{len(df)}\t{first}\t{last}")
    (OUT / "_fetch_log.txt").write_text("\n".join(log), encoding="utf-8")
    print("\n".join(log))


if __name__ == "__main__":
    main()
