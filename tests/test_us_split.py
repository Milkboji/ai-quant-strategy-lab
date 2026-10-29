"""美股分区 v1：日期固定、不随时间滚动；测试期截止 2026-10-28（含），之后的新数据只算「前向新数据」，不并入测试期。"""
from datetime import datetime, timedelta, timezone

import pandas as pd

from aiquant import market as mk
from aiquant import research


def test_us_split_v1_is_frozen_and_later_data_is_forward_only(env, monkeypatch):
    sp = research.us_split(1)
    assert sp["test_start"][:10] == "2026-06-23" and sp["test_end"][:10] == "2026-10-29"     # 结束日不含 → 截止 10-28
    assert len(mk.trading_days(pd.Timestamp(sp["test_start"][:10]).date(), pd.Timestamp("2026-10-28").date())) == 90
    # 时间过去也不滚动：分区只看版本号，不看今天
    monkeypatch.setattr(research, "_utc_today", lambda: datetime(2027, 3, 1, tzinfo=timezone.utc))
    assert research.get_split(mk.US_EQUITY) == research.us_split(1)
    utc = lambda s: datetime.fromisoformat(s + "T00:00:00+00:00")   # noqa: E731
    assert research.split_label(utc("2026-10-29"), utc("2026-12-31"), mk.US_EQUITY) == "前向新数据"
    assert research.split_label(utc("2026-07-01"), utc("2026-10-29"), mk.US_EQUITY) == "测试集"
    assert research.split_label(utc("2026-07-01"), utc("2026-11-30"), mk.US_EQUITY) == "跨越测试集"
    assert research.split_label(utc("2025-08-01"), utc("2026-06-23"), mk.US_EQUITY) == "验证集"
    # ETF 挑战者的留出检验：未暴露谱系用 test_end（10-29）起的前向数据，不含测试期
    from aiquant import challenger
    idx = pd.bdate_range("2017-01-03", "2027-02-26")
    df = pd.DataFrame({"sig_close": 1.0, "open": 1.0, "close": 1.0}, index=idx)
    from aiquant import holdout
    monkeypatch.setattr(holdout, "exposure", lambda lin: None)
    monkeypatch.setattr(challenger, "etf_backtest", lambda d, sma, band, a, b: {"ret": 0.0, "dd": 0.0, "days": 0,
                                                                               "switches": 0})
    ho = challenger.etf_holdout(df, {"sma": 150, "band": 0.02}, {"sma": 170, "band": 0.02})
    assert ho["kind"] == "前向新数据" and ho["start"] == "2026-10-29" and ho["split_version"] == 1
    # 已暴露的谱系（QQQ→QLD）：用上线日之后的前向数据
    monkeypatch.setattr(holdout, "exposure", lambda lin: {"forward_from": "2026-10-21"})
    ho = challenger.etf_holdout(df, {"sma": 150, "band": 0.02}, {"sma": 170, "band": 0.02})
    assert ho["kind"] == "上线后前向数据" and ho["start"] == "2026-10-22"
