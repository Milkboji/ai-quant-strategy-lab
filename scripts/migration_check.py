"""迁移前后对照：在电脑数据库的副本上（只读原库，不联网）执行「ETF 择时 → 通用美股日线执行器」迁移，并检查：
1. 迁移本身不提交任何订单；状态、节奏、借出原样沿用；台账期初 = 券商持仓。
2. BTC 各部署的资金额度：迁移前（原公式）与迁移后（两层分配）逐个相同。
3. 迁移后的下一次开盘检查（用真实 QQQ / QLD 日线）：ETF 择时没有额外交易（看多且持有、偏离 < 阈值 → 保持）。

用法：python scripts/migration_check.py <aiquant.db 副本> <etf_daily 目录> <输出.json> [检查日 YYYY-MM-DD]
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

src_db, etf_dir, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
check_day = sys.argv[4] if len(sys.argv) > 4 else None
root = tempfile.mkdtemp(prefix="aiquant_migr_")
os.environ["AIQUANT_ROOT"] = root
os.environ["AIQUANT_DISABLE_PAPER_LOOP"] = "1"
os.environ["AIQUANT_DISABLE_LIVE_FEED"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
(Path(root) / "data").mkdir(parents=True, exist_ok=True)
shutil.copy(src_db, Path(root) / "data" / "aiquant.db")
shutil.copytree(etf_dir, Path(root) / "data" / "etf_daily")

import pandas as pd  # noqa: E402

from aiquant import db, evolve  # noqa: E402
from aiquant import market as mk  # noqa: E402
from aiquant.paper import etf_trend as et  # noqa: E402
from aiquant.paper import runner as pr  # noqa: E402
from aiquant.paper import us_exec, us_ledger  # noqa: E402

db.init_db()
st = db.get_setting("etf_trend_state") or {}
pos = st.get("position") or {}
snap = db.q1("SELECT equity, cash FROM account_snapshots ORDER BY ts DESC LIMIT 1")
equity = float(snap["equity"]) if snap else 100_000.0
btc = [pr.get_deployment(r["id"]) for r in db.q("SELECT id FROM paper_deployments WHERE broker='alpaca_paper' AND is_test=0 "
                                               "AND status IN ('running','paused_entries') ORDER BY id")]
assert us_exec.etf_deployment() is None, "这个数据库已经迁移过"
plan_before = evolve.plan_budgets(equity, btc)
share_before = et.capital_share(equity)
orders_before = db.q1("SELECT COUNT(*) AS n FROM etf_orders")["n"]

sent: list = []
et._submit = lambda *a, **k: sent.append((a, k)) or {}           # 迁移和检查期间任何下单都会被记下
rep = us_exec.migrate_etf()
dep = us_exec.etf_deployment()
plan_after = evolve.plan_budgets(equity, btc)
share_after = et.capital_share(equity)

# 迁移后的下一次开盘检查（假券商：持仓 = 电脑上 ETF 的持仓；行情 = 本地真实日线）
bars = {s: pd.read_csv(Path(root) / "data" / "etf_daily" / f"{s}.csv") for s in ("QQQ", "QLD")}
for s, b in bars.items():
    b["date"] = pd.to_datetime(b["date"], utc=True).dt.tz_convert(mk.ET).dt.date
last = max(bars["QQQ"]["date"])
day = pd.Timestamp(check_day).date() if check_day else mk.trading_days_after(last, 1)[0]
px = float(bars["QLD"]["close"].iloc[-1])
qty = float(pos.get("qty") or 0)
et._clock = lambda: {"is_open": True}
et._account = lambda: {"equity": str(equity), "cash": str(equity - qty * px)}
et._position = lambda s: {"qty": qty if s == "QLD" else 0.0, "market_value": qty * px if s == "QLD" else 0.0,
                          "avg_entry_price": float(pos.get("avg_entry_price") or 0), "unrealized_pl": 0}
et.daily_bars = lambda s, days_=420: bars[s][["date", "close"]]
et.update_daily_csv = lambda *a, **k: {}
us_exec.http.paper_orders_unlocked = lambda: True
now = datetime.combine(day, datetime.min.time(), tzinfo=mk.ET).replace(hour=9, minute=40).astimezone(timezone.utc)
us_exec.step(now)
why = et.get_why()
after_state = db.get_setting("etf_trend_state") or {}
out = {
    "migration": rep,
    "btc_budgets": [{"deployment": d["id"], "label": d["config"].get("label"), "before": plan_before[d["id"]]["budget"],
                     "after": plan_after[d["id"]]["budget"], "same": plan_before[d["id"]]["budget"] == plan_after[d["id"]]["budget"]}
                    for d in btc],
    "us_capital_share": {"before": share_before, "after": share_after},
    "equity": equity, "etf_orders_before": orders_before,
    "next_check": {"day": str(day), "now_utc": now.isoformat(), "orders_submitted": len(sent), "why": why,
                   "last_action_date": after_state.get("last_action_date"), "position": after_state.get("position"),
                   "signal": after_state.get("signal")},
    "ledger": us_ledger.position(dep["id"]),
}
Path(out_path).write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
print(json.dumps({"orders": len(sent), "budgets_same": all(x["same"] for x in out["btc_budgets"]),
                  "share": out["us_capital_share"], "why": why and why.get("text"), "checks": rep["checks"]},
                 ensure_ascii=False, indent=1))
