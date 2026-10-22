"""离线运行 Freqtrade 回测：用本地市场定义代替联网加载（云端无法访问交易所）。实盘/模拟盘请直接用 freqtrade 命令。"""
import json, sys
from pathlib import Path
from freqtrade.exchange import exchange as ex
from freqtrade.util import dt_ts

MKT = {"BTC/USDT:USDT": {"id": "BTCUSDT", "symbol": "BTC/USDT:USDT", "base": "BTC", "quote": "USDT", "settle": "USDT",
       "baseId": "BTC", "quoteId": "USDT", "settleId": "USDT", "type": "swap", "spot": False, "margin": False,
       "swap": True, "future": False, "option": False, "active": True, "contract": True, "linear": True,
       "inverse": False, "contractSize": 1.0, "expiry": None, "expiryDatetime": None, "strike": None, "optionType": None,
       "taker": 0.0005, "maker": 0.0002,
       "precision": {"amount": 0.001, "price": 0.1},
       "limits": {"amount": {"min": 0.001, "max": 1000}, "price": {"min": 556.8, "max": 4529764},
                  "cost": {"min": 100, "max": None}, "leverage": {"min": 1, "max": 125}},
       "info": {"symbol": "BTCUSDT", "pair": "BTCUSDT", "contractType": "PERPETUAL", "status": "TRADING"}}}

def reload_markets(self, force=False, *, load_leverage_tiers=True):
    if self._last_markets_refresh > 0:
        return
    for api in (self._api, self._api_async):
        api.set_markets(list(MKT.values()))
    self._markets = self._api.markets
    self._last_markets_refresh = dt_ts()
    if load_leverage_tiers and self.trading_mode.value == "futures":
        self.fill_leverage_tiers()

ex.Exchange.reload_markets = reload_markets
ex.Exchange.validate_timeframes = lambda self, tf: None
if __name__ == "__main__":
    from freqtrade.main import main
    sys.exit(main(sys.argv[1:]))
