"""逐笔回归比对：当前引擎在 BTC（5 分钟执行）和 ETF 择时上，与美股改造前的代码（提交 a72ed6a）逐笔一致。

golden 文件 tests/golden/engine_golden.json 由 a72ed6a 的代码在确定性合成行情（tests/golden/synth.py）上生成：
- BTC：5 个覆盖不同功能的策略（均线交叉 + 止损 + 移动止损、RSI + 止盈 + 最长持仓、布林带、多周期辅助周期、日线 MACD + 部分仓位），
  分别在「交易节奏 v1」和「不加节奏」下运行，比较每一笔的成交时间、价格、离场原因、盈亏。
- ETF 择时：旧的向量化回测（单边 5bp）与现在的 regime_bt（同样 5bp 成本、不加节奏）比较每日持仓和每日净收益。
真实数据上的比对（电脑上全部 33 个策略版本 × 2，共 17,959 笔；6 组 ETF × 3 组参数）见 docs/第六步后核对报告.md。
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

from aiquant.engine import backtest as bt
from aiquant.engine import regime_bt as rb
from aiquant.engine.execution import CostModel
from aiquant.engine.rhythm import Rhythm
from aiquant.market import CRYPTO, Market
from aiquant.strategy.schema import validate

from .golden import synth

GOLD = json.loads((Path(__file__).parent / "golden" / "engine_golden.json").read_text(encoding="utf-8"))
RH = Rhythm(cooldown_hours_after_stop=72, loss_streak_n=3, loss_streak_pause_hours=72, vol_target_pct=40,
            vol_lookback_days=20, vol_min_mult=0.3)


def test_btc_trades_identical_to_pre_us_engine():
    df = synth.btc_5m()
    start, end = pd.Timestamp("2024-03-01", tz="UTC"), pd.Timestamp("2025-02-20", tz="UTC")
    n = 0
    for i, sch in enumerate(synth.SCHEMAS):
        obj, errs = validate(sch)
        assert not errs
        for tag, rh in (("rhythm", RH), ("plain", None)):
            for market in (None, CRYPTO):            # 显式传加密货币市场配置，结果也必须相同
                cfg = bt.BTConfig(start=start, end=end, initial_cash=10_000, max_alloc_pct=100,
                                  cost=CostModel(25, 2, 1), rhythm=rh, market=market)
                r = bt.run(df, obj, sch["signal_tf"], sch.get("aux_tf"), cfg)
                got = [[t["entry_fill_ts"], round(t["entry_price"], 6), t["exit_fill_ts"], round(t["exit_price"], 6),
                        t["exit_reason"], round(t["pnl"], 6)] for t in r["trades"]]
                g = GOLD["btc"][f"{i}:{tag}"]
                assert got == g["trades"], f"策略 {i}（{sch['name']}，{tag}）逐笔不一致"
                assert round(r["metrics"]["final_equity"], 6) == g["final_equity"]
                n += len(got)
    assert n > 1000


def test_etf_timing_positions_and_returns_identical_to_old_vectorized_backtest():
    df = synth.etf_daily()
    m5 = Market("us_equity", "回归", 252, "America/New_York", "trading_days", fee_bps=0.0, slippage_bps=5.0, spread_bps=0.0)
    for key, g in GOLD["etf"].items():
        sma, band = key.split("/")
        sim = rb.simulate(df, int(sma), float(band), market=m5, rhythm=None)
        assert [int(x) for x in sim["size"].values] == g["positions"], key
        assert np.allclose(sim["pr"].values, np.array(g["pr"]), atol=1e-11, rtol=0), key
