"""美股 ETF 日线（复权）本地库：data/etf_daily/{SYM}.csv。只读行情接口，所有请求走唯一出口 http.request。

规则：
- 只保存「已收盘」的交易日：美东日期早于今天的日线才写入；当天还没走完的 K 线不进库。
- 每天更新全部 UNIVERSE（一次多品种请求），不只信号/交易两只。
- 复权口径检查：新拉到的最近 30 天与库里同一天的收盘价相差超过 COMPARE_TOL（拆股、分红后复权因子变化），
  说明旧历史已和新口径不一致 → 该品种从 START 起整只重新下载，避免拼接处出现价格断层。
- 写文件先写 .tmp 再原子替换。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .. import config
from . import http

log = logging.getLogger("aiquant.us_daily")
ET = ZoneInfo("America/New_York")
UNIVERSE = ["SPY", "QQQ", "IWM", "DIA", "TLT", "IEF", "SHY", "GLD", "SLV", "USO", "DBC", "UUP", "EFA", "EEM",
            "VNQ", "XLK", "XLE", "XLF", "XLV", "SSO", "QLD", "UPRO", "TQQQ", "TMF", "UBT", "UGL"]
START = "2015-01-01T00:00:00Z"
COLS = ["date", "open", "high", "low", "close", "volume"]
COMPARE_TOL = 0.002        # 0.2%：复权因子变化的判定阈值（正常数据修订远小于此）


def data_dir() -> Path:
    return config.DATA_DIR / "etf_daily"


def path(sym: str) -> Path:
    return data_dir() / f"{sym}.csv"


def et_date(s) -> pd.Series:
    """CSV 里的 date（UTC 时间戳，如 2026-10-23T04:00:00Z）→ 美东交易日期。"""
    return pd.to_datetime(s, utc=True).dt.tz_convert(ET).dt.date


def today_et(now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(ET).date()


def load(sym: str) -> pd.DataFrame | None:
    p = path(sym)
    if not p.exists():
        return None
    return pd.read_csv(p)


def _headers() -> dict:
    from .alpaca import _data_headers
    return _data_headers()


def fetch(symbols: list[str], start: str, end: str | None = None, feed: str = "sip") -> dict[str, pd.DataFrame]:
    """多品种日线（复权），自动翻页。返回 {sym: DataFrame[COLS]}。"""
    params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "adjustment": "all",
              "feed": feed, "limit": 10000, "sort": "asc"}
    if end:
        params["end"] = end
    rows: dict[str, list] = {s: [] for s in symbols}
    while True:
        r = http.request("GET", f"{config.ALPACA_DATA_URL}/v2/stocks/bars", headers=_headers(), params=params,
                         timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"美股日线获取失败 {r.status_code}: {r.text[:150]}")
        js = r.json()
        for s, bars in (js.get("bars") or {}).items():
            rows.setdefault(s, []).extend(bars or [])
        if not js.get("next_page_token"):
            break
        params["page_token"] = js["next_page_token"]
    out = {}
    for s, rr in rows.items():
        df = pd.DataFrame(rr).rename(columns={"t": "date", "o": "open", "h": "high", "l": "low", "c": "close",
                                              "v": "volume"})
        out[s] = df[COLS] if len(df) else pd.DataFrame(columns=COLS)
    return out


def closed_only(df: pd.DataFrame, today: date) -> pd.DataFrame:
    """只保留美东日期早于 today 的日线（当天未收盘的 K 线不进库）。"""
    if df is None or not len(df):
        return df
    return df[et_date(df["date"]) < today].reset_index(drop=True)


def adjustment_changed(old: pd.DataFrame | None, new: pd.DataFrame, tol: float = COMPARE_TOL) -> bool:
    """同一天的收盘价在新旧两份数据里差异超过 tol → 复权口径变了。"""
    if old is None or not len(old) or not len(new):
        return False
    a = old.assign(d=et_date(old["date"])).drop_duplicates("d", keep="last").set_index("d")["close"]
    b = new.assign(d=et_date(new["date"])).drop_duplicates("d", keep="last").set_index("d")["close"]
    both = a.index.intersection(b.index)
    if not len(both):
        return False
    rel = ((b[both] - a[both]).abs() / a[both].abs().clip(lower=1e-9))
    return bool((rel > tol).any())


def _write(sym: str, df: pd.DataFrame) -> None:
    data_dir().mkdir(parents=True, exist_ok=True)
    p = path(sym)
    tmp = p.with_suffix(".tmp")
    df[COLS].to_csv(tmp, index=False)
    tmp.replace(p)


def merge(old: pd.DataFrame | None, new: pd.DataFrame, today: date) -> pd.DataFrame:
    """旧库 + 新数据，按美东日期去重（新数据优先），并去掉 today 及以后的日线。"""
    parts = [x for x in (old, new) if x is not None and len(x)]
    if not parts:
        return pd.DataFrame(columns=COLS)
    allv = pd.concat(parts, ignore_index=True)
    allv["_d"] = et_date(allv["date"])
    allv = allv[allv["_d"] < today].drop_duplicates("_d", keep="last").sort_values("_d")
    return allv[COLS].reset_index(drop=True)


def refresh(symbols: list[str] | None = None, now: datetime | None = None, days: int = 30) -> dict:
    """每日增量更新：拉最近 days 天；复权口径变化的品种整只重下。返回 {sym: 结果说明}。"""
    symbols = symbols or UNIVERSE
    td = today_et(now)
    start = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    got = fetch(symbols, start)
    out = {}
    for s in symbols:
        new = closed_only(got.get(s), td)
        old = load(s)
        if new is None or not len(new):
            out[s] = "无新数据"
            continue
        if adjustment_changed(closed_only(old, td) if old is not None else None, new):
            full = closed_only(fetch([s], START).get(s), td)
            if full is not None and len(full):
                _write(s, merge(None, full, td))
                out[s] = f"复权口径变化，已整只重新下载（{len(full)} 根）"
                log.info("us_daily %s re-downloaded after adjustment change", s)
                continue
        merged = merge(old, new, td)
        _write(s, merged)
        out[s] = f"更新至 {et_date(merged['date']).iloc[-1]}"
    return out


def full_download(symbols: list[str] | None = None, now: datetime | None = None) -> dict:
    """整库下载（首次或手动重建）：sip 失败时改用 iex。"""
    td = today_et(now)
    out = {}
    for s in symbols or UNIVERSE:
        for feed in ("sip", "iex"):
            try:
                df = closed_only(fetch([s], START, feed=feed).get(s), td)
                _write(s, merge(None, df, td))
                out[s] = f"{feed}\t{len(df)}"
                break
            except Exception as e:  # noqa: BLE001
                out[s] = f"{feed}\tERROR {e}"
    return out


# ---------------------------------------------------------------------------
# 不可变快照（与 BTC 的 market_data.get_dataset 同一张 datasets 表）
# ---------------------------------------------------------------------------

VENUE = "alpaca_stocks"


def to_bars(df: pd.DataFrame) -> pd.DataFrame:
    """CSV 日线 → 引擎使用的日线：ts = 当天开盘（UTC），close_time = 当天收盘（UTC，半日市 13:00）。"""
    from .. import market as mk
    days = et_date(df["date"])
    return pd.DataFrame({"ts": pd.to_datetime([mk.session_open(d) for d in days], utc=True),
                         "close_time": pd.to_datetime([mk.session_close(d) for d in days], utc=True),
                         "open": df["open"].astype(float).values, "high": df["high"].astype(float).values,
                         "low": df["low"].astype(float).values, "close": df["close"].astype(float).values,
                         "volume": df["volume"].astype(float).values})


def quality_report(bars: pd.DataFrame, start: datetime, end: datetime) -> dict:
    """按交易日历检查缺口：区间内应有的交易日 vs 实际有的日线。"""
    from .. import market as mk
    s, e = start.astimezone(ET).date(), (end - timedelta(seconds=1)).astimezone(ET).date()
    expected = mk.trading_days(s, e)
    have = set(pd.to_datetime(bars["ts"], utc=True).dt.tz_convert(ET).dt.date)
    missing = [d for d in expected if d not in have]
    cov = (len(expected) - len(missing)) / len(expected) if expected else 0.0
    half = [d for d in expected if mk.is_half_day(d)]
    return {"expected_days": len(expected), "present_days": len(expected) - len(missing), "missing_days": len(missing),
            "missing_sample": [str(d) for d in missing[:10]], "half_days": len(half),
            "coverage_pct": round(cov * 100, 2), "grade": "良好" if cov >= 0.995 else ("一般" if cov >= 0.98 else "较差"),
            "note": "按纽交所交易日历（含节假日、半日市）检查；缺失的交易日不补造。"}


def get_dataset(symbol: str, start: datetime, end: datetime, warmup_days: int = 420) -> dict:
    """美股日线的不可变快照：覆盖 [start - warmup, end)，写入 datasets 表（SHA256、交易日缺口报告）。"""
    from .. import db
    from .market_data import SNAP_DIR, InsufficientData, _hash_df
    wstart = start - timedelta(days=warmup_days)
    raw = load(symbol)
    if raw is None or not len(raw):
        raise InsufficientData(f"缺少日线数据：{symbol}（运行 fetch_etf.bat 下载）")
    bars = to_bars(raw)
    snap = bars[(bars["ts"] >= pd.Timestamp(wstart)) & (bars["ts"] < pd.Timestamp(end))].reset_index(drop=True)
    if snap.empty or snap["ts"].iloc[0] > pd.Timestamp(start):
        first = bars["ts"].iloc[0].strftime("%Y-%m-%d") if len(bars) else "--"
        raise InsufficientData(f"日线数据不足：{symbol} 最早 {first}，回测窗口（含 420 天预热）需要更早的数据，开始于 {start:%Y-%m-%d}")
    h = _hash_df(snap)
    dup = db.q1("SELECT * FROM datasets WHERE symbol=? AND venue=? AND sha256=? AND start_utc=? AND end_utc=?",
                (symbol, VENUE, h, wstart.isoformat(), end.isoformat()))
    if dup and Path(dup["path"]).exists():
        return dup
    qr = quality_report(snap, start, end)
    qr["warmup_start"] = wstart.isoformat()
    qr["research_start"] = start.isoformat()
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    p = SNAP_DIR / f"{VENUE}_{symbol}_{h[:16]}.csv.gz"
    if not p.exists():
        tmp = p.with_name(p.name + ".tmp")
        snap.to_csv(tmp, index=False, compression="gzip", float_format="%.8f")
        tmp.replace(p)
    import json
    did = db.ex("INSERT INTO datasets(symbol,source,venue,timeframe_min,start_utc,end_utc,tz,fetched_at,rows,sha256,path,"
                "quality_json,is_test) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (symbol, "Alpaca API（日线，复权）", VENUE, 1440, wstart.isoformat(), end.isoformat(), "America/New_York",
                 datetime.now(timezone.utc).isoformat(), len(snap), h, str(p), json.dumps(qr, ensure_ascii=False)))
    return db.q1("SELECT * FROM datasets WHERE id=?", (did,))
