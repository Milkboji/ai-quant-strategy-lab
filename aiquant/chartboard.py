"""K 线买卖点看板的数据（页面：ui/page_chart.py）。

- K 线：本地 BTC 5 分钟主行情缓存聚合到所选周期（与回测、模拟盘同一个 market_data.resample）。
- 真实成交：模拟盘订单台账 paper_orders（BTC 策略、实验位）里已成交的买卖，加上通用台账 us_ledger 里的 BTC/USD
  （「我的交易系统」）。时间取下单时间（市价单即时成交），价格取成交均价。
- 信号：每个运行中的 BTC 策略用它正在运行的版本，在同一段 K 线上算出入场 / 退出规则「刚变成成立」的位置
  （engine.signals，与模拟盘同一套代码）。信号只含规则本身；止损 / 止盈 / 移动止损、交易节奏、额度这些
  会让实际成交和信号不一致，所以两者分开画。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from . import db

log = logging.getLogger("aiquant.chartboard")
TF_CN = {15: "15分钟", 30: "30分钟", 60: "1小时", 240: "4小时", 1440: "日线"}


def candles(tf: int, days: int, now: datetime | None = None, df5: pd.DataFrame | None = None) -> pd.DataFrame:
    from .data import market_data as md
    now = now or datetime.now(timezone.utc)
    df5 = md.load_master() if df5 is None else df5
    if df5.empty:
        return md.resample(df5, tf)
    df5 = df5[df5["ts"] >= pd.Timestamp(now - timedelta(days=days))]
    return md.resample(df5, tf, data_end=now).reset_index(drop=True)


def strategies() -> list[dict]:
    """可以在看板上显示的策略：BTC 模拟盘部署（含实验位、已停止但近期有成交的）和「我的交易系统」。"""
    from .paper import runner as pr
    out = []
    for r in db.q("SELECT id, status, broker, config_json FROM paper_deployments WHERE is_test=0 AND "
                  "(broker='alpaca_paper' OR json_extract(config_json,'$.kind')='my_system') ORDER BY id"):
        cfg = json.loads(r["config_json"] or "{}")
        if r["broker"] == "alpaca_us" and cfg.get("market") != "crypto":
            continue
        label = cfg.get("label") or pr.dep_label(r["id"])
        out.append({"id": r["id"], "label": label, "status": r["status"], "broker": r["broker"],
                    "kind": "my_system" if cfg.get("kind") == "my_system" else
                    ("experiment" if cfg.get("experiment") else "btc"),
                    "active": r["status"] in ("running", "paused_entries")})
    return out


def fills(since: datetime, symbol: str = "BTC/USD", ids: list[int] | None = None) -> pd.DataFrame:
    """真实成交：columns ts, side, price, qty, deployment_id, label, source。"""
    labels = {s["id"]: s["label"] for s in strategies()}
    rows = []
    for o in db.q("SELECT deployment_id, side, filled_qty, filled_price, created_at, signal_ts FROM paper_orders "
                  "WHERE status IN ('filled','reconciled') AND filled_qty>0 AND is_test=0 AND created_at>=? ORDER BY id",
                  (since.isoformat(),)):
        if symbol != "BTC/USD":
            continue
        rows.append({"ts": o["created_at"], "side": o["side"], "price": float(o["filled_price"] or 0),
                     "qty": float(o["filled_qty"]), "deployment_id": o["deployment_id"], "source": "模拟盘"})
    try:
        from .paper import us_ledger
        us_ledger.ensure_tables()
        for o in db.q("SELECT deployment_id, side, qty, price, ts FROM us_ledger WHERE symbol=? AND source<>'fee' "
                      "AND ts>=? ORDER BY id", (symbol, since.isoformat())):
            rows.append({"ts": o["ts"], "side": o["side"], "price": float(o["price"]), "qty": float(o["qty"]),
                         "deployment_id": o["deployment_id"], "source": "我的交易系统"})
    except Exception:  # noqa: BLE001
        log.exception("us ledger fills failed")
    df = pd.DataFrame(rows, columns=["ts", "side", "price", "qty", "deployment_id", "source"])
    if df.empty:
        return df.assign(label=pd.Series(dtype=str))
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
    df = df[df["price"] > 0]
    if ids is not None:
        df = df[df["deployment_id"].isin(ids)]
    df["label"] = df["deployment_id"].map(lambda d: labels.get(d, f"#{d}"))
    return df.sort_values("ts").reset_index(drop=True)


def signals(did: int, df5: pd.DataFrame, now: datetime | None = None, since: datetime | None = None) -> pd.DataFrame:
    """某个 BTC 部署（当前运行版本）的规则信号：columns ts, side（buy/sell）, price, label。
    只标「刚成立」的那根 K 线（前一根不成立），避免连续多根重复标记。"""
    from .engine.signals import build_frame, group_signal
    from .paper import runner as pr
    from .strategy import registry
    from .strategy.schema import validate
    dep = pr.get_deployment(did)
    if not dep or dep["broker"] != "alpaca_paper":
        return pd.DataFrame(columns=["ts", "side", "price", "label"])
    v = registry.get_version(dep["version_id"])
    sch, errs = validate(v["schema"])
    if errs:
        return pd.DataFrame(columns=["ts", "side", "price", "label"])
    tf = int(dep["config"].get("signal_tf") or sch.signal_tf or 15)
    aux = dep["config"].get("aux_tf") or sch.aux_tf
    frame = build_frame(df5, sch, tf, aux, data_end=pd.Timestamp(now or datetime.now(timezone.utc)))
    if frame.empty:
        return pd.DataFrame(columns=["ts", "side", "price", "label"])
    ent = group_signal(frame, sch.entry)
    ex = group_signal(frame, sch.exit)
    ent_on = ent & ~ex & ~np.r_[False, ent[:-1]]
    ex_on = ex & ~np.r_[False, ex[:-1]]
    out = []
    label = dep["config"].get("label") or pr.dep_label(did)
    for side, mask in (("buy", ent_on), ("sell", ex_on)):
        f = frame[mask]
        out += [{"ts": t, "side": side, "price": float(p), "label": label}
                for t, p in zip(f["close_time"], f["close"])]
    df = pd.DataFrame(out, columns=["ts", "side", "price", "label"])
    if since is not None and len(df):
        df = df[df["ts"] >= pd.Timestamp(since)]
    return df.sort_values("ts").reset_index(drop=True)


def board(tf: int, days: int, ids: list[int] | None = None, show_signals: bool = True,
          now: datetime | None = None, df5: pd.DataFrame | None = None) -> dict:
    """看板一次需要的全部数据。信号计算多取 60 天预热，只显示所选区间。"""
    from .data import market_data as md
    now = now or datetime.now(timezone.utc)
    full = md.load_master() if df5 is None else df5
    since = now - timedelta(days=days)
    bars = candles(tf, days, now, full)
    strat = strategies()
    chosen = [s for s in strat if ids is None or s["id"] in ids]
    f = fills(since, ids=[s["id"] for s in chosen])
    sig = []
    errors = []
    if show_signals and not full.empty:
        warm = full[full["ts"] >= pd.Timestamp(since - timedelta(days=60))]
        for s in chosen:
            if s["broker"] != "alpaca_paper" or not s["active"]:
                continue
            try:
                d = signals(s["id"], warm, now, since)
                if len(d):
                    sig.append(d)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{s['label']}：{type(e).__name__}")
    sigdf = pd.concat(sig, ignore_index=True) if sig else pd.DataFrame(columns=["ts", "side", "price", "label"])
    return {"bars": bars, "fills": f, "signals": sigdf, "strategies": strat, "errors": errors,
            "since": since, "tf": tf}
