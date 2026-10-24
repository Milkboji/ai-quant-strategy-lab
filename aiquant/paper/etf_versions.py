"""美股 ETF 择时（QQQ 均线 → QLD）纳入策略实验室：规则复述、参数版本、修改 → 预览 → 确认 → 冻结 → 部署确认。

- 规则固定：信号品种收盘站上 N 日均线 ×(1+缓冲) 持有交易品种，跌破 N 日均线 ×(1−缓冲) 卖出持有现金，中间保持原状态；
  可修改的只有两个参数：均线周期 sma（日）和缓冲带 band。
- 版本表 etf_versions：每组参数一个不可修改的版本（相同参数不重复创建）。来源：current（首次登记的运行参数）/
  manual（管理员）/ challenger（自动进化晋级）。
- 部署：管理员确认后写入 etf_trend_config；从下一次每日检查开始生效（美股开盘后按前一日收盘信号决定）。
  部署后按「手动部署后锁定天数」锁定，期间 ETF 挑战者胜出只进入待确认队列。
- 修改和部署只限管理员；每次写入策略审计 strategy_audit。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from .. import db
from . import etf_trend

SMA_RANGE = (20, 400)
BAND_RANGE = (0.0, 0.10)
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS etf_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, version_no INTEGER NOT NULL UNIQUE, sma INTEGER NOT NULL, band REAL NOT NULL,
  params_hash TEXT NOT NULL UNIQUE, source TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL, created_by TEXT,
  deployed_at TEXT);
"""


def ensure_tables() -> None:
    with db.tx() as c:
        c.executescript(SCHEMA_SQL)


def _hash(sma: int, band: float) -> str:
    return hashlib.sha256(json.dumps({"sma": int(sma), "band": round(float(band), 6)}, sort_keys=True).encode()).hexdigest()


def check_params(sma, band) -> tuple[int, float]:
    try:
        sma_i, band_f = int(sma), float(band)
    except (TypeError, ValueError):
        raise ValueError("参数必须是数字")
    if not SMA_RANGE[0] <= sma_i <= SMA_RANGE[1]:
        raise ValueError(f"均线周期需在 {SMA_RANGE[0]}~{SMA_RANGE[1]} 日之间")
    if not BAND_RANGE[0] <= band_f <= BAND_RANGE[1]:
        raise ValueError(f"缓冲带需在 {BAND_RANGE[0]:.0%}~{BAND_RANGE[1]:.0%} 范围内")
    return sma_i, round(band_f, 6)


def ensure_version(sma: int, band: float, source: str, note: str = "", actor: str | None = None) -> dict:
    """冻结一组参数为版本（相同参数返回已有版本）。"""
    ensure_tables()
    sma, band = check_params(sma, band)
    h = _hash(sma, band)
    ex = db.q1("SELECT * FROM etf_versions WHERE params_hash=?", (h,))
    if ex:
        return ex
    nxt = (db.q1("SELECT COALESCE(MAX(version_no),0) AS m FROM etf_versions")["m"] or 0) + 1
    db.ex("INSERT INTO etf_versions(version_no,sma,band,params_hash,source,note,created_at,created_by) VALUES(?,?,?,?,?,?,?,?)",
          (nxt, sma, band, h, source, note[:300], db.now_iso(), actor or "系统"))
    return db.q1("SELECT * FROM etf_versions WHERE params_hash=?", (h,))


def versions() -> list[dict]:
    running()
    return db.q("SELECT * FROM etf_versions ORDER BY version_no DESC")


def running() -> dict:
    """当前运行的参数对应的版本（第一次使用时把运行参数登记为 v1）。"""
    c = etf_trend.get_config()
    return ensure_version(int(c["sma"]), float(c["band"]), "current", "登记时正在运行的参数")


def get(vid: int) -> dict | None:
    ensure_tables()
    return db.q1("SELECT * FROM etf_versions WHERE id=?", (int(vid),))


def label(v: dict | None) -> str:
    return f"ETF 择时 v{v['version_no']}（{v['sma']} 日均线 ±{float(v['band']):.0%}）" if v else "--"


def narrate(sma: int, band: float) -> list[tuple[str, str]]:
    c = etf_trend.get_config()
    sig, trade = c["signal_symbol"], c["trade_symbol"]
    return [
        ("周期", f"用日线判断：每个美股交易日，按 {sig} 前一日的收盘价决定。"),
        ("入场", f"{sig} 收盘价高于 {sma} 日均线的 {1 + band:.0%}（即站上均线 {band:.0%} 以上）时，买入并持有 {trade}（2 倍纳指 ETF）。"),
        ("离场", f"{sig} 收盘价低于 {sma} 日均线的 {1 - band:.0%}（跌破均线 {band:.0%} 以上）时，卖出 {trade}，持有现金。"),
        ("过滤条件", f"收盘价在均线 ±{band:.0%} 的缓冲带里时保持原状态，防止在均线附近来回买卖。"),
        ("止损止盈", "没有固定止损止盈，完全由均线信号决定。杠杆来自 2 倍 ETF 本身，没有保证金借款和强平。"),
        ("仓位", f"占模拟账户权益的 {float(c['budget_pct']):g}%；美股开盘 5 分钟后下市价单，每个交易日最多动作一次。"),
    ]


def preview(sma: int, band: float) -> dict:
    """新参数 vs 当前参数：挑选期（2016-2024）和全部数据上的回测对比。"""
    from .. import challenger
    sma, band = check_params(sma, band)
    df = challenger._etf_data()
    if df is None:
        return {"error": "没有找到美股日线数据（data/etf_daily），无法回测"}
    cur = etf_trend.get_config()
    end = str(df.index[-1].date())
    out = {"periods": []}
    for name, a, b in (("挑选期 2016-2024", "2016-01-01", "2024-12-31"), ("全部数据", str(df.index[0].date()), end)):
        k = challenger.etf_backtest(df, sma, band, a, b)
        c = challenger.etf_backtest(df, int(cur["sma"]), float(cur["band"]), a, b)
        out["periods"].append({"name": name, "new": k, "cur": c})
    return out


def lock_state() -> dict | None:
    lk = db.get_setting("etf_version_lock")
    if not lk or not lk.get("until"):
        return None
    if datetime.fromisoformat(lk["until"]) <= datetime.now(timezone.utc):
        return None
    return lk


def freeze(sma, band, *, confirmed: bool, actor: str | None, role: str | None) -> dict:
    if role != "admin":
        raise PermissionError("只有管理员可以修改美股 ETF 择时参数")
    if not confirmed:
        raise ValueError("请先核对规则复述并勾选「确认无误」")
    sma, band = check_params(sma, band)
    v = ensure_version(sma, band, "manual", "管理员在策略实验室修改", actor)
    from .. import console
    console.audit(actor, role, "保存 ETF 版本", "etf", "美股 ETF 择时", None, None, None, label(v))
    return v


def deploy(vid: int, *, confirmed: bool, actor: str | None, role: str | None) -> dict:
    """部署某个 ETF 版本：写入运行参数（下一次每日检查生效），并锁定 N 天。"""
    from .. import console
    if role != "admin":
        raise PermissionError("只有管理员可以部署美股 ETF 择时版本")
    if not confirmed:
        raise ValueError("请先勾选部署确认")
    v = get(vid)
    if not v:
        raise ValueError("版本不存在")
    old = running()
    if old["id"] == v["id"]:
        return {"result": "无变化", "message": f"{label(v)} 已在运行"}
    db.set_setting("etf_trend_config", dict(db.get_setting("etf_trend_config") or {}, sma=int(v["sma"]), band=float(v["band"])))
    db.ex("UPDATE etf_versions SET deployed_at=? WHERE id=?", (db.now_iso(), v["id"]))
    days = float(console.promotion_config().get("manual_lock_days") or 0)
    lock = None
    if days > 0:
        lock = {"until": (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat(),
                "by": actor or "管理员", "at": db.now_iso(), "days": days}
    db.set_setting("etf_version_lock", lock)
    from . import runner as pr
    pr.event(None, "INFO", f"[ETF趋势] {actor or '管理员'} 部署 {label(v)}（原 {label(old)}），下一次每日检查起生效")
    console.audit(actor, role, "部署此版本", "etf", "美股 ETF 择时", None, None, None, "已部署",
                  f"{label(old)} → {label(v)}" + (f"；锁定 {days:g} 天" if lock else ""))
    return {"result": "已部署", "message": f"已部署 {label(v)}，下一次每日检查起生效" + (f"；已锁定 {days:g} 天" if lock else "")}
