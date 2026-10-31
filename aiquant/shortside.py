"""做空方向（只在本地永续合约模拟账户里运行；Alpaca 现货加密货币不能做空，不下任何订单）。

规则（对称做空版）：把每个 BTC 策略的规则上下翻转（invert）——大于↔小于、上穿↔下穿、布林上轨↔下轨、
前 N 根最高↔最低、最高价↔最低价、RSI / %B 阈值 v → 100−v、变化率 / MACD 阈值取负；成交量、波动率类条件方向无关，保持不变。
翻转后的「入场规则」成立（且翻转后的退出规则不成立）时开空，翻转后的「退出规则」成立时平空；
止损 / 止盈 / 最长持仓与原策略同样的百分比，方向相反（价格涨到 开空价 × (1 + 止损%) 止损）。
成交价：信号 K 线收盘价；止损 / 止盈按之后 K 线的最高 / 最低价触发（同一根先算止损，保守）。
成本：永续合约手续费（默认每边 0.07%）+ 资金费（空头每 8 小时收 / 付 funding_8h，按持仓时长）。

自动资格（系统决定，不问人）：每个策略版本先在训练期（2022-01-01 ~ 验证集开始，本地 5 分钟历史）跑同一套 simulate()，
净收益 > 0、交易 ≥ 10 笔、收益回撤比 ≥ 0.3 才在模拟账户里启用做空腿。结果存 settings `short_qual::<版本 id>`。
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import db

log = logging.getLogger("aiquant.shortside")
TRAIN_START = "2022-01-01"
MIN_TRADES = 10
MIN_RET_DD = 0.3


SWAP_TYPE = {"BB_UPPER": "BB_LOWER", "BB_LOWER": "BB_UPPER", "DONCHIAN_HIGH": "DONCHIAN_LOW", "DONCHIAN_LOW": "DONCHIAN_HIGH"}
SWAP_OP = {">": "<", "<": ">", ">=": "<=", "<=": ">=", "cross_above": "cross_below", "cross_below": "cross_above"}
NEUTRAL = {"VOL_SMA", "ATR", "BB_WIDTH", "BB_WIDTH_RANK", "BB_SQUEEZE"}
CENTER_100 = {"RSI", "BB_PCTB"}
SIGNED = {"ROC", "MACD", "MACD_SIGNAL"}


def invert(schema: dict) -> dict:
    """做多规则 → 对称的做空规则（见模块说明）。返回新的策略字典（仍是做多语义的 schema，由 simulate 当作开空 / 平空用）。"""
    import copy
    d = copy.deepcopy(schema)
    kinds = {}
    for ind in d.get("indicators", []):
        kinds[ind["id"]] = ind["type"]
        ind["type"] = SWAP_TYPE.get(ind["type"], ind["type"])

    def kind(ref):
        if ref is None:
            return None
        base = ref[4:] if ref.startswith("aux.") else ref
        return kinds.get(base, base)

    def flip_ref(ref):
        if ref in ("high", "low"):
            return "low" if ref == "high" else "high"
        if ref in ("aux.high", "aux.low"):
            return "aux.low" if ref == "aux.high" else "aux.high"
        return ref

    for g in (d.get("entry"), d.get("exit")):
        if not g:
            continue
        for c in g["conditions"]:
            lk = kind(c["left"].get("ref"))
            if lk in NEUTRAL or lk == "volume":
                continue
            c["op"] = SWAP_OP[c["op"]]
            c["left"]["ref"] = flip_ref(c["left"]["ref"])
            r = c["right"]
            if r.get("value") is not None:
                if lk in CENTER_100:
                    r["value"] = 100 - float(r["value"])
                elif lk in SIGNED:
                    r["value"] = -float(r["value"])
            else:
                r["ref"] = flip_ref(r["ref"])
                if r.get("mult") not in (None, 1, 1.0):
                    r["mult"] = round(2 - float(r["mult"]), 6)
    d["name"] = (d.get("name") or "")[:54] + "·做空版"
    d.pop("trailing", None)
    return d


def inverted_schema(schema: dict):
    from .strategy.schema import validate
    sch, errs = validate(invert(schema))
    if errs:
        raise ValueError("；".join(errs))
    return sch


BEAR_SMA_DAYS = 200


def bear_mask(frame: pd.DataFrame, df5: pd.DataFrame) -> np.ndarray:
    """熊市过滤：BTC 已收盘日线 < 200 日均线（只用每根K线收盘时已经走完的日线）。"""
    from .data import market_data as md
    d = md.resample(df5, 1440)
    d = d[d["complete"]].reset_index(drop=True)
    d["sma"] = d["close"].rolling(BEAR_SMA_DAYS, min_periods=BEAR_SMA_DAYS).mean()
    d["bear"] = (d["close"] < d["sma"]).astype(float).where(d["sma"].notna(), 0.0)
    m = pd.merge_asof(frame[["close_time"]].sort_values("close_time"), d[["close_time", "bear"]].rename(
        columns={"close_time": "day_close"}), left_on="close_time", right_on="day_close", direction="backward")
    return m["bear"].fillna(0).values.astype(bool)


def signal_arrays(frame: pd.DataFrame, sch, bear: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """sch 是翻转后的做空版：返回 (开空, 平空)。bear 给出时只在熊市开空。"""
    from .engine.signals import group_signal
    ent, ex = group_signal(frame, sch.entry), group_signal(frame, sch.exit)
    opn = ent & ~ex
    if bear is not None:
        opn = opn & bear
    return opn, ex


def simulate(frame: pd.DataFrame, sch, fee: float = 0.0007, funding_8h: float = 0.0001,
             start: pd.Timestamp | None = None, bear: np.ndarray | None = None) -> dict:
    """做空版回测（sch = 翻转后的策略；每笔同样的名义价值，按收益率复利）。返回 trades / return_pct / max_dd_pct / ret_dd。"""
    opn, cov = signal_arrays(frame, sch, bear)
    sl = (sch.stop_loss_pct or 0) / 100
    tp = (sch.take_profit_pct or 0) / 100
    mh = sch.max_hold_bars
    ct = list(pd.to_datetime(frame["close_time"], utc=True))
    hi, lo, cl = frame["high"].values, frame["low"].values, frame["close"].values
    eq, peak, mdd = 1.0, 1.0, 0.0
    trades = []
    pos = None
    for i in range(len(frame)):
        if start is not None and pd.Timestamp(ct[i]) < start:
            continue
        if pos is not None:
            px, why = None, None
            if sl and hi[i] >= pos["entry"] * (1 + sl):
                px, why = pos["entry"] * (1 + sl), "止损"
            elif tp and lo[i] <= pos["entry"] * (1 - tp):
                px, why = pos["entry"] * (1 - tp), "止盈"
            elif cov[i]:
                px, why = cl[i], "平空规则成立"
            elif mh and i - pos["i"] >= mh:
                px, why = cl[i], "最长持仓"
            if px is not None:
                hours = (pd.Timestamp(ct[i]) - pd.Timestamp(pos["t"])).total_seconds() / 3600
                r = (pos["entry"] - px) / pos["entry"] - 2 * fee + funding_8h * hours / 8
                eq *= 1 + r
                trades.append({"open": str(pos["t"]), "close": str(ct[i]), "entry": pos["entry"], "exit": px,
                               "ret_pct": r * 100, "why": why})
                pos = None
                peak = max(peak, eq)
                mdd = min(mdd, eq / peak - 1)
                continue
        if pos is None and opn[i]:
            pos = {"entry": float(cl[i]), "t": ct[i], "i": i}
    ret = (eq - 1) * 100
    dd = mdd * 100
    return {"trades": len(trades), "return_pct": round(ret, 2), "max_dd_pct": round(dd, 2),
            "ret_dd": round(ret / abs(dd), 3) if dd else (round(ret, 3) if ret > 0 else 0.0),
            "win_rate": round(sum(1 for t in trades if t["ret_pct"] > 0) / len(trades) * 100, 1) if trades else None,
            "detail": trades[-20:]}


def _history() -> pd.DataFrame:
    from . import config
    p = config.DATA_DIR / "history" / "BTCUSD_5m.csv.gz"
    df = pd.read_csv(p, usecols=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def qualify(version: dict, tf: int, aux: int | None, df5: pd.DataFrame | None = None) -> dict:
    from . import research
    from .data import market_data as md
    from .engine.signals import build_frame
    from .strategy.schema import validate
    try:
        sch = inverted_schema(version["schema"])
    except ValueError as e:
        return {"ok": False, "why": f"翻转成做空版失败：{e}", "at": db.now_iso()}
    end = md.to_utc(research.get_split()["val_start"])
    df5 = _history() if df5 is None else df5
    df5 = df5[(df5["ts"] >= pd.Timestamp(TRAIN_START, tz="UTC") - pd.Timedelta(days=60)) & (df5["ts"] < end)]
    frame = build_frame(df5, sch, tf, aux, data_end=end)
    r = simulate(frame, sch, start=pd.Timestamp(TRAIN_START, tz="UTC"), bear=bear_mask(frame, df5))
    ok = r["return_pct"] > 0 and r["trades"] >= MIN_TRADES and r["ret_dd"] >= MIN_RET_DD
    r.update(ok=ok, window=[TRAIN_START, end.isoformat()], at=db.now_iso(),
             why=(f"训练期做空版 {r['return_pct']:+.1f}%，回撤 {r['max_dd_pct']:.1f}%，{r['trades']} 笔"
                  + ("：启用做空腿" if ok else f"：不启用（要求净收益 > 0、≥ {MIN_TRADES} 笔、收益回撤比 ≥ {MIN_RET_DD}）")))
    r.pop("detail", None)
    return r


def status(version_id: int) -> dict | None:
    return db.get_setting(f"short_qual::{version_id}")


_busy = False


def maybe_qualify() -> bool:
    """服务循环调用：运行中的 BTC 策略版本还没有做空资格结论的，后台逐个算（一次只跑一个线程）。"""
    global _busy
    from .paper import runner as pr
    if _busy:
        return False
    todo = []
    for r in db.q("SELECT id FROM paper_deployments WHERE broker='alpaca_paper' AND is_test=0 "
                  "AND status IN ('running','paused_entries')"):
        dep = pr.get_deployment(r["id"])
        if status(dep["version_id"]) is None:
            todo.append(dep)
    if not todo:
        return False
    _busy = True

    def work():
        global _busy
        try:
            from .strategy import registry
            df5 = _history()
            for dep in todo:
                v = registry.get_version(dep["version_id"])
                tf = int(dep["config"].get("signal_tf") or v["schema"].get("signal_tf") or 15)
                aux = dep["config"].get("aux_tf") or v["schema"].get("aux_tf")
                try:
                    res = qualify(v, tf, aux, df5)
                except Exception as e:  # noqa: BLE001
                    log.exception("short qualify failed")
                    res = {"ok": False, "why": f"计算失败：{type(e).__name__}", "at": db.now_iso()}
                db.set_setting(f"short_qual::{v['id']}", res)
                pr.event(dep["id"], "INFO", f"做空方向（本地杠杆模拟）：{res['why']}", False)
        finally:
            _busy = False
    threading.Thread(target=work, daemon=True, name="aiquant-shortside").start()
    return True


def live_signal(dep: dict, df5: pd.DataFrame, now: datetime) -> dict | None:
    """最新一根已完成 K 线上的做空信号：{"bar": iso, "open": bool, "cover": bool, "close": px, "high", "low"}。"""
    from .engine.signals import build_frame
    from .strategy import registry
    from .strategy.schema import validate
    v = registry.get_version(dep["version_id"])
    try:
        sch = inverted_schema(v["schema"])
    except ValueError:
        return None
    tf = int(dep["config"].get("signal_tf") or sch.signal_tf or 15)
    aux = dep["config"].get("aux_tf") or sch.aux_tf
    frame = build_frame(df5, sch, tf, aux, data_end=pd.Timestamp(now))
    if len(frame) < 3:
        return None
    opn, cov = signal_arrays(frame, sch, bear_mask(frame, df5))
    i = len(frame) - 1
    return {"bar": pd.Timestamp(frame["close_time"].iloc[i]).isoformat(), "open": bool(opn[i]),
            "cover": bool(cov[i]), "close": float(frame["close"].iloc[i]), "sl": sch.stop_loss_pct,
            "tp": sch.take_profit_pct, "max_hold": sch.max_hold_bars, "tf": tf}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
