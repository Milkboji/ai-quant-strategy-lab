"""实时行情源（后台线程，每秒更新）。

- 秒级K线：由 Alpaca 买卖报价（quotes）的中间价逐笔聚合，成交稀疏时也连续；成交量来自逐笔成交（trades）。
- 分钟级K线：Alpaca 1 分钟成交K线（1Min bars）为基础，本地聚合为 3/5/15/30 分钟、1/4 小时；
  当前未走完的K线用最新秒级数据实时更新（标记为"进行中"）。
- 只读接口，绝不下单。离线测试可设置环境变量 AIQUANT_FEED=synthetic 使用模拟行情（页面会醒目标注）。
"""
from __future__ import annotations

import logging
import math
import os
import random
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import pandas as pd

from .. import config
from ..data import alpaca, http

log = logging.getLogger("aiquant.live")
SYMBOL = "BTC/USD"
BASE = f"{config.ALPACA_DATA_URL}/v1beta3/crypto/us"

# 可选周期（秒）
TIMEFRAMES = {"1秒": 1, "5秒": 5, "15秒": 15, "30秒": 30, "1分钟": 60, "3分钟": 180, "5分钟": 300,
              "15分钟": 900, "30分钟": 1800, "1小时": 3600, "4小时": 14400}
SEC_KEEP = 3 * 3600           # 秒级数据保留 3 小时
MIN_BACKFILL_DAYS = 10        # 分钟K线回补天数


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class LiveFeed:
    def __init__(self, source: str | None = None):
        self.source = source or os.environ.get("AIQUANT_FEED", "alpaca")
        self.lock = threading.RLock()
        self.sec = {}                       # epoch_sec -> [o,h,l,c,vol]
        self.sec_order = deque()
        self.min_bars = pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
        self.bid = self.ask = self.last = None
        self.last_ts: datetime | None = None
        self.status = {"ok": False, "msg": "启动中", "updated": None, "source": self.source}
        self._q_cursor: datetime | None = None
        self._t_cursor: datetime | None = None
        self._last_min_fetch = 0.0
        self._started = False
        self._rng = random.Random(7)
        self._synth_px = 67000.0

    # ------------------------------------------------------------------ 数据写入
    def _add_tick(self, ts: datetime, price: float, vol: float = 0.0) -> None:
        s = int(ts.timestamp())
        row = self.sec.get(s)
        if row is None:
            self.sec[s] = [price, price, price, price, vol]
            self.sec_order.append(s)
        else:
            row[1] = max(row[1], price); row[2] = min(row[2], price); row[3] = price; row[4] += vol
        cutoff = int(time.time()) - SEC_KEEP
        while self.sec_order and self.sec_order[0] < cutoff:
            self.sec.pop(self.sec_order.popleft(), None)

    def _add_volume(self, ts: datetime, vol: float, price: float) -> None:
        s = int(ts.timestamp())
        if s in self.sec:
            self.sec[s][4] += vol
        else:
            self._add_tick(ts, price, vol)

    # ------------------------------------------------------------------ Alpaca
    def _get(self, path: str, params: dict) -> dict:
        r = http.request("GET", f"{BASE}{path}", headers=alpaca._data_headers(), params=params, timeout=10)
        if r.status_code != 200:
            raise RuntimeError(f"Alpaca {path} {r.status_code}: {r.text[:120]}")
        return r.json()

    def _pull_quotes(self, start: datetime, max_pages: int = 20) -> None:
        params = {"symbols": SYMBOL, "start": _iso(start), "limit": 10000, "sort": "asc"}
        for _ in range(max_pages):
            js = self._get("/quotes", params)
            qs = js.get("quotes", {}).get(SYMBOL, []) or []
            for q in qs:
                bp, ap = q.get("bp"), q.get("ap")
                if not bp or not ap or ap <= 0 or bp <= 0:
                    continue
                ts = pd.Timestamp(q["t"]).to_pydatetime()
                self._add_tick(ts, (bp + ap) / 2)
                self.bid, self.ask, self.last_ts = bp, ap, ts
                self._q_cursor = ts + timedelta(microseconds=1)
            tok = js.get("next_page_token")
            if not tok:
                break
            params["page_token"] = tok

    def _pull_trades(self, start: datetime, max_pages: int = 5) -> None:
        params = {"symbols": SYMBOL, "start": _iso(start), "limit": 10000, "sort": "asc"}
        for _ in range(max_pages):
            js = self._get("/trades", params)
            for t in js.get("trades", {}).get(SYMBOL, []) or []:
                ts = pd.Timestamp(t["t"]).to_pydatetime()
                self._add_volume(ts, float(t.get("s") or 0), float(t["p"]))
                self.last = float(t["p"])
                self._t_cursor = ts + timedelta(microseconds=1)
            tok = js.get("next_page_token")
            if not tok:
                break
            params["page_token"] = tok

    def _pull_minutes(self, start: datetime) -> None:
        params = {"symbols": SYMBOL, "timeframe": "1Min", "start": _iso(start), "limit": 10000, "sort": "asc"}
        rows = []
        for _ in range(10):
            js = self._get("/bars", params)
            rows += js.get("bars", {}).get(SYMBOL, []) or []
            tok = js.get("next_page_token")
            if not tok:
                break
            params["page_token"] = tok
        if not rows:
            return
        df = pd.DataFrame(rows).rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low", "c": "close",
                                                "v": "volume"})
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = df[["ts", "open", "high", "low", "close", "volume"]]
        with self.lock:
            m = pd.concat([self.min_bars, df]) if len(self.min_bars) else df
            m = m.drop_duplicates("ts", keep="last").sort_values("ts")
            cutoff = pd.Timestamp(datetime.now(timezone.utc) - timedelta(days=MIN_BACKFILL_DAYS + 1))
            self.min_bars = m[m["ts"] >= cutoff].reset_index(drop=True)

    def _tick_alpaca(self) -> None:
        now = datetime.now(timezone.utc)
        self._n = getattr(self, "_n", 0) + 1
        if self._q_cursor is None:                      # 首次回补
            self._pull_minutes(now - timedelta(days=MIN_BACKFILL_DAYS))
            with self.lock:
                self._pull_quotes(now - timedelta(minutes=15), max_pages=30)
                self._pull_trades(now - timedelta(minutes=15))
            if self._q_cursor is None:
                self._q_cursor = now - timedelta(seconds=5)
            if self._t_cursor is None:
                self._t_cursor = now - timedelta(seconds=5)
            self._last_min_fetch = time.time()
            return
        with self.lock:
            self._pull_quotes(self._q_cursor, max_pages=3)
        if self._n % 3 == 0:
            with self.lock:
                self._pull_trades(self._t_cursor, max_pages=2)
        if time.time() - self._last_min_fetch > 20:
            last = self.min_bars["ts"].max() if len(self.min_bars) else pd.Timestamp(now - timedelta(days=1))
            self._pull_minutes(last.to_pydatetime() - timedelta(minutes=2))
            self._last_min_fetch = time.time()

    # ------------------------------------------------------------------ 模拟行情（仅离线测试）
    def _tick_synth(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        if self._q_cursor is None:
            px = self._synth_px
            start = now - timedelta(days=MIN_BACKFILL_DAYS)
            rows, t = [], start
            while t < now - timedelta(minutes=1):
                o = px
                for _ in range(6):
                    px *= math.exp(self._rng.gauss(0, 0.0006))
                rows.append({"ts": pd.Timestamp(t), "open": o, "high": max(o, px) * 1.0003, "low": min(o, px) * 0.9997,
                             "close": px, "volume": abs(self._rng.gauss(2, 1))})
                t += timedelta(minutes=1)
            self.min_bars = pd.DataFrame(rows)
            for k in range(15 * 60, 0, -1):
                px *= math.exp(self._rng.gauss(0, 0.00012))
                self._add_tick(now - timedelta(seconds=k), px, abs(self._rng.gauss(0.01, 0.01)))
            self._synth_px = px
            self._q_cursor = now
        for _ in range(5):  # noqa: B007
            self._synth_px *= math.exp(self._rng.gauss(0, 0.00012))
            self._add_tick(now, self._synth_px, abs(self._rng.gauss(0.002, 0.002)))
        spread = self._synth_px * 0.00004
        self.bid, self.ask, self.last, self.last_ts = (self._synth_px - spread, self._synth_px + spread,
                                                       self._synth_px, now)
        # 每分钟把已完成的分钟写入 min_bars
        m0 = pd.Timestamp(now).floor("1min")
        lastm = self.min_bars["ts"].max()
        if m0 - pd.Timedelta(minutes=1) > lastm:
            cm = self._sec_frame(int((lastm + pd.Timedelta(minutes=1)).timestamp()), int(m0.timestamp()) - 1)
            if len(cm):
                b = cm.set_index("ts").resample("1min").agg({"open": "first", "high": "max", "low": "min",
                                                              "close": "last", "volume": "sum"}).dropna().reset_index()
                self.min_bars = pd.concat([self.min_bars, b]).drop_duplicates("ts").reset_index(drop=True)

    # ------------------------------------------------------------------ 线程
    def tick(self) -> None:
        try:
            if self.source == "synthetic":
                with self.lock:
                    self._tick_synth()
            else:
                self._tick_alpaca()
            self.status = {"ok": True, "msg": "实时更新中", "updated": datetime.now(timezone.utc).isoformat(),
                           "source": self.source}
        except Exception as e:  # noqa: BLE001
            self.status = {"ok": False, "msg": f"行情获取失败：{e}"[:200],
                           "updated": self.status.get("updated"), "source": self.source}
            log.warning("feed error: %s", e)

    def _loop(self) -> None:
        while True:
            t0 = time.time()
            self.tick()
            try:
                from . import sim
                sim.ENGINE.step(self)
            except Exception as e:  # noqa: BLE001
                log.error("sim step error: %s", e)
            time.sleep(max(0.2, 1.0 - (time.time() - t0)))

    def start(self) -> None:
        if self._started or os.environ.get("AIQUANT_DISABLE_LIVE_FEED") == "1":
            return
        self._started = True
        threading.Thread(target=self._loop, daemon=True, name="aiquant-live-feed").start()

    # ------------------------------------------------------------------ 读取
    def _sec_frame(self, lo: int | None = None, hi: int | None = None) -> pd.DataFrame:
        with self.lock:
            keys = [k for k in self.sec_order if (lo is None or k >= lo) and (hi is None or k <= hi)]
            rows = [[k] + self.sec[k] for k in keys if k in self.sec]
        df = pd.DataFrame(rows, columns=["epoch", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["epoch"], unit="s", utc=True)
        return df.drop(columns=["epoch"])

    def candles(self, tf_sec: int, limit: int = 300) -> pd.DataFrame:
        """返回 [ts, open, high, low, close, volume, complete]，ts 为K线开始时间（UTC）。"""
        now = pd.Timestamp(datetime.now(timezone.utc))
        agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        if tf_sec < 60:
            s = self._sec_frame(int((now - pd.Timedelta(seconds=tf_sec * (limit + 2))).timestamp()))
            if s.empty:
                return s.assign(complete=[])
            c = s.set_index("ts").resample(f"{tf_sec}s", origin="epoch").agg(agg).dropna().reset_index()
        else:
            with self.lock:
                m = self.min_bars.copy()
            span = pd.Timedelta(seconds=tf_sec * (limit + 2))
            m = m[m["ts"] >= now - span - pd.Timedelta(minutes=1)]
            # 用秒级数据补上分钟K线尚未覆盖的最近部分（含当前进行中的分钟）
            last_min = m["ts"].max() if len(m) else now - span
            s = self._sec_frame(int((last_min + pd.Timedelta(minutes=1)).timestamp()))
            if len(s):
                sm = s.set_index("ts").resample("1min").agg(agg).dropna().reset_index()
                m = pd.concat([m, sm]).drop_duplicates("ts", keep="first")
            if m.empty:
                return m.assign(complete=[])
            c = m.set_index("ts").resample(f"{tf_sec}s", origin="epoch").agg(agg).dropna().reset_index()
        c["complete"] = c["ts"] + pd.Timedelta(seconds=tf_sec) <= now
        return c.tail(limit).reset_index(drop=True)

    def snapshot(self) -> dict:
        px = self.last
        mid = (self.bid + self.ask) / 2 if self.bid and self.ask else px
        ref = None
        with self.lock:
            if len(self.min_bars):
                day_ago = pd.Timestamp(datetime.now(timezone.utc) - timedelta(hours=24))
                prev = self.min_bars[self.min_bars["ts"] <= day_ago]
                ref = float(prev["close"].iloc[-1]) if len(prev) else float(self.min_bars["close"].iloc[0])
        return {"bid": self.bid, "ask": self.ask, "mid": mid, "last": px, "ts": self.last_ts,
                "chg24": ((mid / ref - 1) * 100) if (mid and ref) else None, "status": self.status}


FEED = LiveFeed()
