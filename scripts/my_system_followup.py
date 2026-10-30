"""「我的交易系统 v1」10-30 下午的补充研究（只用训练期）：
1. 保本触发倍数：1 倍（原）/ 2 倍 / 关闭，在 2 倍和关闭里选收益回撤比更高的一档；
2. 加仓：统计原定义实际触发次数和平均总仓位；放宽定义只试两档 a / b，收益回撤比更高才采用。
用法：python scripts/my_system_followup.py <数据目录（含 history/ 与 etf_daily/）> <输出.json>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import my_system_research as R  # noqa: E402

from aiquant import market as mk  # noqa: E402
from aiquant.engine import my_system as ms  # noqa: E402
from aiquant.engine.execution import CostModel  # noqa: E402
import pandas as pd  # noqa: E402

CHOSEN = {"crypto": dict(box_days=30, half_r=5.0, time_stop=10), "us_equity": dict(box_days=60, half_r=5.0, time_stop=20)}


def row(r: dict) -> dict:
    m = r["metrics"]
    s = R.summarize_ms(r)
    return {"cagr_pct": s["cagr_pct"], "max_drawdown_pct": s["max_drawdown_pct"], "ret_dd": s["ret_dd"],
            "entries": m["entries"], "adds": m["adds"], "avg_round_peak_pct": round(m["avg_round_peak_pct"], 1),
            "avg_held_pct": round(m["avg_held_pct"], 1), "yearly": s["yearly"], "fee_share_pct": s["fee_share_pct"],
            "avg_hold_days": s["avg_hold_days"]}


def main() -> None:
    data, out = sys.argv[1], sys.argv[2]
    _, btc = R.crypto_daily(str(Path(data) / "history" / "BTCUSD_5m.csv.gz"))
    _, eth = R.crypto_daily(str(Path(data) / "history" / "ETHUSD_5m.csv.gz"))
    s, e = (pd.Timestamp(x, tz="UTC") for x in R.CRYPTO_TRAIN)
    cost = CostModel(25.0, 2.0, 1.0)

    def run(**kw):
        p = ms.Params(**dict(CHOSEN["crypto"], **kw))
        return row(ms.run({"BTC/USD": btc, "ETH/USD": eth}, btc, p, cost=cost, start=s, end=e, market_key="crypto",
                          rhythm=R.CRYPTO_RH, market=mk.CRYPTO))
    res = {"breakeven": {f"{b:g}": run(be_r=b) for b in (1.0, 2.0, 0.0)}}
    be = max((2.0, 0.0), key=lambda b: res["breakeven"][f"{b:g}"]["ret_dd"] or -9)
    res["chosen_be_r"] = be
    res["adds"] = {rule: run(be_r=be, add_rule=rule) for rule in ("quiet3", "a", "b")}
    base = res["adds"]["quiet3"]["ret_dd"] or -9
    best = max(("a", "b"), key=lambda r_: res["adds"][r_]["ret_dd"] or -9)
    res["chosen_add_rule"] = best if (res["adds"][best]["ret_dd"] or -9) > base else "quiet3"
    # 美股：原参数下加仓触发次数和平均仓位（只作统计）
    ub = {x: R.us_bars(data, x) for x in ("QQQ", "SPY", "IWM")}
    us, ue = (pd.Timestamp(x, tz="UTC") for x in R.US_TRAIN)
    res["us_original"] = row(ms.run(ub, ub["SPY"], ms.Params(**CHOSEN["us_equity"]), cost=mk.US_EQUITY.cost(), start=us,
                                    end=ue, market_key="us_equity", rhythm=None, market=mk.US_EQUITY))
    Path(out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    for k in ("breakeven", "adds"):
        for name, m in res[k].items():
            print(k, name, {x: m[x] for x in ("cagr_pct", "max_drawdown_pct", "ret_dd", "entries", "adds",
                                              "avg_round_peak_pct", "avg_held_pct")})
    print("chosen", res["chosen_be_r"], res["chosen_add_rule"])
    print("us", {x: res["us_original"][x] for x in ("ret_dd", "entries", "adds", "avg_round_peak_pct", "avg_held_pct")})


if __name__ == "__main__":
    main()
