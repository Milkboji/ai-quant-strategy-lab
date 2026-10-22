"""把 ai_quant_lab 的 Alpaca BTC/USD 5 分钟行情转换为 Freqtrade 合约回测数据（离线，无需连接交易所）。
假设：用 BTC/USD 现货价格代替 BTC/USDT 永续价格；资金费率按 0.01%/8 小时（交易所基准值）计。"""
from pathlib import Path
import numpy as np, pandas as pd
from freqtrade.data.history import get_datahandler
from freqtrade.enums import CandleType
SRC = Path(__file__).resolve().parent.parent / "data" / "market_cache" / "alpaca_us_BTCUSD_5m.csv.gz"
df = pd.read_csv(SRC); df["ts"] = pd.to_datetime(df["ts"], utc=True); s = df.set_index("ts")
def rs(rule):
    o = s.resample(rule, label="left", closed="left").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    return o.reset_index().rename(columns={"ts": "date"})
dh = get_datahandler(Path(__file__).resolve().parent / "user_data" / "data" / "binance", "feather")
pair = "BTC/USDT:USDT"
for tf, rule in [("1d", "1D"), ("4h", "4h"), ("1h", "1h")]:
    dh.ohlcv_store(pair, tf, rs(rule), CandleType.FUTURES)
m = rs("1h"); dh.ohlcv_store(pair, "1h", m, CandleType.MARK)
f = m[["date"]].copy(); f["funding_rate"] = np.where(f["date"].dt.hour % 8 == 0, 0.0001, 0.0)
dh.ohlcv_store(pair, "1h", f, CandleType.FUNDING_RATE)
print(f"已转换：{len(df)} 根 5 分钟K线 → 日线/4小时/1小时 合约数据")
