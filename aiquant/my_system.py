"""「我的交易系统 v1」：版本（冻结、带审计）、行情（日线）、训练期回测、部署到模拟盘。

- 引擎：aiquant/engine/my_system.py（分批仓位，回测和模拟盘共用同一个 run()）。
- 版本：表 system_versions（与 ETF 择时的 etf_versions 同样的做法：参数一组一个版本，哈希去重，冻结后不改）。
  策略总台参数面板改参数 → freeze() 冻结新版本 → deploy() 部署；运行中的部署在空仓时切换（与其他策略同一规则）。
- 部署：paper_deployments，broker='alpaca_us'（通用执行器的台账 us_ledger，按品种分账），config.kind='my_system'，
  config.market='crypto' | 'us_equity'。新部署进入试用期（额度从现金层出）。执行器：aiquant/paper/my_system_exec.py。
- 规则来源与没有纳入的内容：docs/my_system.md。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from . import config, db
from .engine import my_system as ms

log = logging.getLogger("aiquant.my_system")
KIND = "my_system"
NAME = "我的交易系统"
CRYPTO_UNIVERSE = ["BTC/USD", "ETH/USD"]          # 10-30 下午：先只保留 BTC、ETH
CANDIDATES = ["SOL/USD", "XRP/USD", "DOGE/USD"]   # 下载数据后只用训练期研究，收益回撤比不低于 BTC/ETH 才逐个加入（写审计）
HISTORY_START = "2021-01-01"                      # 候选币第一次下载的起点（训练期从 2022-01 起，留一年预热）
US_UNIVERSE = ["QQQ", "SPY", "IWM"]
BENCH = {"crypto": "BTC/USD", "us_equity": "SPY"}
# 研究选定的参数（训练期，见 docs/my_system.md 第三节；只在规则给出的几档里选）
CHOSEN = {"crypto": {"box_days": 30, "half_r": 5.0, "time_stop": 10, "be_r": 0.0, "add_rule": "quiet3"},
          "us_equity": {"box_days": 60, "half_r": 5.0, "time_stop": 20}}
TRAIN = {"crypto": ("2022-01-01", None), "us_equity": ("2017-01-01", None)}   # 结束 = 验证集开始（按数据分区）
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS system_versions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, market TEXT NOT NULL, version_no INTEGER NOT NULL, params_json TEXT NOT NULL,
  params_hash TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL, created_by TEXT,
  UNIQUE(market, params_hash), UNIQUE(market, version_no));
"""


def ensure_tables() -> None:
    with db.tx() as c:
        c.executescript(SCHEMA_SQL)


def market_obj(key: str):
    from . import market as mk
    return mk.CRYPTO if key == "crypto" else mk.US_EQUITY


def universe(key: str) -> list[str]:
    return list(CRYPTO_UNIVERSE if key == "crypto" else US_UNIVERSE)


# ---------------------------------------------------------------------------
# 版本
# ---------------------------------------------------------------------------

def _hash(p: ms.Params) -> str:
    return hashlib.sha256(json.dumps(p.to_dict(), sort_keys=True).encode()).hexdigest()


def validate_params(d: dict) -> ms.Params:
    p = ms.Params.from_dict(dict(ms.Params().to_dict(), **(d or {})))
    for k, (name, unit, (lo, hi)) in ms.PARAM_LABELS.items():
        v = getattr(p, k)
        if not lo <= v <= hi:
            raise ValueError(f"{name}需在 {lo:g}~{hi:g} {unit}之间")
    if p.first_pct + 2 * p.add_pct > 100 + 1e-9:
        raise ValueError("首仓比例 + 2 次加仓比例不能超过额度的 100%")
    return p


def freeze(market_key: str, params: dict, *, actor: str | None, note: str = "") -> dict:
    """冻结一组参数为一个版本（相同参数不重复创建）。写审计。"""
    ensure_tables()
    p = validate_params(params)
    h = _hash(p)
    dup = db.q1("SELECT * FROM system_versions WHERE market=? AND params_hash=?", (market_key, h))
    if dup:
        return _row(dup)
    vno = (db.q1("SELECT COALESCE(MAX(version_no),0) AS m FROM system_versions WHERE market=?", (market_key,))["m"]) + 1
    vid = db.ex("INSERT INTO system_versions(market,version_no,params_json,params_hash,note,created_at,created_by) "
                "VALUES(?,?,?,?,?,?,?)", (market_key, vno, json.dumps(p.to_dict(), ensure_ascii=False), h, note[:300],
                                          db.now_iso(), actor or "系统"))
    from . import console
    console.audit(actor, None, "冻结版本", key_of(market_key), label_of(market_key, vno), None, None, vid, "已冻结",
                  "参数：" + params_text(p))
    return get_version(vid)


def _row(r: dict | None) -> dict | None:
    if not r:
        return None
    return dict(r, params=json.loads(r["params_json"]))


def get_version(vid: int | None) -> dict | None:
    if not vid:
        return None
    ensure_tables()
    return _row(db.q1("SELECT * FROM system_versions WHERE id=?", (vid,)))


def versions(market_key: str) -> list[dict]:
    ensure_tables()
    return [_row(r) for r in db.q("SELECT * FROM system_versions WHERE market=? ORDER BY version_no DESC", (market_key,))]


def key_of(market_key: str) -> str:
    return f"my_system:{market_key}"


def label_of(market_key: str, vno: int | None) -> str:
    return f"{NAME}（{'加密货币' if market_key == 'crypto' else '美股'}）v{vno or '?'}"


def params_text(p: ms.Params) -> str:
    return "；".join(f"{name} {getattr(p, k):g}{unit if unit.startswith('%') else ' ' + unit}"
                     for k, (name, unit, _) in ms.PARAM_LABELS.items())


# ---------------------------------------------------------------------------
# 部署
# ---------------------------------------------------------------------------

def deployments(market_key: str | None = None, statuses=("running", "paused_entries")) -> list[dict]:
    from .paper import runner as pr
    rows = db.q("SELECT id FROM paper_deployments WHERE broker='alpaca_us' AND is_test=0 AND "
                f"json_extract(config_json,'$.kind')=? AND status IN ({','.join('?' * len(statuses))}) ORDER BY id",
                (KIND, *statuses))
    out = [pr.get_deployment(r["id"]) for r in rows]
    return [d for d in out if market_key is None or d["config"].get("market") == market_key]


def deploy(vid: int, *, actor: str | None, role: str | None) -> dict:
    """部署一个版本：该市场还没有运行中的部署 → 新建（进入试用期）；已有 → 空仓时切换（否则等空仓）。写审计。"""
    from . import allocation, console
    console.require_role(role)
    v = get_version(vid)
    if not v:
        raise ValueError("版本不存在")
    mkey = v["market"]
    cur = deployments(mkey)
    name = label_of(mkey, v["version_no"])
    if cur:
        dep = cur[0]
        cfg = dep["config"]
        if dep["version_id"] == vid:
            return {"result": "无变化", "deployment_id": dep["id"], "message": f"{name} 已在运行"}
        from .paper import my_system_exec as mx
        if mx.is_flat(dep["id"]):
            cfg.pop("pending_switch", None)
            old = dep["version_id"]
            cfg["live_since"] = db.now_iso()
            cfg.pop("engine_state", None)
            db.ex("UPDATE paper_deployments SET version_id=?, config_json=?, updated_at=? WHERE id=?",
                  (vid, json.dumps(cfg, ensure_ascii=False), db.now_iso(), dep["id"]))
            console.audit(actor, role, "部署", key_of(mkey), name, dep["id"], old, vid, "已切换", params_text(ms.Params.from_dict(v["params"])))
            return {"result": "已切换", "deployment_id": dep["id"], "message": f"已切换到 {name}"}
        cfg["pending_switch"] = {"version_id": vid, "at": db.now_iso(), "by": actor or ""}
        db.ex("UPDATE paper_deployments SET config_json=?, updated_at=? WHERE id=?",
              (json.dumps(cfg, ensure_ascii=False), db.now_iso(), dep["id"]))
        console.audit(actor, role, "部署", key_of(mkey), name, dep["id"], dep["version_id"], vid, "等待空仓后切换",
                      params_text(ms.Params.from_dict(v["params"])))
        return {"result": "等待空仓后切换", "deployment_id": dep["id"], "message": f"{name}：持仓中，空仓后切换"}
    cfg = {"kind": KIND, "market": mkey, "label": name, "symbols": universe(mkey), "live_since": db.now_iso(),
           "budget_usd": 0.0}
    cfg = allocation.start_trial(cfg, mkey)
    did = db.ex("INSERT INTO paper_deployments(version_id,config_json,broker,status,created_at,updated_at,is_test) "
                "VALUES(?,?,?,?,?,?,0)", (vid, json.dumps(cfg, ensure_ascii=False), "alpaca_us", "running",
                                          db.now_iso(), db.now_iso()))
    console.audit(actor, role, "部署", key_of(mkey), name, did, None, vid, "已启动（试用期）",
                  params_text(ms.Params.from_dict(v["params"])))
    from .paper import runner as pr
    pr.event(did, "INFO", f"部署并启动 {name}（试用期，额度从现金层出）：{'、'.join(cfg['symbols'])}")
    return {"result": "已启动", "deployment_id": did, "message": f"已部署 {name}（试用期）"}


def stop(did: int, *, actor: str | None, role: str | None) -> str:
    """下线：空仓时立即停止；有持仓则等全部卖出后停止（执行器处理）。"""
    from . import console
    from .paper import runner as pr
    console.require_role(role)
    dep = pr.get_deployment(did)
    cfg = dep["config"]
    from .paper import my_system_exec as mx
    if mx.is_flat(did):
        pr.set_status(did, "stopped")
        res = "已下线"
    else:
        cfg["pending_offline"] = True
        db.ex("UPDATE paper_deployments SET config_json=?, updated_at=? WHERE id=?",
              (json.dumps(cfg, ensure_ascii=False), db.now_iso(), did))
        res = "等待空仓后下线"
    console.audit(actor, role, "下线", key_of(cfg.get("market")), cfg.get("label"), did, dep["version_id"], None, res, "")
    return res


# ---------------------------------------------------------------------------
# 行情（日线）
# ---------------------------------------------------------------------------

def _cache_dir() -> Path:
    p = config.DATA_DIR / "my_system"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cache_path(sym: str) -> Path:
    return _cache_dir() / f"{sym.replace('/', '')}_1d.csv"


def _daily_from_5m(df5: pd.DataFrame) -> pd.DataFrame:
    from .data import market_data as md
    if df5 is None or not len(df5):
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    d = md.resample(df5, 1440)
    d = d[d["complete"]].rename(columns={"open_time": "ts"})
    return d[["ts", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


def crypto_daily(sym: str, now: datetime | None = None, fetch: bool = True) -> pd.DataFrame:
    """加密货币 UTC 日线（只含已走完的日线）。本地缓存 data/my_system/{SYM}_1d.csv；第一次从 data/history/{SYM}_5m.csv.gz
    （如有）生成，缺的天数用 Alpaca 5 分钟 K 线补（唯一出口）。"""
    now = now or datetime.now(timezone.utc)
    p = _cache_path(sym)
    d = None
    if p.exists():
        d = pd.read_csv(p)
        d["ts"] = pd.to_datetime(d["ts"], utc=True)
    else:
        h = config.DATA_DIR / "history" / f"{sym.replace('/', '')}_5m.csv.gz"
        if h.exists():
            x = pd.read_csv(h)
            x["ts"] = pd.to_datetime(x["ts"], utc=True)
            d = _daily_from_5m(x)
    today = pd.Timestamp(now).tz_convert("UTC").normalize()
    last = d["ts"].max() if d is not None and len(d) else None
    if fetch and (last is None or last < today - pd.Timedelta(days=1)):
        from .data import alpaca
        start = (last + pd.Timedelta(days=1)) if last is not None else pd.Timestamp(HISTORY_START, tz="UTC")
        x = alpaca.fetch_bars_5m(sym, start.to_pydatetime(), today.to_pydatetime())
        if len(x):
            x["ts"] = pd.to_datetime(x["ts"], utc=True)
            new = _daily_from_5m(x)
            new = new[new["ts"] < today]
            d = pd.concat([d, new]) if d is not None else new
            d = d.drop_duplicates("ts", keep="last").sort_values("ts").reset_index(drop=True)
            tmp = p.with_name(p.name + ".tmp")
            d.to_csv(tmp, index=False)
            tmp.replace(p)
    if d is None:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    return d[d["ts"] < today].reset_index(drop=True)


def us_daily_bars(sym: str, now: datetime | None = None) -> pd.DataFrame:
    """美股日线（复权，只含已收盘的交易日）：ts = 当天开盘（UTC）。"""
    from .data import us_daily
    raw = us_daily.load(sym)
    if raw is None or not len(raw):
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    b = us_daily.to_bars(raw)
    if now is not None:
        b = b[b["close_time"] <= pd.Timestamp(now)]
    return b[["ts", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


def daily(market_key: str, sym: str, now: datetime | None = None, fetch: bool = True) -> pd.DataFrame:
    return crypto_daily(sym, now, fetch) if market_key == "crypto" else us_daily_bars(sym, now)


# ---------------------------------------------------------------------------
# 训练期回测（策略实验室模板用）
# ---------------------------------------------------------------------------

def train_window(market_key: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    from . import research
    from .data import market_data as md
    sp = research.get_split(market_obj(market_key))
    return pd.Timestamp(TRAIN[market_key][0], tz="UTC"), pd.Timestamp(md.to_utc(sp["val_start"]))


def backtest(market_key: str, params: dict | None = None, *, symbols: list[str] | None = None,
             fetch: bool = False) -> dict:
    """训练期回测（不触及验证集 / 测试集，不写数据库）。缺数据的品种跳过并写明。"""
    from . import research, rhythm
    p = validate_params(params or default_params(market_key))
    m = market_obj(market_key)
    s, e = train_window(market_key)
    bars, missing = {}, []
    for sym in symbols or universe(market_key):
        try:
            d = daily(market_key, sym, fetch=fetch)
        except Exception:  # noqa: BLE001
            d = None
        if d is None or len(d) < 300:
            missing.append(sym)
            continue
        bars[sym] = d
    bench = bars.get(BENCH[market_key])
    if bench is None:
        bench = daily(market_key, BENCH[market_key], fetch=fetch)
    if not bars or bench is None or not len(bench):
        raise ValueError("本地没有足够的日线数据：" + "、".join(missing or universe(market_key)))
    base = research.base_cost_model(research.get_research(), m) if market_key == "crypto" else m.cost()
    res = ms.run(bars, bench, p, cost=base, start=s, end=e, market_key=market_key, rhythm=rhythm.current(m), market=m)
    res["missing"] = missing
    res["window"] = (s.isoformat(), e.isoformat())
    res["symbols"] = list(bars)
    return res


def default_params(market_key: str) -> dict:
    return dict(ms.Params().to_dict(), **CHOSEN[market_key])


# ---------------------------------------------------------------------------
# 一次性部署（10-30 研究结论：两个市场的最大回撤都不比现役主力策略差 → 部署，见 docs/my_system.md）
# ---------------------------------------------------------------------------

RESEARCH_SUMMARY = {
    "crypto": {"v1": {"cagr": 5.61, "dd": -5.26, "ret_dd": 1.07}, "main": {"cagr": 8.2, "dd": -43.21, "ret_dd": 0.19}},
    "us_equity": {"v1": {"cagr": 0.89, "dd": -6.09, "ret_dd": 0.15}, "main": {"cagr": 34.88, "dd": -34.21, "ret_dd": 1.02}},
}


def initial_deploy() -> dict:
    """资金分层：加密货币上限 → 56%（腾出 10% 给试用）；冻结加密货币版本并部署（试用期），写审计。只执行一次（upgrades）。
    美股 v1 不部署（10-30 下午：收益回撤比 0.15，远低于 ETF 择时的 1.02）。"""
    from . import allocation
    from .data import http
    if not http.paper_orders_unlocked():          # 只在用户自己的模拟盘上执行（公开版默认下单锁关闭，不改资金分层、不部署）
        return {"skipped": "模拟盘下单锁未开启，不改资金分层、不部署"}
    out = {}
    cfg = allocation.get_config()
    if float(cfg["crypto"]["max"]) != 56.0:
        allocation.set_config(actor="系统（按 10-30 指令）", role="admin", crypto_max=56.0)
        out["crypto_max"] = "→ 56%"
    out["us_equity"] = "不部署：收益回撤比 0.15，远低于 ETF 择时的 1.02"
    if deployments("crypto"):
        out["crypto"] = "已有部署"
        return out
    r = RESEARCH_SUMMARY["crypto"]
    v = freeze("crypto", default_params("crypto"), actor="系统（按 10-30 指令）",
               note=f"训练期研究选定：最大回撤 {r['v1']['dd']}% vs 主力 {r['main']['dd']}%；保本止损关闭")
    out["crypto"] = deploy(v["id"], actor="系统（按 10-30 指令）", role="admin")["message"]
    return out


def adjust_1030() -> dict:
    """10-30 下午的调整（幂等）：下线美股 v1（有持仓等离场后停，额度退回现金层）；加密货币 v1 只保留 BTC、ETH；
    关闭保本止损，冻结新版本并部署（持仓中则空仓后切换）。"""
    from .data import http
    if not http.paper_orders_unlocked():
        return {"skipped": "模拟盘下单锁未开启"}
    out = {}
    actor = "系统（按 10-30 下午指令）"
    for d in deployments("us_equity"):
        out["us_equity"] = stop(d["id"], actor=actor, role="admin") + "（收益回撤比 0.15，远低于 ETF 择时的 1.02；额度退回现金层）"
    for d in deployments("crypto"):
        cfg = d["config"]
        keep = [x for x in cfg.get("symbols") or [] if x in CRYPTO_UNIVERSE] or list(CRYPTO_UNIVERSE)
        if keep != cfg.get("symbols"):
            removed = [x for x in cfg.get("symbols") or [] if x not in keep]
            from .paper import my_system_exec as mx
            held = [x for x in removed
                    if mx.us_ledger.position(d["id"], x)["qty"] > mx.us_ledger.QTY_EPS]
            cfg["symbols"] = keep + held          # 已有持仓的先保留（不再加仓），离场后由执行器移除
            cfg["drop_symbols"] = held
            cfg["rebase"] = True                  # 标的变了：下一次检查按新组合重放引擎、重新对齐，不补单
            db.ex("UPDATE paper_deployments SET config_json=?, updated_at=? WHERE id=?",
                  (json.dumps(cfg, ensure_ascii=False), db.now_iso(), d["id"]))
            from . import console
            console.audit(actor, "admin", "调整标的", key_of("crypto"), cfg.get("label"), d["id"], d["version_id"],
                          d["version_id"], "只保留 BTC、ETH", "去掉：" + "、".join(removed))
            out["symbols"] = "只保留 BTC、ETH（去掉 " + "、".join(removed) + "）"
        cur = get_version(d["version_id"])
        want = dict(cur["params"], be_r=0.0, add_rule=CHOSEN["crypto"]["add_rule"])
        if cur["params"] != validate_params(want).to_dict():
            v = freeze("crypto", want, actor=actor, note="保本止损关闭（训练期：1 倍 0.77、2 倍 0.77、关闭 1.07）")
            out["crypto"] = deploy(v["id"], actor=actor, role="admin")["message"]
    return out


# ---------------------------------------------------------------------------
# 候选币：下载数据 → 训练期研究 → 收益回撤比不低于 BTC/ETH 才加入
# ---------------------------------------------------------------------------

def candidate_status() -> dict:
    return {c: db.get_setting(f"my_system_candidate::{c}") for c in CANDIDATES}


def study_candidate(sym: str, *, fetch: bool = True) -> dict:
    """用加密货币部署当前版本的参数，在训练期分别回测「只有该币」和「BTC + ETH」；该币的收益回撤比不低于 BTC/ETH 才通过。
    交易少于 5 轮、或训练期数据不足两年，不通过。"""
    from . import rhythm
    from . import research
    deps = deployments("crypto")
    v = get_version(deps[0]["version_id"]) if deps else None
    p = validate_params(v["params"] if v else default_params("crypto"))
    m = market_obj("crypto")
    s, e = train_window("crypto")
    d = crypto_daily(sym, fetch=fetch)
    bench = crypto_daily("BTC/USD", fetch=fetch)
    base_bars = {x: crypto_daily(x, fetch=fetch) for x in ("BTC/USD", "ETH/USD")}
    cost = research.base_cost_model(research.get_research(), m)
    rh = rhythm.current(m)
    have = d[(d["ts"] >= s) & (d["ts"] < e)] if len(d) else d
    if len(have) < 730:
        return {"symbol": sym, "pass": False, "why": f"训练期日线只有 {len(have)} 根（需要至少两年）"}
    c = ms.run({sym: d}, bench, p, cost=cost, start=s, end=e, market_key="crypto", rhythm=rh, market=m)["metrics"]
    b = ms.run(base_bars, bench, p, cost=cost, start=s, end=e, market_key="crypto", rhythm=rh, market=m)["metrics"]
    ok = c["entries"] >= 5 and c.get("ret_dd") is not None and b.get("ret_dd") is not None and c["ret_dd"] >= b["ret_dd"]
    why = (f"{sym} 收益回撤比 {c.get('ret_dd') or 0:.2f}（年化 {c['cagr_pct']:.1f}%，回撤 {c['max_drawdown_pct']:.1f}%，"
           f"{c['entries']} 轮）vs BTC/ETH {b.get('ret_dd') or 0:.2f}")
    return {"symbol": sym, "pass": bool(ok), "why": why, "window": (s.isoformat(), e.isoformat()),
            "candidate": {k: c.get(k) for k in ("cagr_pct", "max_drawdown_pct", "ret_dd", "entries")},
            "base": {k: b.get(k) for k in ("cagr_pct", "max_drawdown_pct", "ret_dd", "entries")}}


def add_symbol(sym: str, why: str, *, actor: str = "系统（候选币研究）") -> bool:
    """把通过研究的候选币加入加密货币部署（执行器从下一次检查起跟随；加入之前引擎里的历史信号不补）。写审计。"""
    from . import console
    deps = deployments("crypto")
    if not deps:
        return False
    d = deps[0]
    cfg = d["config"]
    if sym in (cfg.get("symbols") or []):
        return False
    cfg["symbols"] = list(cfg.get("symbols") or []) + [sym]
    cfg["rebase"] = True                          # 下一次检查重新对齐引擎持仓：新币加入前的历史信号不补
    db.ex("UPDATE paper_deployments SET config_json=?, updated_at=? WHERE id=?",
          (json.dumps(cfg, ensure_ascii=False), db.now_iso(), d["id"]))
    console.audit(actor, "admin", "加入标的", key_of("crypto"), cfg.get("label"), d["id"], d["version_id"], d["version_id"],
                  f"加入 {sym}", why)
    from .paper import runner as pr
    pr.event(d["id"], "INFO", f"[我的交易系统] 加入标的 {sym}：{why}")
    return True


_cand_running = False


def maybe_candidates(now: datetime | None = None) -> bool:
    """后台每天最多一次：还没研究过（或上次失败）的候选币，下载数据、做训练期研究，通过的逐个加入。单独线程，不拖慢交易循环。"""
    global _cand_running
    from .data import http
    now = now or datetime.now(timezone.utc)
    if _cand_running or not deployments("crypto") or not http.paper_orders_unlocked() or not config.secret("ALPACA_API_KEY_ID"):
        return False
    todo = []
    for c in CANDIDATES:
        st = db.get_setting(f"my_system_candidate::{c}") or {}
        if st.get("done"):
            continue
        if st.get("at") and st["at"][:10] == now.strftime("%Y-%m-%d"):
            continue
        todo.append(c)
    if not todo:
        return False
    _cand_running = True

    def work():
        global _cand_running
        try:
            for c in todo:
                try:
                    r = study_candidate(c)
                    added = add_symbol(c, r["why"]) if r["pass"] else False
                    db.set_setting(f"my_system_candidate::{c}", dict(r, done=True, added=added, at=db.now_iso()))
                except Exception as e:  # noqa: BLE001  下载失败等：明天再试
                    log.warning("candidate %s failed", c, exc_info=True)
                    db.set_setting(f"my_system_candidate::{c}", {"done": False, "at": db.now_iso(),
                                                                  "why": f"{type(e).__name__}: {str(e)[:160]}"})
        finally:
            _cand_running = False
    import threading
    threading.Thread(target=work, daemon=True, name="aiquant-ms-candidates").start()
    return True
