"""TrendCoreFutures — 日线趋势核心策略（BTC 永续合约版）

来源：ai_quant_lab 研究结论 —— 训练年 BTC 下跌 32.7%，只做多时最有效的是
“日线 EMA 快线上穿慢线入场 + 盈利后移动止盈”的低频趋势跟随。
合约版新增：死叉时可做空（下跌趋势也能参与），杠杆由 LEVERAGE 控制，逐仓。

风险说明：杠杆放大的是收益和回撤两边；止损按“保证金亏损比例”计算（3 倍杠杆下 -9% 止损 ≈ 价格反向 3%）。
"""
import os
from datetime import datetime

import talib.abstract as ta
from pandas import DataFrame

from freqtrade.strategy import IStrategy, IntParameter, DecimalParameter, BooleanParameter


class TrendCoreFutures(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1d"
    can_short = os.environ.get("FT_SHORT", "0") == "1"   # 回测结论：加做空反而更差，默认只做多
    startup_candle_count = 60
    process_only_new_candles = True
    use_exit_signal = True

    LEVERAGE = float(os.environ.get("FT_LEVERAGE", "2"))

    # 风险以“保证金”计：按杠杆缩放，使价格止损约 4%（1 倍时 -4%，2 倍时 -8%，3 倍时 -12%）
    stoploss = -0.04 * float(os.environ.get("FT_LEVERAGE", "2"))
    trailing_stop = True
    trailing_only_offset_is_reached = True
    trailing_stop_positive = 0.03 * float(os.environ.get("FT_LEVERAGE", "2"))
    trailing_stop_positive_offset = 0.06 * float(os.environ.get("FT_LEVERAGE", "2"))
    minimal_roi = {"0": 100}

    fast = IntParameter(5, 20, default=9, space="buy")
    slow = IntParameter(15, 60, default=21, space="buy")
    short_filter = IntParameter(30, 200, default=50, space="sell")   # 只在价格低于该均线时做空
    use_short_filter = BooleanParameter(default=True, space="sell")

    def populate_indicators(self, df: DataFrame, metadata: dict) -> DataFrame:
        for p in set(list(range(5, 21)) + list(range(15, 61))):
            df[f"ema{p}"] = ta.EMA(df, timeperiod=p)
        for p in range(30, 201, 10):
            df[f"emaf{p}"] = ta.EMA(df, timeperiod=p)
        return df

    def _cross(self, df):
        f, s = df[f"ema{self.fast.value}"], df[f"ema{self.slow.value}"]
        up = (f > s) & (f.shift(1) <= s.shift(1))
        dn = (f < s) & (f.shift(1) >= s.shift(1))
        return up, dn

    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        up, dn = self._cross(df)
        df.loc[up & (df["volume"] > 0), ["enter_long", "enter_tag"]] = (1, "ema_golden_cross")
        sf = round(self.short_filter.value / 10) * 10
        filt = (df["close"] < df[f"emaf{sf}"]) if self.use_short_filter.value else True
        df.loc[dn & filt & (df["volume"] > 0), ["enter_short", "enter_tag"]] = (1, "ema_death_cross")
        return df

    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        up, dn = self._cross(df)
        df.loc[dn, ["exit_long", "exit_tag"]] = (1, "death_cross")
        df.loc[up, ["exit_short", "exit_tag"]] = (1, "golden_cross")
        return df

    def leverage(self, pair: str, current_time: datetime, current_rate: float, proposed_leverage: float,
                 max_leverage: float, entry_tag: str | None, side: str, **kwargs) -> float:
        return min(self.LEVERAGE, max_leverage)
