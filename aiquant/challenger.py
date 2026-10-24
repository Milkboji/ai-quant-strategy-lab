"""冠军–挑战者：进化找到的更好候选不再丢弃，而是在实时行情上与现役版本并行“模拟运行”，用新数据决定是否上线。

为什么需要它：历史留出数据（验证集）次数有限、前向新数据又还很短时，进化只能“等待新数据”，下一轮又重新搜索，
好的候选白白丢掉。挑战者机制把候选登记下来，从登记时刻起，用同一个确定性引擎在之后到来的真实行情上同时计算
挑战者与现役版本的表现（这段数据在挑选时还不存在，是真正的样本外），每小时更新一次：
- 运行满最短天数、挑战者有成交、收益不差于现役、回撤不明显更深 → 自动晋级：冻结新版本并部署到模拟盘（空仓时切换）
- 运行满最短天数但明显更差 → 淘汰
- 现役版本已被替换 → 作废
覆盖：BTC 模拟盘全部策略 + 美股 ETF 择时（参数：均线长度、缓冲带）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, db, research
from .data import market_data as md
from .strategy import registry
from .strategy.schema import schema_hash

log = logging.getLogger("aiquant.challenger")

TOL_RET = 0.5          # 挑战者前向收益可比现役低的百分点（噪声容忍）
EARLY_TRADES = 3       # 前向提前决策需要的最少成交笔数
EARLY_LEAD = 2.0       # 提前晋级：前向收益领先现役的百分点
EARLY_LAG = 3.0        # 提前淘汰：前向收益落后现役的百分点
TOL_DD = 3.0           # 前向最大回撤可更深的百分点
ETF_MIN_DAYS = 60      # ETF 挑战者最短前向交易日
ETF_SELECT = ("2016-01-01", "2024-12-31")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS evolve_challengers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, deployment_id INTEGER, label TEXT,
  champion_version_id INTEGER, champion_params TEXT, candidate_json TEXT, change TEXT, train_json TEXT,
  registered_at TEXT, status TEXT, last_eval_json TEXT, decided_at TEXT, reason TEXT, new_version_id INTEGER,
  is_test INTEGER DEFAULT 0);
"""


def ensure_tables() -> None:
    with db.tx() as c:
        c.executescript(SCHEMA_SQL)


def _event(did, level, msg, is_test=False):
    from .paper import runner as pr
    pr.event(did, level, msg[:480], is_test)


def running(kind: str | None = None) -> list[dict]:
    ensure_tables()
    sql = "SELECT * FROM evolve_challengers WHERE status='running'"
    args: list = []
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    return db.q(sql + " ORDER BY id", args)


def all_rows(limit: int = 40) -> list[dict]:
    ensure_tables()
    rows = db.q("SELECT * FROM evolve_challengers ORDER BY id DESC LIMIT ?", (limit,))
    for r in rows:
        for k in ("candidate_json", "train_json", "last_eval_json", "champion_params"):
            r[k[:-5] if k.endswith("_json") else k] = json.loads(r[k]) if r.get(k) else None
    return rows


# ---------------------------------------------------------------------------
# BTC 策略挑战者
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 快速检验：用挑选时没用过的数据立即判断，不必干等新行情
#   1) 更早历史：训练窗口之前的 BTC 行情（2021 年起），按年切成多段
#   2) 跨币种：同一套规则在 ETH/USD 上（ETH 从未参与任何挑选）
# 结论：通过 → 立即晋级；明显更差 → 立即淘汰；说不清 → 交给前向模拟运行
# ---------------------------------------------------------------------------

_hist_cache: dict = {}


def _history(sym: str) -> pd.DataFrame | None:
    p = config.DATA_DIR / "history" / f"{sym.replace('/', '')}_5m.csv.gz"
    if not p.exists():
        return None
    sig = (p.stat().st_mtime_ns, p.stat().st_size)
    hit = _hist_cache.get(str(p))
    if hit and hit[0] == sig:
        return hit[1]
    df = pd.read_csv(p)
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    _hist_cache[str(p)] = (sig, df)
    return df


def _folds(df: pd.DataFrame, end: datetime, warmup_days: int = 60, years: int = 6) -> list[tuple[datetime, datetime]]:
    first = df["ts"].iloc[0].to_pydatetime() + timedelta(days=warmup_days)
    out = []
    e = end
    while len(out) < years:
        s = e - timedelta(days=365)
        if s < first:
            if (e - first).days >= 120:
                out.append((first, e))
            break
        out.append((s, e))
        e = s
    return list(reversed(out))


def fast_check_btc(champ: dict, cand: dict, r: dict) -> dict:
    from . import evolve
    rr = dict(r)
    res = {"btc": [], "eth": []}
    train_start = evolve._windows()["train"][0]
    jobs = []
    btc = _history("BTC/USD")
    if btc is not None and len(btc):
        jobs += [("btc", btc, f) for f in _folds(btc[btc["ts"] < pd.Timestamp(train_start)], train_start)]
    eth = _history("ETH/USD")
    if eth is not None and len(eth):
        jobs += [("eth", eth, f) for f in _folds(eth, eth["ts"].iloc[-1].to_pydatetime().replace(minute=0))]
    for kind, df, (a, b) in jobs:
        try:
            k = evolve.evaluate(df, cand, a, b, rr)["metrics"]
            c = evolve.evaluate(df, champ, a, b, rr)["metrics"]
        except Exception as e:  # noqa: BLE001
            log.info("fast check fold failed: %s", e)
            continue
        res[kind].append({"start": a.strftime("%Y-%m-%d"), "end": b.strftime("%Y-%m-%d"),
                          "cand": round(k["net_return_pct"], 2), "champ": round(c["net_return_pct"], 2),
                          "cand_dd": round(k["max_drawdown_pct"], 2), "champ_dd": round(c["max_drawdown_pct"], 2),
                          "cand_trades": k["trades"], "champ_trades": c["trades"]})
    return _fast_verdict(res)


def _fast_verdict(res: dict) -> dict:
    b, e = res["btc"], res["eth"]
    if len(b) < 2:
        return dict(res, verdict="inconclusive", reason="更早的 BTC 历史不足（需先运行 fetch_history.bat 下载 2021 年起数据）")
    def agg(rows):
        return (sum(x["cand"] for x in rows), sum(x["champ"] for x in rows),
                sum(1 for x in rows if x["cand"] >= x["champ"] - 0.25), sum(x["cand_trades"] for x in rows),
                min(x["cand_dd"] for x in rows), min(x["champ_dd"] for x in rows))
    bc, bp, bw, bt_, bdd, pdd = agg(b)
    txt = f"更早历史 {len(b)} 段：候选合计 {bc:+.1f}% vs 现役 {bp:+.1f}%，{bw}/{len(b)} 段不差"
    eth_ok, eth_bad = True, False
    if e:
        ec, ep, ew, _, _, _ = agg(e)
        txt += f"；ETH {len(e)} 段：候选合计 {ec:+.1f}% vs 现役 {ep:+.1f}%，{ew}/{len(e)} 段不差"
        eth_ok = ec >= ep - 1.0 and ew * 2 >= len(e)
        eth_bad = ec < ep - 3.0 and ew * 2 < len(e)
    else:
        txt += "；缺少 ETH 历史"
    btc_ok = bc >= bp and bw * 2 >= len(b) and bt_ >= 3 and bdd >= pdd - TOL_DD
    btc_bad = bc < bp - 2.0 and bw * 2 < len(b)
    if btc_ok and eth_ok and e:
        v = "pass"
    elif btc_bad or (eth_bad and not btc_ok):
        v = "fail"
    else:
        v = "inconclusive"
    return dict(res, verdict=v, reason=txt)


def register_btc(dep: dict, champion: dict, cand_schema: dict, change: str, train_brief: dict, is_test: bool) -> int:
    """登记（或替换为训练分更高的）挑战者。同一部署同时只保留一个运行中的挑战者。"""
    ensure_tables()
    ex = db.q1("SELECT * FROM evolve_challengers WHERE kind='btc' AND deployment_id=? AND status='running'", (dep["id"],))
    if ex:
        old = json.loads(ex["candidate_json"])
        if schema_hash(old) == schema_hash(cand_schema):
            return ex["id"]
        old_score = (json.loads(ex["train_json"]) or {}).get("score") or -1e9
        if (train_brief.get("score") or -1e9) <= old_score:
            return ex["id"]
        db.ex("UPDATE evolve_challengers SET status='replaced', decided_at=?, reason=? WHERE id=?",
              (db.now_iso(), "被训练分更高的新挑战者替换", ex["id"]))
    label = json.loads(dep["config_json"]).get("label") or f"#{dep['id']}"
    cid = db.ex("INSERT INTO evolve_challengers(kind,deployment_id,label,champion_version_id,candidate_json,change,"
                "train_json,registered_at,status,is_test) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("btc", dep["id"], label, champion["id"], json.dumps(cand_schema, ensure_ascii=False), change,
                 json.dumps(train_brief, ensure_ascii=False), db.now_iso(), "running", 1 if is_test else 0))
    _event(dep["id"], "INFO", f"自动进化：挑战者 #{cid} 开始在实时行情上模拟运行（{change}）。"
                              f"训练集稳健分 {train_brief.get('score')}，收益 {train_brief.get('return_pct')}%", is_test)
    return cid


def retire_for_deployment(did: int, reason: str) -> None:
    ensure_tables()
    db.ex("UPDATE evolve_challengers SET status='expired', decided_at=?, reason=? WHERE kind='btc' "
          "AND deployment_id=? AND status='running'", (db.now_iso(), reason, did))


def _master(source: str) -> pd.DataFrame:
    venue = "TEST_SYNTHETIC" if source == "test" else None
    return md.load_master(venue) if venue else md.load_master()


def evaluate_btc(now: datetime | None = None, source: str | None = None) -> list[str]:
    from . import evolve
    from .paper import runner as pr
    out = []
    rows = running("btc")
    if not rows:
        return out
    now = (now or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    now = now - timedelta(minutes=now.minute % 5)
    r = research.get_research()
    cache: dict = {}
    for ch in rows:
        src = "test" if ch["is_test"] else (source or r["source"])
        rr = dict(r, source=src)
        dep = pr.get_deployment(ch["deployment_id"])
        if not dep or dep["status"] not in ("running", "paused_entries"):
            _close(ch, "expired", "部署已停止")
            continue
        if dep["version_id"] != ch["champion_version_id"]:
            _close(ch, "expired", "现役版本已被其他进化结果替换")
            continue
        champ = registry.get_version(dep["version_id"])
        cand = json.loads(ch["candidate_json"])
        csch = dict(champ["schema"])
        csch["signal_tf"] = int(dep["config"].get("signal_tf") or csch.get("signal_tf") or 15)
        start = md.to_utc(ch["registered_at"])
        tj = json.loads(ch["train_json"] or "{}")
        if "fast" not in tj and not ch["is_test"]:
            fc = fast_check_btc(csch, cand, rr)
            if len(fc["btc"]) >= 2 and fc["eth"]:   # 历史数据还没下载好时不记录，下次再做
                tj["fast"] = {"verdict": fc["verdict"], "reason": fc["reason"], "btc": fc["btc"], "eth": fc["eth"]}
                db.ex("UPDATE evolve_challengers SET train_json=? WHERE id=?", (json.dumps(tj, ensure_ascii=False), ch["id"]))
            if fc["verdict"] == "pass":
                out.append(_promote_btc(ch, dep, champ, cand, None, rr, "快速检验通过：" + fc["reason"]))
                continue
            if fc["verdict"] == "fail":
                _close(ch, "rejected", "快速检验明显更差，淘汰：" + fc["reason"])
                _event(dep["id"], "INFO", f"自动进化：挑战者 #{ch['id']} 快速检验淘汰 — {fc['reason']}", bool(ch["is_test"]))
                out.append(f"#{ch['id']} 快速淘汰")
                continue
        if src not in cache:
            cache[src] = _master(src)
        df = cache[src]
        days = (now - start).total_seconds() / 86400
        need = max(evolve.FORWARD_MIN_DAYS.get(int(cand.get("signal_tf") or 15), 14),
                   evolve.FORWARD_MIN_DAYS.get(csch["signal_tf"], 14))
        try:
            if now - start < timedelta(hours=2):
                raise ValueError("前向数据不足 2 小时")
            k = evolve.evaluate(df, cand, start, now, rr)
            c = evolve.evaluate(df, csch, start, now, rr)
        except Exception as e:  # noqa: BLE001
            db.ex("UPDATE evolve_challengers SET last_eval_json=? WHERE id=?",
                  (json.dumps({"at": db.now_iso(), "days": round(days, 2), "need_days": need, "note": str(e)[:120]},
                              ensure_ascii=False), ch["id"]))
            continue
        km, cm = k["metrics"], c["metrics"]
        ev = {"at": db.now_iso(), "days": round(days, 2), "need_days": need,
              "challenger": {"return_pct": round(km["net_return_pct"], 2), "max_dd_pct": round(km["max_drawdown_pct"], 2),
                             "trades": km["trades"]},
              "champion": {"return_pct": round(cm["net_return_pct"], 2), "max_dd_pct": round(cm["max_drawdown_pct"], 2),
                           "trades": cm["trades"]}}
        db.ex("UPDATE evolve_challengers SET last_eval_json=? WHERE id=?", (json.dumps(ev, ensure_ascii=False), ch["id"]))
        lead = km["net_return_pct"] - cm["net_return_pct"]
        if days < need:
            # 证据已经很清楚时提前决定：运行满 1/3 时间、挑战者成交 ≥3 笔
            if days >= need / 3 and km["trades"] >= EARLY_TRADES:
                why = (f"提前决策（前向 {days:.1f} 天，{km['trades']} 笔）：挑战者 {km['net_return_pct']:+.2f}% / 回撤 {km['max_drawdown_pct']:.1f}%，"
                       f"现役 {cm['net_return_pct']:+.2f}% / 回撤 {cm['max_drawdown_pct']:.1f}%")
                if lead >= EARLY_LEAD and km["max_drawdown_pct"] >= cm["max_drawdown_pct"] - TOL_DD:
                    out.append(_promote_btc(ch, dep, champ, cand, k, rr, why))
                elif lead <= -EARLY_LAG:
                    _close(ch, "rejected", "前向明显落后，提前淘汰：" + why)
                    _event(dep["id"], "INFO", f"自动进化：挑战者 #{ch['id']} 提前淘汰 — {why}", bool(ch["is_test"]))
                    out.append(f"#{ch['id']} 提前淘汰")
            continue
        better = km["net_return_pct"] >= cm["net_return_pct"] - TOL_RET and km["max_drawdown_pct"] >= cm["max_drawdown_pct"] - TOL_DD
        why = (f"前向 {days:.0f} 天：挑战者 {km['net_return_pct']:+.2f}% / 回撤 {km['max_drawdown_pct']:.1f}% / {km['trades']} 笔，"
               f"现役 {cm['net_return_pct']:+.2f}% / 回撤 {cm['max_drawdown_pct']:.1f}% / {cm['trades']} 笔")
        if better and km["trades"] >= 1:
            out.append(_promote_btc(ch, dep, champ, cand, k, rr, why))
        elif not better:
            _close(ch, "rejected", "前向表现不如现役，淘汰：" + why)
            _event(dep["id"], "INFO", f"自动进化：挑战者 #{ch['id']} 淘汰 — {why}", bool(ch["is_test"]))
            out.append(f"#{ch['id']} 淘汰")
        elif days >= need * 3:
            _close(ch, "expired", "运行很久仍没有成交，无法证明更好：" + why)
            out.append(f"#{ch['id']} 过期")
    return out


def _close(ch: dict, status: str, reason: str, new_vid: int | None = None) -> None:
    db.ex("UPDATE evolve_challengers SET status=?, decided_at=?, reason=?, new_version_id=? WHERE id=?",
          (status, db.now_iso(), reason[:480], new_vid, ch["id"]))


def _promote_btc(ch, dep, champ, cand, fwd_eval, rr, why) -> str:
    from . import evolve
    from .engine import backtest as bt
    nv = registry.freeze(champ["slot_no"], dict(cand, name=champ["schema"]["name"]), nl_text=champ["nl_text"],
                         source="auto_evolve", change_summary=f"[挑战者#{ch['id']}晋级] {ch['change']}"[:500],
                         parent_id=champ["id"], set_current=champ["slot_no"] is not None)
    try:   # 部署门禁需要训练集回测：用与进化相同的训练窗口补算并保存
        tr = evolve._windows()["train"]
        ds = md.get_dataset(*tr, source=rr["source"])
        te = evolve.evaluate(md.load_dataset(ds), nv["schema"], tr[0], tr[1], rr)
        research.store_backtest(job_id=None, version=nv, slot_no=champ["slot_no"], ds=ds, start=tr[0], end=tr[1],
                                tf=te["tf"], aux=te["aux"], split="训练集", cfg=te["cfg"], res=te["res"],
                                is_test=int(ch["is_test"]))
    except Exception:  # noqa: BLE001
        log.exception("store training backtest for challenger failed")
    if champ["slot_no"] is None:
        db.set_setting(f"preset_current::{champ['name']}", nv["id"])
    _close(ch, "promoted", "前向表现不差于现役，晋级上线：" + why, nv["id"])
    tf = int(cand.get("signal_tf") or 15)
    evolve.schedule_switch(dep["id"], nv["id"], tf, cand.get("aux_tf"), f"挑战者#{ch['id']}晋级：{why}",
                           trigger="challenger")
    return f"#{ch['id']} 晋级 → {registry.label(nv)}"


# ---------------------------------------------------------------------------
# 美股 ETF 择时挑战者（参数：均线长度、缓冲带）
# ---------------------------------------------------------------------------

def _etf_data() -> pd.DataFrame | None:
    d = config.DATA_DIR / "etf_daily"
    from .paper import etf_trend as et
    cfg = et.get_config()
    a, b = d / f"{cfg['signal_symbol']}.csv", d / f"{cfg['trade_symbol']}.csv"
    if not (a.exists() and b.exists()):
        return None
    def load(p: Path) -> pd.DataFrame:
        x = pd.read_csv(p)
        x["date"] = pd.to_datetime(x["date"], utc=True).dt.tz_convert("America/New_York").dt.normalize().dt.tz_localize(None)
        return x.drop_duplicates("date").set_index("date")
    s, t = load(a), load(b)
    return pd.DataFrame({"sig_close": s["close"], "open": t["open"], "close": t["close"]}).dropna()


def etf_backtest(df: pd.DataFrame, sma: int, band: float, start: str, end: str, cost: float = 0.0005) -> dict:
    """收盘出信号，次日开盘调仓，持有期收益=开盘到下一日开盘；空仓收益为 0（现金）。"""
    from .paper.etf_trend import regime_series
    st = regime_series(df["sig_close"], int(sma), float(band))
    w = st.shift(1).fillna(0.0)
    r = df["open"].pct_change().shift(-1).fillna(0.0)
    pr = w * r - (w.diff().abs().fillna(0.0)) * cost
    x = pr[start:end]
    if len(x) < 20:
        return {"cagr": 0.0, "dd": 0.0, "ret": 0.0, "calmar": 0.0, "days": len(x), "switches": 0}
    eq = (1 + x).cumprod()
    yrs = len(x) / 252
    cagr = (eq.iloc[-1] ** (1 / yrs) - 1) * 100
    dd = float((eq / eq.cummax() - 1).min() * 100)
    return {"cagr": round(cagr, 2), "dd": round(dd, 2), "ret": round((eq.iloc[-1] - 1) * 100, 2),
            "calmar": round(cagr / max(abs(dd), 1.0), 3), "days": int(len(x)),
            "switches": int((w[start:end].diff().abs() > 0).sum())}


def etf_round(run_id: int | None = None) -> dict:
    """在挑选期（2016-2024）比较当前参数与相邻参数；明显更好且没有运行中的 ETF 挑战者时登记挑战者。"""
    from .paper import etf_trend as et
    ensure_tables()
    df = _etf_data()
    if df is None:
        return {"decision": "跳过", "reason": "缺少 ETF 日线数据（运行 fetch_etf.bat）"}
    cfg = et.get_config()
    cur = {"sma": int(cfg["sma"]), "band": float(cfg["band"])}
    base = etf_backtest(df, cur["sma"], cur["band"], *ETF_SELECT)
    best, best_m = None, base
    for n in sorted({max(60, cur["sma"] + d) for d in (-50, -30, -20, 0, 20, 30, 50)}):
        for b in (0.0, 0.01, 0.02, 0.03):
            if n == cur["sma"] and abs(b - cur["band"]) < 1e-9:
                continue
            m = etf_backtest(df, n, b, *ETF_SELECT)
            if m["calmar"] > best_m["calmar"]:
                best, best_m = {"sma": n, "band": b}, m
    rec = {"current": dict(cur, **base), "best": (dict(best, **best_m) if best else None)}
    if not best or best_m["calmar"] < base["calmar"] * 1.10:
        rec.update(decision="保留", reason=f"相邻参数没有明显更好（当前 Calmar {base['calmar']}）")
        return rec
    if running("etf"):
        rec.update(decision="保留", reason="已有 ETF 挑战者在模拟运行，等待其结果")
        return rec
    change = f"均线 {cur['sma']}→{best['sma']} 日，缓冲 {cur['band']:.0%}→{best['band']:.0%}"
    cid = db.ex("INSERT INTO evolve_challengers(kind,label,champion_params,candidate_json,change,train_json,registered_at,"
                "status) VALUES(?,?,?,?,?,?,?,?)",
                ("etf", "美股 ETF 择时", json.dumps(cur), json.dumps(best), change, json.dumps(best_m),
                 db.now_iso(), "running"))
    _event(None, "INFO", f"[ETF趋势] 自动进化：挑战者 #{cid} 开始模拟运行（{change}；挑选期 Calmar {base['calmar']}→{best_m['calmar']}）")
    rec.update(decision="挑战者上线", reason=change)
    return rec


def evaluate_etf(now: datetime | None = None) -> list[str]:
    from .paper import etf_trend as et
    out = []
    rows = running("etf")
    if not rows:
        return out
    df = _etf_data()
    if df is None:
        return out
    cfg = et.get_config()
    for ch in rows:
        champ = json.loads(ch["champion_params"])
        if int(cfg["sma"]) != champ["sma"] or abs(float(cfg["band"]) - champ["band"]) > 1e-9:
            _close(ch, "expired", "现役参数已变化")
            continue
        cand = json.loads(ch["candidate_json"])
        tj = json.loads(ch["train_json"] or "{}")
        if "fast" not in tj:
            fc = fast_check_etf(champ, cand)
            tj["fast"] = fc
            db.ex("UPDATE evolve_challengers SET train_json=? WHERE id=?", (json.dumps(tj, ensure_ascii=False), ch["id"]))
            if fc["verdict"] == "pass":
                db.set_setting("etf_trend_config", dict(db.get_setting("etf_trend_config") or {}, sma=cand["sma"], band=cand["band"]))
                _close(ch, "promoted", "快速检验通过，晋级上线：" + fc["reason"])
                _event(None, "INFO", f"[ETF趋势] 自动进化：挑战者 #{ch['id']} 快速晋级，参数改为 {cand['sma']} 日均线 ±{cand['band']:.0%} — {fc['reason']}")
                out.append(f"ETF #{ch['id']} 快速晋级")
                continue
            if fc["verdict"] == "fail":
                _close(ch, "rejected", "快速检验明显更差，淘汰：" + fc["reason"])
                _event(None, "INFO", f"[ETF趋势] 自动进化：挑战者 #{ch['id']} 快速淘汰 — {fc['reason']}")
                out.append(f"ETF #{ch['id']} 快速淘汰")
                continue
        start = str(pd.Timestamp(ch["registered_at"]).tz_convert("America/New_York").date() + timedelta(days=1))
        end = str(df.index[-1].date())
        k = etf_backtest(df, cand["sma"], cand["band"], start, end)
        c = etf_backtest(df, champ["sma"], champ["band"], start, end)
        ev = {"at": db.now_iso(), "days": k["days"], "need_days": ETF_MIN_DAYS,
              "challenger": {"return_pct": k["ret"], "max_dd_pct": k["dd"], "trades": k["switches"]},
              "champion": {"return_pct": c["ret"], "max_dd_pct": c["dd"], "trades": c["switches"]}}
        db.ex("UPDATE evolve_challengers SET last_eval_json=? WHERE id=?", (json.dumps(ev), ch["id"]))
        if k["days"] < ETF_MIN_DAYS:
            continue
        why = f"前向 {k['days']} 个交易日：挑战者 {k['ret']:+.2f}% / 回撤 {k['dd']:.1f}%，现役 {c['ret']:+.2f}% / 回撤 {c['dd']:.1f}%"
        if k["ret"] >= c["ret"] - TOL_RET and k["dd"] >= c["dd"] - TOL_DD:
            db.set_setting("etf_trend_config", dict(db.get_setting("etf_trend_config") or {}, sma=cand["sma"], band=cand["band"]))
            _close(ch, "promoted", "晋级上线：" + why)
            _event(None, "INFO", f"[ETF趋势] 自动进化：挑战者 #{ch['id']} 晋级，参数改为 {cand['sma']} 日均线 ±{cand['band']:.0%} — {why}")
            out.append(f"ETF #{ch['id']} 晋级")
        else:
            _close(ch, "rejected", "淘汰：" + why)
            _event(None, "INFO", f"[ETF趋势] 自动进化：挑战者 #{ch['id']} 淘汰 — {why}")
            out.append(f"ETF #{ch['id']} 淘汰")
    return out


def fast_check_etf(champ: dict, cand: dict) -> dict:
    """跨市场检验：同样的参数用在标普 500（SPY 信号 → SSO 2 倍）上，该市场从未参与挑选。"""
    d = config.DATA_DIR / "etf_daily"
    if not ((d / "SPY.csv").exists() and (d / "SSO.csv").exists()):
        return {"verdict": "inconclusive", "reason": "缺少 SPY/SSO 日线"}
    def load(p):
        x = pd.read_csv(p)
        x["date"] = pd.to_datetime(x["date"], utc=True).dt.tz_convert("America/New_York").dt.normalize().dt.tz_localize(None)
        return x.drop_duplicates("date").set_index("date")
    s, t = load(d / "SPY.csv"), load(d / "SSO.csv")
    df = pd.DataFrame({"sig_close": s["close"], "open": t["open"], "close": t["close"]}).dropna()
    rows = []
    for a, b in (("2016-01-01", "2019-12-31"), ("2020-01-01", "2022-12-31"), ("2023-01-01", "2026-12-31")):
        k = etf_backtest(df, cand["sma"], cand["band"], a, b)
        c = etf_backtest(df, champ["sma"], champ["band"], a, b)
        rows.append({"period": f"{a[:4]}-{b[:4]}", "cand_calmar": k["calmar"], "champ_calmar": c["calmar"],
                     "cand_cagr": k["cagr"], "champ_cagr": c["cagr"]})
    wins = sum(1 for x in rows if x["cand_calmar"] >= x["champ_calmar"] - 0.02)
    txt = "标普 500（SPY→SSO）三段：" + "，".join(f"{x['period']} Calmar {x['champ_calmar']}→{x['cand_calmar']}" for x in rows)
    v = "pass" if wins == len(rows) else ("fail" if wins <= 1 else "inconclusive")
    return {"verdict": v, "reason": txt, "rows": rows}


def evaluate_all(now: datetime | None = None) -> list[str]:
    out = []
    try:
        out += evaluate_btc(now)
    except Exception:  # noqa: BLE001
        log.exception("evaluate_btc failed")
    try:
        out += evaluate_etf(now)
    except Exception:  # noqa: BLE001
        log.exception("evaluate_etf failed")
    db.set_setting("challenger_last_eval", {"at": db.now_iso(), "actions": out})
    return out
