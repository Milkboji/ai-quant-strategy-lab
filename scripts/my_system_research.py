"""「我的交易系统 v1」研究（只用训练期）：参数只在给定几档里选，和现有主力日线策略、买入持有比较，逐条规则做开 / 关对比。

用法：python scripts/my_system_research.py <数据目录（含 history/ 与 etf_daily/）> <输出.json>
- 加密货币：BTC/USD、ETH/USD 5 分钟历史 → UTC 日线；训练期 = 数据分区验证集开始之前（预热一年后起算）。
- 美股：QQQ、SPY、IWM 日线（复权）；训练期 = 美股分区 v1 的训练期 2017-01-01 ~ 2025-07-30。
- 只读本地文件，不访问网络；不写数据库（不产生留出集触及）。
"""
from __future__ import annotations

import itertools
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("AIQUANT_ROOT", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from aiquant import market as mk  # noqa: E402
from aiquant.data import market_data as md  # noqa: E402
from aiquant.data import us_daily  # noqa: E402
from aiquant.engine import backtest as bt  # noqa: E402
from aiquant.engine import my_system as ms  # noqa: E402
from aiquant.engine import regime_bt  # noqa: E402
from aiquant.engine.execution import CostModel  # noqa: E402
from aiquant.engine.rhythm import Rhythm  # noqa: E402
from aiquant.strategy.schema import validate  # noqa: E402

GRID = {"box_days": (20, 30, 60), "half_r": (2.0, 3.0, 5.0), "time_stop": (10, 20, 40)}
CRYPTO_TRAIN = ("2022-01-01", "2026-05-23")     # 数据 2021-01 起，预热一年；验证集 2026-05-23 开始（电脑上冻结的分区）
US_TRAIN = ("2017-01-01", "2025-07-31")         # 美股分区 v1 训练期
CRYPTO_RH = Rhythm(cooldown_hours_after_stop=72, loss_streak_n=3, loss_streak_pause_hours=72)   # 电脑上现行的加密货币节奏
MAIN_CRYPTO = {"alloc_pct": 100.0, "direction": "long_only", "name": "策略1", "signal_tf": 1440,
               "indicators": [{"id": "ema9", "type": "EMA", "params": {"period": 9}},
                              {"id": "ema21", "type": "EMA", "params": {"period": 21}},
                              {"id": "bb_mid", "type": "BB_MID", "params": {"period": 20, "std": 2}}],
               "entry": {"mode": "all", "conditions": [{"left": {"ref": "ema9"}, "op": "cross_above", "right": {"ref": "ema21"}}]},
               "exit": {"mode": "any", "conditions": [{"left": {"ref": "ema9"}, "op": "cross_below", "right": {"ref": "ema21"}},
                                                      {"left": {"ref": "close"}, "op": "<", "right": {"ref": "bb_mid"}}]}}
ABLATE = [("regime", "大势过滤（200 日均线）"), ("quiet", "低波动过滤"), ("box_atr", "加上区间高度 ≤ 4×ATR（对照）"), ("adds", "加仓"),
          ("half", "卖一半 + 买回"), ("nearhigh", "接近前高卖出"), ("time", "时间止损"), ("be", "保本止损"),
          ("trail", "移动止盈")]


def crypto_daily(path: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    d = md.resample(df, 1440)
    d = d[d["complete"]].rename(columns={"open_time": "ts"})[["ts", "open", "high", "low", "close", "volume"]]
    return df, d.reset_index(drop=True)


def us_bars(data: str, sym: str) -> pd.DataFrame:
    raw = pd.read_csv(Path(data) / "etf_daily" / f"{sym}.csv")
    b = us_daily.to_bars(raw)
    return b[["ts", "open", "high", "low", "close", "volume"]]


def curve_metrics(eq: pd.Series, init: float, trades: int, fees: float, hold_days: float, pnl_abs: float) -> dict:
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    final = float(eq.iloc[-1])
    cagr = ((final / init) ** (1 / years) - 1) * 100
    dd = float(((eq / eq.cummax()) - 1).min() * 100)
    yearly = []
    for y, g in eq.groupby(eq.index.year):
        prev = eq[eq.index < g.index[0]]
        base = float(prev.iloc[-1]) if len(prev) else init
        yearly.append({"year": int(y), "ret": round((float(g.iloc[-1]) / base - 1) * 100, 2)})
    return {"cagr_pct": round(cagr, 2), "max_drawdown_pct": round(dd, 2), "ret_dd": round(cagr / abs(dd), 3) if dd < 0 else None,
            "net_return_pct": round((final / init - 1) * 100, 2), "trades": trades,
            "fee_share_pct": round(fees / (pnl_abs + fees) * 100, 2) if (pnl_abs + fees) else 0.0,
            "avg_hold_days": round(hold_days, 1), "yearly": yearly}


def summarize_ms(r: dict) -> dict:
    m = r["metrics"]
    eq = pd.Series([v for _, v in r["equity"]], index=pd.to_datetime([t for t, _ in r["equity"]], utc=True))
    pnl_abs = abs(sum(t["pnl"] for t in r["trades"]))
    out = curve_metrics(eq, m["initial_cash"], m["entries"], m["fees"], m["avg_hold_days"], pnl_abs)
    out.update(tranche_trades=m["tranche_trades"], blocked=m["blocked"],
               tranche_kinds=pd.Series([t["tranche"] for t in r["trades"]]).value_counts().to_dict() if r["trades"] else {},
               exit_reasons=pd.Series([t["exit_reason"] for t in r["trades"]]).value_counts().head(12).to_dict()
               if r["trades"] else {})
    return out


def stability(y: list[dict]) -> tuple[int, float]:
    rets = [x["ret"] for x in y]
    return sum(1 for r in rets if r < 0), float(np.std(rets))


def pick(rows: list[dict]) -> dict:
    """先按亏损年份数最少（逐年最稳定），再按收益回撤比最高。"""
    ok = [r for r in rows if r["m"]["ret_dd"] is not None and r["m"]["trades"] >= 5]
    best_loss = min(stability(r["m"]["yearly"])[0] for r in ok)
    cand = [r for r in ok if stability(r["m"]["yearly"])[0] == best_loss]
    return max(cand, key=lambda r: (r["m"]["ret_dd"], -stability(r["m"]["yearly"])[1]))


def research(market_key: str, bars: dict, bench: pd.DataFrame, cost: CostModel, rh, window, market) -> dict:
    s, e = (pd.Timestamp(x, tz="UTC") for x in window)
    rows = []
    for box, half, ts in itertools.product(*GRID.values()):
        p = ms.Params(box_days=box, half_r=half, time_stop=ts)
        r = ms.run(bars, bench, p, cost=cost, start=s, end=e, market_key=market_key, rhythm=rh, market=market)
        rows.append({"params": p.to_dict(), "m": summarize_ms(r)})
    best = pick(rows)
    bp = ms.Params.from_dict(best["params"])
    full = ms.run(bars, bench, bp, cost=cost, start=s, end=e, market_key=market_key, rhythm=rh, market=market)
    abl = []
    for key, name in ABLATE:
        r = ms.run(bars, bench, bp, cost=cost, start=s, end=e, market_key=market_key, rhythm=rh, market=market,
                   off=frozenset({key}))
        abl.append({"rule": name, "key": key, "m": summarize_ms(r)})
    default = next(r for r in rows if r["params"]["box_days"] == 30 and r["params"]["half_r"] == 3.0
                   and r["params"]["time_stop"] == 20)
    return {"grid": rows, "chosen": best, "default": default, "ablation": abl,
            "chosen_trades_sample": full["trades"][:40], "chosen_events_sample": full["events"][:40],
            "assumptions": full["assumptions"]}


def main() -> None:
    data, out = sys.argv[1], sys.argv[2]
    res = {"windows": {"crypto": CRYPTO_TRAIN, "us_equity": US_TRAIN}}
    # ---------------- 加密货币 ----------------
    btc5, btc = crypto_daily(str(Path(data) / "history" / "BTCUSD_5m.csv.gz"))
    _, eth = crypto_daily(str(Path(data) / "history" / "ETHUSD_5m.csv.gz"))
    ccost = CostModel(25.0, 2.0, 1.0)
    cr = research("crypto", {"BTC/USD": btc, "ETH/USD": eth}, btc, ccost, CRYPTO_RH, CRYPTO_TRAIN, mk.CRYPTO)
    s, e = (pd.Timestamp(x, tz="UTC") for x in CRYPTO_TRAIN)
    obj, errs = validate(MAIN_CRYPTO)
    assert not errs, errs
    r = bt.run(btc5, obj, 1440, None, bt.BTConfig(start=s, end=e, initial_cash=10_000, cost=ccost, rhythm=CRYPTO_RH))
    eq = pd.Series([v for _, v in r["equity"]], index=pd.to_datetime([t for t, _ in r["equity"]], utc=True))
    holds = [(pd.Timestamp(t["exit_fill_ts"]) - pd.Timestamp(t["entry_fill_ts"])).total_seconds() / 86400 for t in r["trades"]]
    cr["main"] = {"name": "策略1 v3（EMA9/21 金叉 + 布林中轨离场，日线，BTC）",
                  "m": curve_metrics(eq, 10_000, len(r["trades"]), r["metrics"]["fees"], float(np.mean(holds)) if holds else 0,
                                     abs(sum(t["pnl"] for t in r["trades"])))}
    bh = {}
    for name, d in (("BTC 买入持有", btc), ("ETH 买入持有", eth)):
        x = d[(d["ts"] >= s) & (d["ts"] < e)]
        eqb = pd.Series(x["close"].values / x["open"].iloc[0] * 10_000 * (1 - 0.0028), index=x["ts"])
        bh[name] = curve_metrics(eqb, 10_000, 1, 50.0, (e - s).days, abs(eqb.iloc[-1] - 10_000))
    cr["buy_hold"] = bh
    res["crypto"] = cr
    # ---------------- 美股 ----------------
    syms = ["QQQ", "SPY", "IWM"]
    ub = {x: us_bars(data, x) for x in syms}
    ucost = mk.US_EQUITY.cost()
    us = research("us_equity", ub, ub["SPY"], ucost, None, US_TRAIN, mk.US_EQUITY)
    df = pd.DataFrame({"sig_close": _by_date(ub["QQQ"])["close"], "open": _by_date(us_bars(data, "QLD"))["open"],
                       "close": _by_date(us_bars(data, "QLD"))["close"]}).dropna()
    sim = regime_bt.simulate(df, 150, 0.02, market=mk.US_EQUITY, rhythm=None)
    sim = sim[(sim.index >= pd.Timestamp(US_TRAIN[0])) & (sim.index < pd.Timestamp(US_TRAIN[1]))]
    eq = pd.Series((1 + sim["pr"]).cumprod().values * 10_000, index=pd.to_datetime(sim.index).tz_localize("UTC"))
    sw = sim["event"].isin(["buy", "sell"]).sum()
    fee_cost = float(sim["cost"].sum() * 10_000)
    holding = float((sim["size"] > 0).sum()) / max(1, (sim["event"] == "buy").sum())
    us["main"] = {"name": "ETF 择时（QQQ 收盘对 150 日均线 ±2% → QLD，现役参数）",
                  "m": curve_metrics(eq, 10_000, int((sim["event"] == "buy").sum()), fee_cost, holding,
                                     abs(eq.iloc[-1] - 10_000))}
    us["main"]["m"]["switches"] = int(sw)
    bh = {}
    s, e = (pd.Timestamp(x, tz="UTC") for x in US_TRAIN)
    for x in syms + ["QLD"]:
        d = ub.get(x) if x in ub else us_bars(data, x)
        d = d[(d["ts"] >= s) & (d["ts"] < e)]
        eqb = pd.Series(d["close"].values / d["open"].iloc[0] * 10_000, index=d["ts"])
        bh[f"{x} 买入持有"] = curve_metrics(eqb, 10_000, 1, 0.0, (e - s).days, abs(eqb.iloc[-1] - 10_000))
    us["buy_hold"] = bh
    res["us_equity"] = us
    Path(out).write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    for k in ("crypto", "us_equity"):
        c = res[k]
        print(k, "chosen", c["chosen"]["params"], {x: c["chosen"]["m"][x] for x in ("cagr_pct", "max_drawdown_pct", "ret_dd", "trades")})
        print("   main", {x: c["main"]["m"][x] for x in ("cagr_pct", "max_drawdown_pct", "ret_dd", "trades")})


def _by_date(b: pd.DataFrame) -> pd.DataFrame:
    x = b.copy()
    x.index = pd.to_datetime(x["ts"], utc=True).dt.tz_convert("America/New_York").dt.normalize().dt.tz_localize(None)
    return x


if __name__ == "__main__":
    main()
