"""交易节奏（Rhythm）：在策略信号之上的一层统一纪律，回测与模拟盘使用同一套规则。

不改变策略的入场/退出信号本身，只决定“这一次信号要不要做、做多大”：
- 冷却：止损离场后等待一段时间再开新仓（避免震荡市里被反复打脸），普通离场后可设较短冷却。
- 连亏刹车：连续亏损 N 笔后暂停新开仓一段时间。
- 大势过滤：BTC 日线收盘低于 N 日均线时不开新仓（只影响开仓，持仓照常按规则离场）。
- 波动率定仓：近期波动越大，单笔仓位越小（年化波动率目标 / 实际波动率，限制在 [下限, 1]）。
- 每日开仓上限：同一策略一天（UTC）最多开仓次数。

所有判断只使用已收盘的日线和已发生的成交，不使用未来数据。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Rhythm:
    cooldown_hours_after_stop: float = 0.0
    cooldown_hours_after_exit: float = 0.0
    loss_streak_n: int = 0
    loss_streak_pause_hours: float = 0.0
    regime_sma_days: int = 0
    vol_target_pct: float = 0.0
    vol_lookback_days: int = 20
    vol_min_mult: float = 0.3
    max_entries_per_day: int = 0

    @property
    def active(self) -> bool:
        return any([self.cooldown_hours_after_stop, self.cooldown_hours_after_exit, self.loss_streak_n,
                    self.regime_sma_days, self.vol_target_pct, self.max_entries_per_day])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Rhythm":
        if not d:
            return cls()
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    def describe(self, unit: str = "小时", asset: str = "BTC") -> list[str]:
        """unit：时长单位（加密货币「小时」，美股「个交易日」）。"""
        out = []
        if self.cooldown_hours_after_stop:
            out.append(f"止损后冷却 {self.cooldown_hours_after_stop:g} {unit}")
        if self.cooldown_hours_after_exit:
            out.append(f"普通离场后冷却 {self.cooldown_hours_after_exit:g} {unit}")
        if self.loss_streak_n:
            out.append(f"连亏 {self.loss_streak_n} 笔暂停开仓 {self.loss_streak_pause_hours:g} {unit}")
        if self.regime_sma_days:
            out.append(f"{asset} 日线低于 {self.regime_sma_days} 日均线不开新仓")
        if self.vol_target_pct:
            out.append(f"波动率定仓：年化目标 {self.vol_target_pct:g}%，最低 {self.vol_min_mult:.0%} 仓位")
        if self.max_entries_per_day:
            out.append(f"每天最多开仓 {self.max_entries_per_day} 次")
        return out or ["不限制（每个信号都执行）"]


STOP_REASONS = ("止损", "移动止损", "止损/移动止损")


def daily_context(df5: pd.DataFrame, rh: Rhythm, market=None) -> pd.DataFrame:
    """按「日」聚合，返回每个已收盘日线的 close_time、regime_ok、vol_mult。

    加密货币：UTC 自然日，close_time = 次日 00:00 UTC，年化 √365。
    美股：美东交易日，close_time = 当天收盘（16:00，半日市 13:00），年化 √252。
    某时刻 t 只能使用 close_time <= t 的日线。df5 可以是 5 分钟 K 线，也可以直接是日线（列 ts、close）。
    """
    if df5.empty:
        return pd.DataFrame(columns=["close_time", "regime_ok", "vol_mult"])
    d = df5[["ts", "close"]].copy()
    d["ts"] = pd.to_datetime(d["ts"], utc=True)
    us = market is not None and getattr(market, "key", "crypto") != "crypto"
    if us:
        from .. import market as mk
        d["day"] = d["ts"].dt.tz_convert(mk.ET).dt.date
        day = d.groupby("day")["close"].last()
        ct = [pd.Timestamp(mk.session_close(x)) for x in day.index]
        out = pd.DataFrame({"close_time": pd.to_datetime(ct, utc=True), "close": day.values})
        ann = float(market.periods_per_year)
    else:
        day = d.set_index("ts")["close"].resample("1D").last().dropna()
        out = pd.DataFrame({"close_time": day.index + pd.Timedelta(days=1), "close": day.values})
        ann = float(getattr(market, "periods_per_year", 365) or 365)
    if rh.regime_sma_days:
        sma = out["close"].rolling(rh.regime_sma_days, min_periods=rh.regime_sma_days).mean()
        out["regime_ok"] = (out["close"] >= sma) | sma.isna()
    else:
        out["regime_ok"] = True
    if rh.vol_target_pct:
        r = np.log(out["close"]).diff()
        vol = r.rolling(rh.vol_lookback_days, min_periods=max(5, rh.vol_lookback_days // 2)).std() * np.sqrt(ann) * 100
        m = (rh.vol_target_pct / vol).clip(lower=rh.vol_min_mult, upper=1.0)
        out["vol_mult"] = m.fillna(1.0)
    else:
        out["vol_mult"] = 1.0
    return out[["close_time", "regime_ok", "vol_mult"]]


def lookup(ctx: pd.DataFrame, times) -> tuple[np.ndarray, np.ndarray]:
    """对一组时刻（UTC）返回 (regime_ok, vol_mult)，只使用此前已收盘日线。"""
    t = pd.to_datetime(pd.Series(times), utc=True)
    if ctx.empty:
        return np.ones(len(t), bool), np.ones(len(t))
    q = pd.DataFrame({"t": t.values, "k": range(len(t))}).sort_values("t")
    q["t"] = pd.to_datetime(q["t"], utc=True)
    c = ctx.copy()
    c["close_time"] = pd.to_datetime(c["close_time"], utc=True)
    m = pd.merge_asof(q, c.sort_values("close_time"), left_on="t", right_on="close_time", direction="backward")
    m = m.sort_values("k")
    ok = m["regime_ok"].fillna(True).astype(bool).to_numpy()
    vm = m["vol_mult"].fillna(1.0).astype(float).to_numpy()
    return ok, vm


class Gate:
    """节奏状态机：回测与实盘共用。时间均为 UTC pandas Timestamp。"""

    def __init__(self, rh: Rhythm, market=None):
        self.rh = rh
        self.market = market        # None / 加密货币：时长按小时；美股：按交易日（休市日不算）
        self.block_until: pd.Timestamp | None = None
        self.block_why = ""
        self.streak = 0
        self.entries_day: dict[str, int] = {}

    def on_exit(self, t: pd.Timestamp, reason: str, pnl: float) -> None:
        rh = self.rh
        hrs = rh.cooldown_hours_after_stop if reason in STOP_REASONS else rh.cooldown_hours_after_exit
        if hrs:
            self._extend(self._after(t, hrs), "止损后冷却" if reason in STOP_REASONS else "离场后冷却")
        self.streak = self.streak + 1 if pnl <= 0 else 0
        if rh.loss_streak_n and self.streak >= rh.loss_streak_n:
            self._extend(self._after(t, rh.loss_streak_pause_hours), f"连亏 {self.streak} 笔刹车")
            self.streak = 0

    def _after(self, t: pd.Timestamp, amount: float) -> pd.Timestamp:
        if self.market is None or getattr(self.market, "duration_unit", "hours") == "hours":
            return t + pd.Timedelta(hours=amount)
        return self.market.after(t, amount)

    def _extend(self, until: pd.Timestamp, why: str) -> None:
        if self.block_until is None or until > self.block_until:
            self.block_until, self.block_why = until, why

    def _day(self, t: pd.Timestamp) -> str:
        """「每日开仓上限」按哪一天计数：加密货币 UTC 日，美股美东交易日。"""
        if self.market is not None and getattr(self.market, "key", "crypto") != "crypto":
            return pd.Timestamp(t).tz_convert("America/New_York").strftime("%Y-%m-%d")
        return t.strftime("%Y-%m-%d")

    def on_entry(self, t: pd.Timestamp) -> None:
        k = self._day(t)
        self.entries_day[k] = self.entries_day.get(k, 0) + 1

    def check(self, t: pd.Timestamp, regime_ok: bool = True) -> str | None:
        """返回阻止开仓的原因；None = 允许。"""
        if self.block_until is not None and t < self.block_until:
            return f"{self.block_why}（至 {(self.block_until + pd.Timedelta(hours=8)):%m-%d %H:%M} 北京时间）"
        if self.rh.regime_sma_days and not regime_ok:
            return f"大势过滤：日线低于 {self.rh.regime_sma_days} 日均线"
        if self.rh.max_entries_per_day and self.entries_day.get(self._day(t), 0) >= self.rh.max_entries_per_day:
            return f"今日开仓已达 {self.rh.max_entries_per_day} 次"
        return None
