"""下载美股 ETF 日线（复权）到 data/etf_daily/*.csv，供多资产趋势研究使用。只读行情接口。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from aiquant import config  # noqa: E402
from aiquant.data import http  # noqa: E402
from aiquant.data.alpaca import _data_headers  # noqa: E402

UNIVERSE = ["SPY", "QQQ", "IWM", "DIA", "TLT", "IEF", "SHY", "GLD", "SLV", "USO", "DBC", "UUP", "EFA", "EEM",
            "VNQ", "XLK", "XLE", "XLF", "XLV", "SSO", "QLD", "UPRO", "TQQQ", "TMF", "UBT", "UGL"]
OUT = ROOT / "data" / "etf_daily"


def fetch(sym: str, feed: str) -> pd.DataFrame:
    url = f"{config.ALPACA_DATA_URL}/v2/stocks/bars"
    params = {"symbols": sym, "timeframe": "1Day", "start": "2015-01-01T00:00:00Z", "adjustment": "all",
              "feed": feed, "limit": 10000, "sort": "asc"}
    rows = []
    while True:
        r = http.request("GET", url, headers=_data_headers(), params=params, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"{r.status_code} {r.text[:150]}")
        js = r.json()
        rows += (js.get("bars") or {}).get(sym, []) or []
        if not js.get("next_page_token"):
            break
        params["page_token"] = js["next_page_token"]
    df = pd.DataFrame(rows).rename(columns={"t": "date", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    return df[["date", "open", "high", "low", "close", "volume"]] if len(df) else df


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    log = []
    for s in UNIVERSE:
        for feed in ("sip", "iex"):
            try:
                df = fetch(s, feed)
                df.to_csv(OUT / f"{s}.csv", index=False)
                log.append(f"{s}\t{feed}\t{len(df)}\t{df['date'].iloc[0] if len(df) else ''}\t{df['date'].iloc[-1] if len(df) else ''}")
                break
            except Exception as e:  # noqa: BLE001
                log.append(f"{s}\t{feed}\tERROR {e}")
    (OUT / "_fetch_log.txt").write_text("\n".join(log), encoding="utf-8")
    print("\n".join(log))


if __name__ == "__main__":
    main()
