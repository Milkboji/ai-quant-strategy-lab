"""行情缓存、数据版本、K 线聚合与质量检查。

原则：
- 基础粒度为 5 分钟 OHLCV（UTC）。
- 只缓存 Alpaca 实际返回的 K 线；无成交的 5 分钟不补假 K 线，只在缺口报告中统计。
- 每个回测引用一个不可变的数据快照（dataset），带 SHA256。
- 不同交易场所数据不混合（缓存文件按 venue 区分）。
"""
from __future__ import annotations

import hashlib
import io
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .. import config, db
from . import alpaca

COLS = ["ts", "open", "high", "low", "close", "volume", "trade_count", "vwap"]
SNAP_DIR = config.CACHE_DIR / "snapshots"
SNAP_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_WARMUP_DAYS = 60
# 聚合K线至少需要 90% 的 5 分钟K线才视为完整；不完整K线不参与指标与信号（已披露）
MIN_BAR_COVERAGE = 0.9


class InsufficientData(RuntimeError):
    def __init__(self, msg: str, available: tuple[str | None, str | None] = (None, None)):
        super().__init__(msg)
        self.available = available


def _master_path(venue: str, symbol: str) -> Path:
    return config.CACHE_DIR / f"{venue}_{symbol.replace('/', '')}_5m.csv.gz"


def _coverage_key(venue: str, symbol: str) -> str:
    return f"coverage::{venue}::{symbol}"


def to_utc(x) -> datetime:
    ts = pd.Timestamp(x)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert("UTC").to_pydatetime()


# ---------------------------------------------------------------------------
# 覆盖区间（记录"已向 API 请求过"的区间，用来区分"未下载"和"无成交"）
# ---------------------------------------------------------------------------

def _merge_intervals(iv: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    iv = sorted(iv)
    out: list[tuple[datetime, datetime]] = []
    for s, e in iv:
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def get_coverage(venue: str = alpaca.VENUE, symbol: str = "BTC/USD") -> list[tuple[datetime, datetime]]:
    raw = db.get_setting(_coverage_key(venue, symbol), [])
    return [(to_utc(s), to_utc(e)) for s, e in raw]


def _set_coverage(iv, venue, symbol):
    db.set_setting(_coverage_key(venue, symbol), [(s.isoformat(), e.isoformat()) for s, e in _merge_intervals(iv)])


def missing_ranges(start: datetime, end: datetime, cov) -> list[tuple[datetime, datetime]]:
    gaps, cur = [], start
    for s, e in _merge_intervals(cov):
        if e <= cur:
            continue
        if s > cur:
            gaps.append((cur, min(s, end)))
        cur = max(cur, e)
        if cur >= end:
            break
    if cur < end:
        gaps.append((cur, end))
    return [(s, e) for s, e in gaps if e > s]


_master_cache: dict[str, tuple[tuple[int, int], pd.DataFrame]] = {}
_master_lock = threading.Lock()


def load_master(venue: str = alpaca.VENUE, symbol: str = "BTC/USD") -> pd.DataFrame:
    """读取主行情缓存。文件未变化时复用内存中的解析结果（避免每分钟重复解析整份 CSV 造成内存碎片）。"""
    p = _master_path(venue, symbol)
    if not p.exists():
        return pd.DataFrame(columns=COLS)
    st_ = p.stat()
    sig = (st_.st_mtime_ns, st_.st_size)
    with _master_lock:
        hit = _master_cache.get(str(p))
        if hit and hit[0] == sig:
            return hit[1].copy()
    try:
        df = pd.read_csv(p)
    except Exception as e:  # noqa: BLE001  文件损坏（例如两个进程同时写）：隔离、保留能读出的部分、其余重新下载
        df = _recover_master(p, venue, symbol, e)
        st_ = p.stat() if p.exists() else None
        sig = (st_.st_mtime_ns, st_.st_size) if st_ else (0, 0)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    with _master_lock:
        _master_cache.clear()          # 只保留最新一份
        _master_cache[str(p)] = (sig, df)
    return df.copy()


def _recover_master(p: Path, venue: str, symbol: str, err: Exception) -> pd.DataFrame:
    """主行情缓存损坏：
    1. 逐行读出损坏位置之前的完整 K 线（文件按时间排序，所以是从最早开始的连续一段）；
    2. 原文件改名为 .corrupt-时间 隔离（不删除，便于排查）；
    3. 保存读出的部分，覆盖区间截到最后一根读出的 K 线 → 之后缺的部分在下次更新时自动重新下载。"""
    import gzip
    import logging
    lines: list[str] = []
    try:   # 逐行读到第一行坏数据为止（字段数不对、时间或价格不是数字），之后的全部丢弃
        with gzip.open(p, "rt", encoding="utf-8", errors="strict") as f:
            header = f.readline()
            ncol = len(header.strip().split(","))
            lines.append(header)
            for line in f:
                parts = line.rstrip("\n").split(",")
                if len(parts) != ncol or not line.endswith("\n"):
                    break
                try:
                    pd.Timestamp(parts[0])
                    [float(x) for x in parts[1:5]]
                except Exception:  # noqa: BLE001
                    break
                lines.append(line)
    except Exception:  # noqa: BLE001
        pass
    good = pd.DataFrame(columns=COLS)
    if len(lines) > 2:
        try:
            good = pd.read_csv(io.StringIO("".join(lines[:-1])))    # 再去掉一行，避免损坏前最后一行不完整
            good = good.dropna(subset=["ts", "open", "high", "low", "close"])
            good["ts"] = pd.to_datetime(good["ts"], utc=True, errors="coerce")
            good = good.dropna(subset=["ts"])
            good, _ = clean_bars(good)
        except Exception:  # noqa: BLE001
            good = pd.DataFrame(columns=COLS)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    bad = p.with_name(f"{p.name}.corrupt-{stamp}")
    try:
        p.replace(bad)
    except OSError:          # 另一个进程已经处理过
        pass
    last = pd.Timestamp(good["ts"].max()) + pd.Timedelta(minutes=5) if len(good) else None
    if len(good):
        _save_master(good, venue, symbol)
        cov = [(s_, min(e_, last.to_pydatetime())) for s_, e_ in get_coverage(venue, symbol) if s_ < last.to_pydatetime()]
    else:
        cov = []
    _set_coverage(cov, venue, symbol)
    msg = (f"行情缓存文件损坏（{type(err).__name__}: {str(err)[:80]}），已隔离为 {bad.name}；"
           + (f"保留 {len(good)} 根 K 线（到 {last:%Y-%m-%d %H:%M} UTC），之后的部分自动重新下载" if len(good)
              else "没有可用的 K 线，全部重新下载"))
    logging.getLogger("aiquant.data").error(msg)
    try:
        from ..paper import runner as pr
        pr.event(None, "ERROR", msg)
    except Exception:  # noqa: BLE001
        pass
    return good if len(good) else pd.DataFrame(columns=COLS)


def _save_master(df: pd.DataFrame, venue: str, symbol: str) -> None:
    """先写本进程 / 本线程专用的临时文件，再原子替换：多个进程同时写也不会把文件写坏（最多后写的覆盖先写的）。"""
    import os
    p = _master_path(venue, symbol)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        df.to_csv(tmp, index=False, compression="gzip")
        tmp.replace(p)
    finally:
        tmp.unlink(missing_ok=True)


def clean_bars(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """去重、排序、标记异常。异常行被剔除（不修补），数量写入质量报告。"""
    n0 = len(df)
    df = df.sort_values("ts")
    dups = int(df.duplicated("ts", keep="last").sum())
    df = df.drop_duplicates("ts", keep="last")
    bad_mask = (
        (df[["open", "high", "low", "close"]] <= 0).any(axis=1)
        | (df["high"] < df[["open", "close", "low"]].max(axis=1) - 1e-9)
        | (df["low"] > df[["open", "close", "high"]].min(axis=1) + 1e-9)
        | df[["open", "high", "low", "close"]].isna().any(axis=1)
    )
    bad = int(bad_mask.sum())
    df = df[~bad_mask].reset_index(drop=True)
    ret = df["close"].pct_change().abs()
    outliers = int((ret > 0.15).sum())  # 5 分钟内 >15% 视为可疑，仅标记
    return df, {"input_rows": n0, "duplicates_removed": dups, "invalid_rows_removed": bad,
                "suspicious_jumps_gt15pct": outliers}


def ingest(df_new: pd.DataFrame, fetched: list[tuple[datetime, datetime]],
           venue: str = alpaca.VENUE, symbol: str = "BTC/USD") -> None:
    m = load_master(venue, symbol)
    merged = pd.concat([m, df_new], ignore_index=True) if len(m) else df_new
    merged, _ = clean_bars(merged)
    _save_master(merged, venue, symbol)
    _set_coverage(get_coverage(venue, symbol) + list(fetched), venue, symbol)


def update_from_alpaca(start: datetime, end: datetime, symbol: str = "BTC/USD", progress=None) -> dict:
    """只下载缺失区间（不重复下载）。"""
    with _ds_lock:
        return _update_from_alpaca(start, end, symbol, progress)


def _update_from_alpaca(start: datetime, end: datetime, symbol: str = "BTC/USD", progress=None) -> dict:
    now = datetime.now(timezone.utc)
    end = min(end, now.replace(second=0, microsecond=0))
    todo = missing_ranges(start, end, get_coverage(alpaca.VENUE, symbol))
    total = 0
    for s, e in todo:
        df = alpaca.fetch_bars_5m(symbol, s, e, progress=progress)
        total += len(df)
        # 最后一段的结尾若接近"现在"，最后一根 K 线可能未走完：覆盖区间截到最后完整 K 线
        cov_end = e
        if (now - e) < timedelta(minutes=10):
            cov_end = (pd.Timestamp(e).floor("5min") - pd.Timedelta(minutes=5)).to_pydatetime()
            df = df[df["ts"] < pd.Timestamp(cov_end)]
        ingest(df, [(s, cov_end)], alpaca.VENUE, symbol)
    db.set_setting("market_last_update", {"at": db.now_iso(), "rows": total, "ranges": len(todo)})
    return {"downloaded_rows": total, "ranges": len(todo)}


# ---------------------------------------------------------------------------
# 数据快照（数据版本）
# ---------------------------------------------------------------------------

def _hash_df(df: pd.DataFrame) -> str:
    buf = io.StringIO()
    df.to_csv(buf, index=False, float_format="%.8f")
    return hashlib.sha256(buf.getvalue().encode()).hexdigest()


def quality_report(df: pd.DataFrame, start: datetime, end: datetime) -> dict:
    expected = int((end - start) / timedelta(minutes=5))
    present = int(((df["ts"] >= pd.Timestamp(start)) & (df["ts"] < pd.Timestamp(end))).sum())
    missing = max(expected - present, 0)
    gaps = df["ts"].diff().dt.total_seconds().div(60).fillna(5)
    largest_gap_min = float(gaps.max()) if len(gaps) else 0.0
    cov = present / expected if expected else 0.0
    grade = "良好" if cov >= 0.98 else ("一般" if cov >= 0.90 else "较差")
    return {"expected_5m_bars": expected, "present_5m_bars": present, "missing_5m_bars": missing,
            "coverage_pct": round(cov * 100, 2), "largest_gap_minutes": largest_gap_min,
            "grade": grade,
            "note": "缺失的5分钟K线代表该时段 Alpaca 无成交或未返回数据，系统不补造虚假K线。"}


_ds_lock = threading.RLock()


def get_dataset(start: datetime, end: datetime, **kw) -> dict:
    """线程安全包装：多个回测任务共享同一数据版本，只下载/生成一次。"""
    with _ds_lock:
        return _get_dataset(start, end, **kw)


def _get_dataset(start: datetime, end: datetime, *, source: str = "alpaca", symbol: str = "BTC/USD",
                 warmup_days: int = DEFAULT_WARMUP_DAYS, progress=None, allow_test: bool = False) -> dict:
    """返回覆盖 [start-warmup, end) 的不可变数据快照记录。数据不足时抛出 InsufficientData。"""
    start, end = to_utc(start), to_utc(end)
    wstart = start - timedelta(days=warmup_days)
    venue = alpaca.VENUE
    if source == "test":
        venue = "TEST_SYNTHETIC"
        allow_test = True

    # 1) 复用已有的同区间快照
    existing = db.q1(
        "SELECT * FROM datasets WHERE symbol=? AND venue=? AND start_utc=? AND end_utc=? AND is_test=? "
        "ORDER BY id DESC LIMIT 1",
        (symbol, venue, wstart.isoformat(), end.isoformat(), 1 if venue == "TEST_SYNTHETIC" else 0))
    if existing and Path(existing["path"]).exists():
        return existing

    # 2) 需要时增量下载
    if source == "alpaca":
        update_from_alpaca(wstart, end, symbol, progress=progress)
    cov = get_coverage(venue, symbol)
    miss = missing_ranges(wstart, end, cov)
    if miss:
        merged = _merge_intervals(cov)
        avail = (merged[0][0].isoformat(), merged[-1][1].isoformat()) if merged else (None, None)
        msg = (f"本地数据不足：需要 {wstart:%Y-%m-%d} 至 {end:%Y-%m-%d}（含 {warmup_days} 天指标预热），"
               f"缺少 {len(miss)} 段，首段缺口 {miss[0][0]:%Y-%m-%d %H:%M} ~ {miss[0][1]:%Y-%m-%d %H:%M} UTC。")
        if source == "cache":
            msg += " 当前为『本地行情缓存』模式，可切换为『Alpaca API』或在数据管理页补采。"
        raise InsufficientData(msg, avail)

    m = load_master(venue, symbol)
    snap = m[(m["ts"] >= pd.Timestamp(wstart)) & (m["ts"] < pd.Timestamp(end))].reset_index(drop=True)
    if snap.empty:
        raise InsufficientData("所选区间内没有任何 K 线数据。")
    h = _hash_df(snap)
    qr = quality_report(snap, start, end)
    qr["warmup_start"] = wstart.isoformat()
    qr["research_start"] = start.isoformat()
    path = SNAP_DIR / f"{venue}_{symbol.replace('/', '')}_{h[:16]}.csv.gz"
    if not path.exists():
        tmp = path.with_name(path.name + ".tmp")
        snap.to_csv(tmp, index=False, compression="gzip", float_format="%.8f")
        tmp.replace(path)
    dup = db.q1("SELECT * FROM datasets WHERE sha256=? AND start_utc=? AND end_utc=?",
                (h, wstart.isoformat(), end.isoformat()))
    if dup:
        return dup
    did = db.ex(
        "INSERT INTO datasets(symbol,source,venue,timeframe_min,start_utc,end_utc,tz,fetched_at,rows,sha256,path,"
        "quality_json,is_test) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (symbol, "TEST_SYNTHETIC" if venue == "TEST_SYNTHETIC" else "Alpaca API", venue, 5,
         wstart.isoformat(), end.isoformat(), "UTC", db.now_iso(), len(snap), h, str(path),
         json.dumps(qr, ensure_ascii=False), 1 if venue == "TEST_SYNTHETIC" else 0))
    return db.q1("SELECT * FROM datasets WHERE id=?", (did,))


def load_dataset(ds: dict) -> pd.DataFrame:
    df = pd.read_csv(ds["path"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    if "close_time" in df.columns:          # 美股日线快照
        df["close_time"] = pd.to_datetime(df["close_time"], utc=True)
    if _hash_df(df) != ds["sha256"]:
        raise RuntimeError(f"数据快照 {ds['id']} 哈希校验失败，可能被修改")
    return df


# ---------------------------------------------------------------------------
# K 线聚合
# ---------------------------------------------------------------------------

def resample(df5: pd.DataFrame, tf_min: int, data_end: datetime | None = None) -> pd.DataFrame:
    """按 UTC 边界把 5 分钟 K 线聚合为 tf_min。

    返回列：open_time, close_time, open, high, low, close, volume, n_5m, expected_5m, complete
    complete=False 表示该周期在数据结束时尚未走完（不得用于信号）。
    缺少成交的周期不会生成 K 线。
    """
    if df5.empty:
        return pd.DataFrame(columns=["open_time", "close_time", "open", "high", "low", "close", "volume",
                                     "n_5m", "expected_5m", "complete"])
    s = df5.set_index("ts")
    if tf_min == 5:
        out = s[["open", "high", "low", "close", "volume"]].copy()
        out["n_5m"] = 1
    else:
        rule = f"{tf_min}min"
        g = s.resample(rule, origin="epoch", label="left", closed="left")
        out = pd.DataFrame({
            "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
            "close": g["close"].last(), "volume": g["volume"].sum(), "n_5m": g["open"].count(),
        })
        out = out[out["n_5m"] > 0]
    out.index.name = "open_time"
    out = out.reset_index()
    out["close_time"] = out["open_time"] + pd.Timedelta(minutes=tf_min)
    out["expected_5m"] = tf_min // 5
    last_avail = (pd.Timestamp(data_end) if data_end is not None
                  else df5["ts"].max() + pd.Timedelta(minutes=5))
    min_n = np.ceil(out["expected_5m"] * MIN_BAR_COVERAGE)
    out["complete"] = (out["close_time"] <= last_avail) & (out["n_5m"] >= min_n)
    return out[["open_time", "close_time", "open", "high", "low", "close", "volume", "n_5m", "expected_5m",
                "complete"]]


def cache_summary(venue: str = alpaca.VENUE, symbol: str = "BTC/USD") -> dict:
    cov = _merge_intervals(get_coverage(venue, symbol))
    p = _master_path(venue, symbol)
    rows = 0
    if p.exists():
        try:
            rows = len(pd.read_csv(p, usecols=["ts"]))
        except Exception:
            rows = 0
    return {"venue": venue, "symbol": symbol, "rows": rows,
            "coverage": [(s.isoformat(), e.isoformat()) for s, e in cov],
            "first": cov[0][0].isoformat() if cov else None,
            "last": cov[-1][1].isoformat() if cov else None,
            "last_update": db.get_setting("market_last_update")}


# ---------------------------------------------------------------------------
# 合成测试数据（仅测试使用，所有记录 is_test=1，绝不参与正式研究）
# ---------------------------------------------------------------------------

def install_synthetic_test_data(start: datetime, end: datetime, seed: int = 7, symbol: str = "BTC/USD") -> int:
    rng = np.random.default_rng(seed)
    idx = pd.date_range(to_utc(start), to_utc(end), freq="5min", inclusive="left")
    n = len(idx)
    # 带趋势切换的几何随机游走
    drift = np.repeat(rng.normal(0, 0.00008, size=n // 2000 + 1), 2000)[:n]
    rets = drift + rng.normal(0, 0.0016, size=n)
    close = 60000 * np.exp(np.cumsum(rets))
    open_ = np.concatenate([[close[0]], close[:-1]])
    spread = np.abs(rng.normal(0, 0.0008, size=n)) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    vol = np.abs(rng.normal(3, 1, size=n))
    df = pd.DataFrame({"ts": idx, "open": open_, "high": high, "low": low, "close": close,
                       "volume": vol, "trade_count": (vol * 10).astype(int), "vwap": (high + low) / 2})
    ingest(df, [(to_utc(start), to_utc(end))], "TEST_SYNTHETIC", symbol)
    return n
