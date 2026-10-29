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
    """新参数 vs 当前参数：只在训练期（美股统一分区）上回测对比。验证期 / 测试期只能通过计次的留出检验查看，
    这里不再提供「全部数据」预览。"""
    from .. import challenger
    sma, band = check_params(sma, band)
    df = challenger._etf_data()
    if df is None:
        return {"error": "没有找到美股日线数据（data/etf_daily），无法回测"}
    cur = etf_trend.get_config()
    a, b = challenger.etf_select()
    out = {"periods": []}
    k = challenger.etf_backtest(df, sma, band, a, b)
    c = challenger.etf_backtest(df, int(cur["sma"]), float(cur["band"]), a, b)
    out["periods"].append({"name": f"训练期 {a} ~ {b}", "new": k, "cur": c})
    return out


def lock_state() -> dict | None:
    """有效的 ETF 版本锁（与 BTC 部署的 version_lock 相同结构）：{"until": iso|None, "kind": manual_deploy|manual, ...}。"""
    lk = db.get_setting("etf_version_lock")
    if not lk:
        return None
    if lk.get("until") and datetime.fromisoformat(lk["until"]) <= datetime.now(timezone.utc):
        return None
    return lk


def _require(role: str | None) -> None:
    from .. import console
    console.require_role(role)


def set_lock(on: bool, *, actor: str | None, role: str | None) -> dict:
    """策略总台「锁定版本」开关（与 BTC 的 console.set_lock 同样的语义：打开 = 不设到期；关闭 = 清除任何锁）。"""
    from .. import console
    from . import runner as pr
    _require(role)
    v = running()
    if on:
        db.set_setting("etf_version_lock", {"until": None, "kind": "manual", "by": actor or "用户", "at": db.now_iso()})
        pr.event(None, "INFO", f"[ETF趋势] 策略总台：{actor or '用户'} 锁定版本，自动进化不会自动替换")
        console.audit(actor, role, "锁定版本", "etf", "美股 ETF 择时", None, None, None, "已锁定", label(v))
        return {"result": "已锁定", "message": "已锁定：挑战者胜出后会进入待确认队列"}
    db.set_setting("etf_version_lock", None)
    pr.event(None, "INFO", f"[ETF趋势] 策略总台：{actor or '用户'} 解除版本锁定")
    console.audit(actor, role, "解除锁定", "etf", "美股 ETF 择时", None, None, None, "已解除", label(v))
    return {"result": "已解除", "message": "已解除锁定：恢复自动晋级规则"}


TRIGGER_CN = {"manual": "手动部署", "approve": "批准晋级", "challenger": "挑战者晋级"}


def activate(sma: int, band: float, *, trigger: str, note: str = "", actor: str | None = None,
             role: str | None = None, lock_days: float | None = None) -> dict:
    """唯一改写美股 ETF 运行参数的入口：手动部署、批准晋级、挑战者自动晋级都走这里。
    登记 / 复用版本 → 写入运行参数（下一次每日检查生效）→ 记录部署时间 → 事件 + 审计 →（手动部署）自动锁定。"""
    from .. import console
    from . import runner as pr
    old = running()
    src = {"manual": "manual", "approve": "challenger", "challenger": "challenger"}.get(trigger, "manual")
    v = ensure_version(int(sma), float(band), src, note or TRIGGER_CN.get(trigger, trigger), actor)
    if old["id"] == v["id"]:
        return {"result": "无变化", "message": f"{label(v)} 已在运行", "version": v}
    db.set_setting("etf_trend_config", dict(db.get_setting("etf_trend_config") or {}, sma=int(v["sma"]), band=float(v["band"])))
    db.ex("UPDATE etf_versions SET deployed_at=? WHERE id=?", (db.now_iso(), v["id"]))
    lock = None
    if lock_days and lock_days > 0:
        lock = {"until": (datetime.now(timezone.utc) + timedelta(days=lock_days)).replace(microsecond=0).isoformat(),
                "kind": "manual_deploy", "by": actor or "管理员", "at": db.now_iso(), "days": lock_days}
        db.set_setting("etf_version_lock", lock)
    who = actor or ("自动进化" if trigger == "challenger" else "用户")
    pr.event(None, "INFO", f"[ETF趋势] {who} {TRIGGER_CN.get(trigger, trigger)}：{label(old)} → {label(v)}，下一次每日检查起生效")
    console.audit(actor or "系统", role or "system", {"manual": "部署此版本", "approve": "批准晋级",
                                                      "challenger": "挑战者晋级"}.get(trigger, trigger),
                  "etf", "美股 ETF 择时", None, None, None, "已部署",
                  f"{label(old)} → {label(v)}" + (f"；锁定 {lock_days:g} 天" if lock else "") + (f"；{note}" if note else ""))
    return {"result": "已部署", "version": v, "lock": lock,
            "message": f"已部署 {label(v)}，下一次每日检查起生效" + (f"；已锁定 {lock_days:g} 天" if lock else "")}


def freeze(sma, band, *, confirmed: bool, actor: str | None, role: str | None) -> dict:
    _require(role)
    if not confirmed:
        raise ValueError("请先核对规则复述并勾选「确认无误」")
    sma, band = check_params(sma, band)
    v = ensure_version(sma, band, "manual", "在策略实验室修改", actor)
    from .. import console
    console.audit(actor, role, "保存 ETF 版本", "etf", "美股 ETF 择时", None, None, None, label(v))
    return v


def deploy(vid: int, *, confirmed: bool, actor: str | None, role: str | None) -> dict:
    """手动部署某个 ETF 版本（管理员 / 交易员）：写入运行参数（下一次每日检查生效），并按设置锁定 N 天。"""
    from .. import console
    _require(role)
    if not confirmed:
        raise ValueError("请先勾选部署确认")
    v = get(vid)
    if not v:
        raise ValueError("版本不存在")
    days = float(console.promotion_config().get("manual_lock_days") or 0)
    res = activate(int(v["sma"]), float(v["band"]), trigger="manual", actor=actor, role=role, lock_days=days)
    res.pop("version", None)
    res.pop("lock", None)
    return res


# ---------------------------------------------------------------------------
# 上线 / 下线（与 BTC 相同：有持仓时先停止新开仓，等按信号离场空仓后再下线）
# ---------------------------------------------------------------------------

def status() -> str:
    c = etf_trend.get_config()
    if not c.get("enabled"):
        return "已下线"
    if c.get("pending_offline"):
        return "等待空仓后下线"
    return "运行中"


def set_online(on: bool, *, actor: str | None, role: str | None) -> dict:
    from .. import console
    from . import runner as pr
    _require(role)
    cfg = dict(db.get_setting("etf_trend_config") or {})
    if on:
        cfg.update(enabled=True, pending_offline=False)
        db.set_setting("etf_trend_config", cfg)
        pr.event(None, "INFO", f"[ETF趋势] 策略总台：{actor or '用户'} 重新上线美股 ETF 择时")
        console.audit(actor, role, "上线", "etf", "美股 ETF 择时", None, None, None, "已上线")
        _rebalance()
        return {"result": "已上线", "message": "已上线：下一次每日检查起按信号交易"}
    st = db.get_setting("etf_trend_state") or {}
    held = float((st.get("position") or {}).get("qty") or 0) > 0
    if held:
        cfg.update(pending_offline=True)
        db.set_setting("etf_trend_config", cfg)
        pr.event(None, "INFO", f"[ETF趋势] 策略总台：{actor or '用户'} 下线，先停止新开仓，按信号卖出空仓后下线")
        console.audit(actor, role, "下线", "etf", "美股 ETF 择时", None, None, None, "等待空仓后下线")
        return {"result": "等待空仓后下线", "message": "已停止新开仓；持仓按信号卖出后自动下线"}
    cfg.update(enabled=False, pending_offline=False)
    db.set_setting("etf_trend_config", cfg)
    pr.event(None, "INFO", f"[ETF趋势] 策略总台：{actor or '用户'} 下线美股 ETF 择时")
    console.audit(actor, role, "下线", "etf", "美股 ETF 择时", None, None, None, "已下线")
    _rebalance()
    return {"result": "已下线", "message": "已下线，资金比例释放给 BTC 策略"}


BUDGET_RANGE = (5.0, 60.0)


def set_budget_pct(pct, *, actor: str | None, role: str | None) -> dict:
    """修改美股 ETF 的资金比例（管理员 / 交易员），写审计并立即重新分配 BTC 额度。下一次买入时生效，不主动调仓。"""
    from .. import console
    from . import runner as pr
    _require(role)
    try:
        v = round(float(pct), 2)
    except (TypeError, ValueError):
        raise ValueError("资金比例必须是数字")
    if not BUDGET_RANGE[0] <= v <= BUDGET_RANGE[1]:
        raise ValueError(f"资金比例需在 {BUDGET_RANGE[0]:g}%~{BUDGET_RANGE[1]:g}% 之间")
    cfg = dict(db.get_setting("etf_trend_config") or {})
    old = float(etf_trend.get_config()["budget_pct"])
    if abs(old - v) < 1e-9:
        return {"result": "无变化", "message": f"资金比例已是 {v:g}%"}
    cfg["budget_pct"] = v
    db.set_setting("etf_trend_config", cfg)
    pr.event(None, "INFO", f"[ETF趋势] {actor or '用户'} 把资金比例从 {old:g}% 改为 {v:g}%（下一次买入生效）")
    console.audit(actor, role, "修改资金比例", "etf", "美股 ETF 择时", None, None, None, "已修改", f"{old:g}% → {v:g}%")
    _rebalance()
    return {"result": "已修改", "message": f"资金比例已改为 {v:g}%，下一次买入时生效；BTC 策略额度已重新分配"}


def set_lend_pct(pct, *, actor: str | None, role: str | None) -> dict:
    """美股空仓时 BTC 最多可临时使用的美股额度比例（0~100%，0 = 不借出）。管理员 / 交易员，写审计。"""
    from .. import console
    _require(role)
    try:
        v = round(float(pct), 2)
    except (TypeError, ValueError):
        raise ValueError("借出比例必须是数字")
    if not 0 <= v <= 100:
        raise ValueError("借出比例需在 0%~100% 之间")
    cfg = dict(db.get_setting("etf_trend_config") or {})
    old = float(etf_trend.get_config().get("lend_pct", 50.0))
    if abs(old - v) < 1e-9:
        return {"result": "无变化", "message": f"借出比例已是 {v:g}%"}
    cfg["lend_pct"] = v
    db.set_setting("etf_trend_config", cfg)
    ls = etf_trend.lend_state()
    if ls.get("state") == "lent":
        db.set_setting("etf_lend", dict(ls, lend_pct=v))
    console.audit(actor, role, "修改借出比例", "etf", "美股 ETF 择时", None, None, None, "已修改", f"{old:g}% → {v:g}%")
    _rebalance()
    return {"result": "已修改", "message": f"美股空仓时，BTC 最多临时使用美股额度的 {v:g}%"}


def finish_pending_offline(pos_qty: float) -> bool:
    """服务循环里调用：等待下线且已空仓 → 正式下线。"""
    cfg = dict(db.get_setting("etf_trend_config") or {})
    if not cfg.get("pending_offline") or pos_qty > 0:
        return False
    cfg.update(enabled=False, pending_offline=False)
    db.set_setting("etf_trend_config", cfg)
    from .. import console
    from . import runner as pr
    pr.event(None, "INFO", "[ETF趋势] 已空仓，按计划下线")
    console.audit("系统", "system", "下线完成", "etf", "美股 ETF 择时", None, None, None, "已下线")
    _rebalance()
    return True


def _rebalance() -> None:
    try:
        from .. import evolve
        evolve.rebalance()
    except Exception:  # noqa: BLE001
        pass
