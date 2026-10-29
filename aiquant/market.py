"""市场配置：加密货币 / 美股。回测、进化、模拟盘共用同一套代码，只用这里的参数区分市场。

| 项目 | 加密货币（BTC/ETH） | 美股（ETF） |
| --- | --- | --- |
| 年化周期数 | 365（7×24 交易） | 252（交易日） |
| 「日」的边界 | UTC 自然日 | 美东交易时段（收盘 16:00，半日市 13:00） |
| 冷却 / 刹车时长单位 | 小时 | 交易日（休市日不算） |
| 成本 | 手续费每边 25bp、滑点 2bp、点差 1bp | 佣金 0、滑点 4bp、点差 2bp，卖出另收监管费 0.28bp |

交易日历：优先使用 Alpaca Paper `/v2/calendar`（含节假日和半日市，缓存在 settings.us_calendar，每周刷新一次）；
取不到时使用内置的纽交所规则（元旦、马丁·路德·金纪念日、总统日、耶稣受难日、阵亡将士纪念日、六月节、独立日、
劳动节、感恩节、圣诞节；独立日前一天、感恩节次日、平安夜为 13:00 提前收盘）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .engine.execution import CostModel

log = logging.getLogger("aiquant.market")
ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Market:
    key: str
    name: str
    periods_per_year: int
    tz: str
    duration_unit: str                       # "hours" | "trading_days"
    fee_bps: float
    slippage_bps: float
    spread_bps: float
    sell_fee_bps: float = 0.0
    long_start: str = "2021-01-01"           # 长周期体检从哪一年开始
    symbols: tuple = field(default_factory=tuple)

    @property
    def unit_cn(self) -> str:
        return "小时" if self.duration_unit == "hours" else "个交易日"

    def cost(self, source: str = "冻结代理模型（未使用历史逐笔报价）") -> CostModel:
        return CostModel(fee_bps=self.fee_bps, slippage_bps=self.slippage_bps, spread_bps=self.spread_bps,
                         spread_source=source, sell_fee_bps=self.sell_fee_bps)

    def annual_vol_pct(self, daily_log_ret_std) -> float | pd.Series:
        """日收益率标准差 → 年化波动率（%）。加密货币 √365，美股 √252。"""
        return daily_log_ret_std * np.sqrt(self.periods_per_year) * 100

    def after(self, t: pd.Timestamp, amount: float) -> pd.Timestamp:
        """t 之后经过 amount 个时长单位的时刻（UTC）。
        加密货币：amount 小时。美股：t 所在交易日之后再过 amount 个完整交易日，下一个交易日开盘起解除
        （例：周五离场、冷却 3 个交易日 → 周一、二、三不开仓，周四开盘起恢复；节假日自动顺延）。"""
        t = pd.Timestamp(t)
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        if self.duration_unit == "hours":
            return t + pd.Timedelta(hours=float(amount))
        n = int(np.ceil(float(amount)))
        if n <= 0:
            return t
        d0 = t.tz_convert(ET).date()
        days = trading_days_after(d0, n + 1)
        return pd.Timestamp(session_open(days[-1]))


CRYPTO = Market("crypto", "加密货币", 365, "UTC", "hours", fee_bps=25.0, slippage_bps=2.0, spread_bps=1.0,
                long_start="2021-01-01", symbols=("BTC/USD", "ETH/USD"))
US_EQUITY = Market("us_equity", "美股", 252, "America/New_York", "trading_days", fee_bps=0.0, slippage_bps=4.0,
                   spread_bps=2.0, sell_fee_bps=0.28, long_start="2016-01-01")
MARKETS = {m.key: m for m in (CRYPTO, US_EQUITY)}


def get(key: str | None) -> Market:
    return MARKETS.get(key or "crypto", CRYPTO)


def of_symbol(sym: str | None) -> Market:
    """BTC/USD、ETH/USD → 加密货币；其他（股票 / ETF 代码）→ 美股。"""
    if not sym or "/" in sym:
        return CRYPTO
    return US_EQUITY


# ---------------------------------------------------------------------------
# 美股交易日历
# ---------------------------------------------------------------------------

def _observed(d: date) -> date:
    """周六的假日在周五休市，周日的在周一休市。"""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def _nth_weekday(y: int, m: int, wd: int, n: int) -> date:
    d = date(y, m, 1)
    d += timedelta(days=(wd - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(y: int, m: int, wd: int) -> date:
    d = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y, 12, 31)
    return d - timedelta(days=(d.weekday() - wd) % 7)


def _easter(y: int) -> date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = ((h + l_ - 7 * m + 114) % 31) + 1
    return date(y, month, day)


# 临时休市（国葬日、飓风等），规则推算不出来，单独列出
SPECIAL_CLOSURES = {date(2007, 1, 2), date(2012, 10, 29), date(2012, 10, 30), date(2018, 12, 5), date(2025, 1, 9)}


@lru_cache(maxsize=64)
def nyse_rules(y: int) -> tuple[frozenset, frozenset]:
    """内置纽交所规则：(休市日, 13:00 提前收盘日)。"""
    hol = {_nth_weekday(y, 1, 0, 3), _nth_weekday(y, 2, 0, 3), _easter(y) - timedelta(days=2),
           _last_weekday(y, 5, 0), _nth_weekday(y, 9, 0, 1), _nth_weekday(y, 11, 3, 4),
           _observed(date(y, 7, 4)), _observed(date(y, 12, 25))}
    ny = date(y, 1, 1)
    if ny.weekday() != 5:          # 元旦落在周六时不补休（前一年 12-31 照常交易）
        hol.add(_observed(ny))
    if y >= 2022:
        hol.add(_observed(date(y, 6, 19)))
    hol |= {d for d in SPECIAL_CLOSURES if d.year == y}
    early = set()
    j3 = date(y, 7, 3)
    if j3.weekday() < 5 and j3 not in hol:
        early.add(j3)
    early.add(_nth_weekday(y, 11, 3, 4) + timedelta(days=1))
    c24 = date(y, 12, 24)
    if c24.weekday() < 5 and c24 not in hol:
        early.add(c24)
    return frozenset(hol), frozenset(early)


def _cached() -> dict[str, tuple[str, str]]:
    try:
        from . import db
        c = db.get_setting("us_calendar") or {}
        return {d: (o, cl) for d, o, cl in (c.get("days") or [])}
    except Exception:  # noqa: BLE001
        return {}


_CACHE: dict = {"at": None, "days": None}


def _days_map() -> dict[str, tuple[str, str]]:
    now = datetime.now(timezone.utc)
    if _CACHE["days"] is None or _CACHE["at"] is None or (now - _CACHE["at"]).total_seconds() > 600:
        _CACHE["days"], _CACHE["at"] = _cached(), now
    return _CACHE["days"]


def invalidate_cache() -> None:
    _CACHE["days"], _CACHE["at"] = None, None


def session(d: date) -> tuple[time, time] | None:
    """某个美东日期的交易时段 (开盘, 收盘)；休市返回 None。"""
    m = _days_map()
    if m:
        lo, hi = min(m), max(m)
        k = d.isoformat()
        if lo <= k <= hi:
            if k not in m:
                return None
            o, c = m[k]
            return time.fromisoformat(o), time.fromisoformat(c)
    if d.weekday() >= 5:
        return None
    hol, early = nyse_rules(d.year)
    if d in hol:
        return None
    return time(9, 30), (time(13, 0) if d in early else time(16, 0))


def is_trading_day(d: date) -> bool:
    return session(d) is not None


def is_half_day(d: date) -> bool:
    s = session(d)
    return bool(s and s[1] < time(16, 0))


def trading_days_after(d: date, n: int) -> list[date]:
    out, cur = [], d
    while len(out) < n:
        cur += timedelta(days=1)
        if is_trading_day(cur):
            out.append(cur)
    return out


def trading_days(start: date, end: date) -> list[date]:
    out, cur = [], start
    while cur <= end:
        if is_trading_day(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


def session_open(d: date) -> datetime:
    s = session(d) or (time(9, 30), time(16, 0))
    return datetime.combine(d, s[0], ET).astimezone(timezone.utc)


def session_close(d: date) -> datetime:
    s = session(d) or (time(9, 30), time(16, 0))
    return datetime.combine(d, s[1], ET).astimezone(timezone.utc)


def refresh_calendar(force: bool = False) -> bool:
    """从 Alpaca Paper 拉交易日历（只读 GET），每周一次。成功返回 True。"""
    from . import db
    c = db.get_setting("us_calendar") or {}
    if not force and c.get("fetched_at"):
        try:
            if datetime.now(timezone.utc) - datetime.fromisoformat(c["fetched_at"]) < timedelta(days=7):
                return False
        except ValueError:
            pass
    last_try = db.get_setting("us_calendar_attempt")
    if not force and last_try:
        try:
            if datetime.now(timezone.utc) - datetime.fromisoformat(last_try) < timedelta(hours=6):
                return False      # 失败后 6 小时再试，避免每轮循环都请求
        except ValueError:
            pass
    db.set_setting("us_calendar_attempt", db.now_iso())
    from .data import alpaca
    end = (datetime.now(timezone.utc) + timedelta(days=400)).strftime("%Y-%m-%d")
    code, b = alpaca.paper_get("/v2/calendar", {"start": "2015-01-01", "end": end})
    if code != 200 or not isinstance(b, list) or not b:
        return False
    days = [[x["date"], str(x["open"])[:5], str(x["close"])[:5]] for x in b if x.get("date")]
    db.set_setting("us_calendar", {"fetched_at": db.now_iso(), "source": "alpaca", "days": days})
    invalidate_cache()
    return True
