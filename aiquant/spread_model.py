"""点差模型：用 Alpaca 历史报价按时段估计真实点差，替换回测里的固定代理值。

口径（cost basis）
- fixed_v1：固定代理值（加密货币 1 bps、美股 2 bps，完整点差）。在切换前生成的回测都属于这个口径，称为「旧口径」。
- quotes_vN：第 N 版报价估计。切换时把每个品种「按 UTC 小时的完整点差中位数」冻结成快照（settings.spread_basis），
  之后的回测都用这份快照，结果可复现；重新估计要生成新版本，同样需要管理员确认。

流程
1. 采样 `collect()`：后台循环每轮最多 REQ_PER_TICK 次请求（美股开盘前后 9:25~9:50 美东暂停，避免拖慢下单）。
   每个（品种, 日期, 时段）只取一页（≤1000 条报价），算点差中位数和 P90，写入 spread_samples。
   - BTC/USD：24 个 UTC 小时，每小时 :20 起 10 分钟。
   - 美股：09:31、09:35（执行器下单时刻）、09:45、11:00、13:00、15:30（美东），各取 5 分钟。
   所有请求走 aiquant/data/http.py 的唯一出口（data.alpaca.markets，只读 GET），写审计。
2. 估计 `proposal()`：每个时段取各天中位数的中位数；样本 ≥ MIN_DAYS 天的品种才进入新口径。
   美股日线回测在开盘成交，开盘所在的 UTC 小时（13、14 点，夏令时 / 冬令时）用 09:35 的估计，其他小时用盘中估计。
3. 报告 `build_report()`：对当前部署的每个版本，在训练期（不触及验证 / 测试集）分别用旧口径和新口径回测，写差异。
4. 切换 `enable()`：管理员在报告页确认后生效。旧回测不重算、不覆盖，只在页面上标为「旧口径」。
"""
from __future__ import annotations

import hashlib
import json
import logging
import statistics
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

import pandas as pd

from . import config, db

log = logging.getLogger("aiquant.spread_model")
FIXED = "fixed_v1"
LOOKBACK_DAYS = 20          # 采样最近 20 天（美股按交易日）
MIN_DAYS = 10               # 每个品种至少 10 天样本才能进入新口径
MIN_QUOTES = 20             # 一个样本至少 20 条有效报价
REQ_PER_TICK = 10           # 后台循环每轮最多请求次数
MAX_ATTEMPTS = 3
US_BUCKETS = ["09:31", "09:35", "09:45", "11:00", "13:00", "15:30"]
US_OPEN_BUCKET = "09:35"
US_INTRADAY = ["09:45", "11:00", "13:00", "15:30"]
DEFAULT_US_SYMBOLS = ["QQQ", "SPY", "QLD", "SSO", "TQQQ", "UPRO"]
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS spread_samples (
  symbol TEXT NOT NULL, day TEXT NOT NULL, bucket TEXT NOT NULL, n INTEGER, median_bps REAL, p90_bps REAL,
  status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, err TEXT, fetched_at TEXT,
  PRIMARY KEY (symbol, day, bucket));
CREATE TABLE IF NOT EXISTS spread_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, actor TEXT, candidate_json TEXT NOT NULL,
  rows_json TEXT NOT NULL, markdown TEXT NOT NULL, profiles_hash TEXT NOT NULL);
"""


def ensure_tables() -> None:
    with db.tx() as c:
        c.executescript(SCHEMA_SQL)


# ---------------------------------------------------------------------------
# 口径
# ---------------------------------------------------------------------------

def current() -> dict | None:
    """已启用的报价估计口径（快照）；None = 仍是固定代理值（旧口径）。"""
    return db.get_setting("spread_basis")


def current_name() -> str:
    b = current()
    return f"quotes_v{b['version']}" if b else FIXED


def basis_label(name: str | None) -> str:
    name = name or FIXED
    if name == FIXED:
        return "固定点差代理（旧口径）" if current() else "固定点差代理"
    return f"实测点差 v{name.split('_v')[-1]}"


def is_old(name: str | None) -> bool:
    """回测结果的成本口径与当前口径不同（例如切换后，之前按固定代理值算的结果）。"""
    return (name or FIXED) != current_name()


def profile(symbol: str | None) -> list[float] | None:
    b = current()
    if not b or not symbol:
        return None
    p = (b.get("profiles") or {}).get(symbol)
    return list(p) if p else None


def apply(cm, symbol: str | None):
    """把当前口径套到成本模型上：有该品种的快照 → 分时段点差；否则保持固定代理值（并在 basis 里写明）。"""
    p = profile(symbol)
    if p is None:
        return cm
    b = current()
    return replace(cm, spread_hourly=tuple(float(x) for x in p), spread_bps=float(max(p)),
                   spread_source=f"Alpaca 历史报价估计 v{b['version']}（{b.get('window', '')}，按 UTC 小时）",
                   basis=f"quotes_v{b['version']}")


def open_spread(symbol: str | None) -> float | None:
    """美股开盘时段的完整点差（ETF 择时回测在开盘调仓）；None = 用固定代理值。"""
    from . import market as mk
    p = profile(symbol)
    if p is None:
        return None
    today = datetime.now(timezone.utc).astimezone(mk.ET).date()
    return float(p[mk.session_open(_last_trading_day(today)).hour])


def _last_trading_day(d: date) -> date:
    from . import market as mk
    while not mk.is_trading_day(d):
        d -= timedelta(days=1)
    return d


# ---------------------------------------------------------------------------
# 采样
# ---------------------------------------------------------------------------

def symbols() -> list[str]:
    """采样品种：BTC/USD + 已部署 / ETF 择时的美股交易品种 + 常用杠杆 ETF。"""
    out = ["BTC/USD"]
    us = list(db.get_setting("spread_us_symbols") or DEFAULT_US_SYMBOLS)
    try:
        for d in db.q("SELECT config_json FROM paper_deployments WHERE broker='alpaca_us' AND is_test=0 "
                      "AND status IN ('running','paused_entries')"):
            c = json.loads(d["config_json"] or "{}")
            for k in ("trade_symbol", "signal_symbol"):
                if c.get(k):
                    us.append(c[k])
    except Exception:  # noqa: BLE001
        pass
    try:
        from .paper import etf_trend
        c = etf_trend.get_config()
        us += [c.get("trade_symbol"), c.get("signal_symbol")]
    except Exception:  # noqa: BLE001
        pass
    for s in us:
        if s and s not in out:
            out.append(s)
    return out


def _days(sym: str, today: date) -> list[date]:
    from . import market as mk
    if sym == "BTC/USD":
        return [today - timedelta(days=k) for k in range(LOOKBACK_DAYS, 0, -1)]
    days, d = [], today - timedelta(days=1)
    while len(days) < LOOKBACK_DAYS and d > today - timedelta(days=60):
        if mk.is_trading_day(d):
            days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


def buckets(sym: str, day: date) -> list[tuple[str, datetime, datetime]]:
    """(时段名, 开始, 结束) UTC。"""
    from . import market as mk
    if sym == "BTC/USD":
        base = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        return [(f"{h:02d}", base + timedelta(hours=h, minutes=20), base + timedelta(hours=h, minutes=30)) for h in range(24)]
    out = []
    close = mk.session_close(day)
    for b in US_BUCKETS:
        hh, mm = map(int, b.split(":"))
        st = datetime.combine(day, time(hh, mm), tzinfo=mk.ET).astimezone(timezone.utc)
        if st + timedelta(minutes=5) <= close:
            out.append((b, st, st + timedelta(minutes=5)))
    return out


def todo(now: datetime | None = None) -> list[tuple[str, str, str, datetime, datetime]]:
    """还没采到的样本（失败的最多重试 MAX_ATTEMPTS 次）。"""
    ensure_tables()
    now = now or datetime.now(timezone.utc)
    have = {(r["symbol"], r["day"], r["bucket"]): r for r in db.q("SELECT symbol,day,bucket,status,attempts FROM spread_samples")}
    out = []
    for sym in symbols():
        for d in _days(sym, now.date()):
            for b, st, en in buckets(sym, d):
                r = have.get((sym, str(d), b))
                if en > now - timedelta(minutes=20):           # 免费行情：最近 15 分钟的 SIP 数据不可取
                    continue
                if r and (r["status"] == "ok" or r["attempts"] >= MAX_ATTEMPTS or r["status"] == "empty"):
                    continue
                out.append((sym, str(d), b, st, en))
    return out


def _quiet_window(now: datetime) -> bool:
    """美股开盘前后（美东 9:25~9:50）不采样，避免拖慢执行器下单。"""
    from . import market as mk
    et = now.astimezone(mk.ET)
    return mk.is_trading_day(et.date()) and time(9, 25) <= et.time() <= time(9, 50)


def fetch_quotes(sym: str, start: datetime, end: datetime) -> list[dict]:
    """一页历史报价（≤1000 条）。只读 GET，走唯一出口。"""
    from .data import http
    from .data.alpaca import _data_headers
    iso = lambda t: t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    if sym == "BTC/USD":
        r = http.request("GET", f"{config.ALPACA_DATA_URL}/v1beta3/crypto/us/quotes", headers=_data_headers(),
                         params={"symbols": sym, "start": iso(start), "end": iso(end), "limit": 1000, "sort": "asc"},
                         timeout=20)
        if r.status_code != 200:
            raise RuntimeError(f"报价获取失败 {r.status_code}: {r.text[:120]}")
        return list(((r.json() or {}).get("quotes") or {}).get(sym) or [])
    r = http.request("GET", f"{config.ALPACA_DATA_URL}/v2/stocks/{sym}/quotes", headers=_data_headers(),
                     params={"start": iso(start), "end": iso(end), "limit": 1000, "feed": "sip", "sort": "asc"},
                     timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"报价获取失败 {r.status_code}: {r.text[:120]}")
    return list((r.json() or {}).get("quotes") or [])


def summarize(quotes: list[dict]) -> dict | None:
    """一页报价 → 完整点差（bps）的中位数 / P90。去掉无效报价（买价或卖价为 0、交叉盘）。"""
    xs = []
    for q in quotes:
        bp, ap = float(q.get("bp") or 0), float(q.get("ap") or 0)
        if bp > 0 and ap >= bp:
            xs.append((ap - bp) / ((ap + bp) / 2) * 1e4)
    if len(xs) < MIN_QUOTES:
        return None
    xs.sort()
    return {"n": len(xs), "median": statistics.median(xs), "p90": xs[min(len(xs) - 1, int(len(xs) * 0.9))]}


def collect(now: datetime | None = None, max_requests: int = REQ_PER_TICK) -> int:
    """采样若干个缺失的（品种, 日期, 时段）。返回本次请求次数。"""
    now = now or datetime.now(timezone.utc)
    if _quiet_window(now) or not config.secret("ALPACA_API_KEY_ID"):
        return 0
    n = 0
    for sym, day, b, st, en in todo(now)[:max_requests]:
        n += 1
        prev = db.q1("SELECT attempts FROM spread_samples WHERE symbol=? AND day=? AND bucket=?", (sym, day, b))
        att = (prev["attempts"] if prev else 0) + 1
        try:
            s = summarize(fetch_quotes(sym, st, en))
            if s is None:
                db.ex("INSERT OR REPLACE INTO spread_samples(symbol,day,bucket,n,median_bps,p90_bps,status,attempts,err,fetched_at)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?)", (sym, day, b, 0, None, None, "empty", att, None, db.now_iso()))
            else:
                db.ex("INSERT OR REPLACE INTO spread_samples(symbol,day,bucket,n,median_bps,p90_bps,status,attempts,err,fetched_at)"
                      " VALUES(?,?,?,?,?,?,?,?,?,?)", (sym, day, b, s["n"], round(s["median"], 4), round(s["p90"], 4),
                                                       "ok", att, None, db.now_iso()))
        except Exception as e:  # noqa: BLE001
            db.ex("INSERT OR REPLACE INTO spread_samples(symbol,day,bucket,n,median_bps,p90_bps,status,attempts,err,fetched_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?,?)", (sym, day, b, 0, None, None, "error", att, str(e)[:200], db.now_iso()))
    return n


# ---------------------------------------------------------------------------
# 估计
# ---------------------------------------------------------------------------

def bucket_stats(sym: str) -> dict[str, dict]:
    """每个时段：各天中位数的中位数、P90 的中位数、天数。"""
    ensure_tables()
    out: dict[str, dict] = {}
    rows = db.q("SELECT bucket, day, median_bps, p90_bps FROM spread_samples WHERE symbol=? AND status='ok' ORDER BY day",
                (sym,))
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(r["bucket"], []).append(r)
    for b, rr in by.items():
        out[b] = {"median_bps": statistics.median(x["median_bps"] for x in rr),
                  "p90_bps": statistics.median(x["p90_bps"] for x in rr), "days": len({x["day"] for x in rr}),
                  "first": rr[0]["day"], "last": rr[-1]["day"]}
    return out


def estimate(sym: str) -> dict | None:
    """一个品种的 24 小时点差快照；样本不足返回 None。"""
    st = bucket_stats(sym)
    if not st:
        return None
    days = min(v["days"] for v in st.values())
    if sym == "BTC/USD":
        if len(st) < 24 or days < MIN_DAYS:
            return None
        prof = [round(st[f"{h:02d}"]["median_bps"], 3) for h in range(24)]
    else:
        if US_OPEN_BUCKET not in st or not any(b in st for b in US_INTRADAY) or st[US_OPEN_BUCKET]["days"] < MIN_DAYS:
            return None
        days = st[US_OPEN_BUCKET]["days"]
        intra = statistics.median(st[b]["median_bps"] for b in US_INTRADAY if b in st)
        prof = [round(intra, 3)] * 24
        prof[13] = prof[14] = round(st[US_OPEN_BUCKET]["median_bps"], 3)   # 开盘所在 UTC 小时（夏令时 13 点 / 冬令时 14 点）
    first = min(v["first"] for v in st.values())
    last = max(v["last"] for v in st.values())
    return {"profile": prof, "days": days, "first": first, "last": last, "buckets": st}


def proposal() -> dict:
    """候选口径：样本足够的品种。"""
    profs, info, missing = {}, {}, []
    for sym in symbols():
        e = estimate(sym)
        if e is None:
            missing.append(sym)
            continue
        profs[sym] = e["profile"]
        info[sym] = {k: e[k] for k in ("days", "first", "last")}
    b = current()
    ver = (b["version"] + 1) if b else 1
    window = (f"{min(v['first'] for v in info.values())} ~ {max(v['last'] for v in info.values())}" if info else "")
    return {"version": ver, "profiles": profs, "info": info, "missing": missing, "window": window,
            "hash": hashlib.sha256(json.dumps(profs, sort_keys=True).encode()).hexdigest()[:16]}


def progress(now: datetime | None = None) -> dict:
    """采样进度（页面显示）。"""
    ensure_tables()
    left = todo(now)
    ok = db.q1("SELECT COUNT(*) AS n FROM spread_samples WHERE status='ok'")["n"]
    err = db.q1("SELECT COUNT(*) AS n FROM spread_samples WHERE status='error' AND attempts>=?", (MAX_ATTEMPTS,))["n"]
    return {"ok": ok, "left": len(left), "failed": err, "symbols": symbols()}


# ---------------------------------------------------------------------------
# 新旧口径对比报告
# ---------------------------------------------------------------------------

def _train_window(market) -> tuple[datetime, datetime]:
    """训练期（不触及验证 / 测试集）：加密货币取验证集开始前 365 天；美股取验证集开始前 3 年。"""
    from . import research
    from .data import market_data as md
    sp = research.get_split(market)
    vs = md.to_utc(sp["val_start"])
    return (vs - timedelta(days=365) if market.key == "crypto" else vs - timedelta(days=365 * 3)), vs


def _deployed() -> list[dict]:
    return db.q("SELECT * FROM paper_deployments WHERE broker IN ('alpaca_paper','alpaca_us') AND is_test=0 "
                "AND status IN ('running','paused_entries') ORDER BY id")


def _bt_pair(cand: dict, dep: dict) -> dict | None:
    """同一版本、同一窗口：旧口径 vs 新口径。"""
    from . import market as mk
    from . import research
    from .engine import backtest as bt
    from .paper import runner as pr
    from .strategy import registry
    from .strategy.schema import validate
    d = pr.get_deployment(dep["id"])
    cfg = d["config"]
    if d["broker"] == "alpaca_us" and cfg.get("kind") == "etf_sma":
        return _etf_pair(cand, d)
    v = registry.get_version(d["version_id"])
    obj, errs = validate(v["schema"])
    if errs:
        return None
    if d["broker"] == "alpaca_us":
        from .data import us_daily
        m, sym = mk.US_EQUITY, cfg.get("trade_symbol")
        sig = cfg.get("signal_symbol") or sym
        df = us_daily.to_bars(us_daily.load(sym))
        dsig = us_daily.to_bars(us_daily.load(sig)) if sig != sym else None
        tf, aux = 1440, None
    else:
        from .data import market_data as md
        m, sym, dsig = mk.CRYPTO, "BTC/USD", None
        df = md.load_master()
        tf, aux = int(cfg.get("signal_tf") or obj.signal_tf or 60), cfg.get("aux_tf") or obj.aux_tf
    s, e = _train_window(m)
    df = df[(df["ts"] >= pd.Timestamp(s) - pd.Timedelta(days=420)) & (df["ts"] < pd.Timestamp(e))].reset_index(drop=True)
    if dsig is not None:
        dsig = dsig[dsig["ts"] < pd.Timestamp(e)].reset_index(drop=True)
    r = research.get_research()
    if m.key != "crypto":
        r = dict(r, symbol=sym)
    old_cfg = research.bt_config(s, e, r, m)
    old_cfg.cost = research.base_cost_model(r, m)
    prof = cand["profiles"].get(sym)
    if prof is None:
        return {"deployment": d["id"], "label": cfg.get("label") or f"#{d['id']}", "symbol": sym, "skipped": "该品种样本不足"}
    new_cfg = research.bt_config(s, e, r, m)
    new_cfg.cost = replace(old_cfg.cost, spread_hourly=tuple(prof), spread_bps=float(max(prof)),
                           basis=f"quotes_v{cand['version']}")
    a = bt.run(df, obj, tf, aux, old_cfg, df_signal=dsig)["metrics"]
    b = bt.run(df, obj, tf, aux, new_cfg, df_signal=dsig)["metrics"]
    return {"deployment": d["id"], "label": cfg.get("label") or f"#{d['id']}", "symbol": sym, "window": f"{s:%Y-%m-%d} ~ {e:%Y-%m-%d}",
            "old": _m(a), "new": _m(b), "diff_pp": round(b["net_return_pct"] - a["net_return_pct"], 3)}


def _m(x: dict) -> dict:
    return {"ret": round(float(x["net_return_pct"]), 3), "trades": int(x["trades"]), "spread": round(float(x["spread_cost"]), 2),
            "cost": round(float(x["total_cost"]), 2), "dd": round(float(x["max_drawdown_pct"]), 3)}


def _etf_pair(cand: dict, d: dict) -> dict | None:
    from . import challenger
    from . import market as mk
    from . import rhythm
    from .engine import regime_bt
    from .paper import etf_trend
    c = etf_trend.get_config()
    sym = c.get("trade_symbol")
    df = challenger._etf_data()
    if df is None:
        return None
    s, e = _train_window(mk.US_EQUITY)
    prof = cand["profiles"].get(sym)
    if prof is None:
        return {"deployment": d["id"], "label": d["config"].get("label") or "ETF 择时", "symbol": sym, "skipped": "该品种样本不足"}
    op = float(prof[mk.session_open(_last_trading_day(e.date() - timedelta(days=1))).hour])
    kw = dict(market=mk.US_EQUITY, rhythm=rhythm.current(mk.US_EQUITY))
    a = regime_bt.backtest(df, int(c["sma"]), float(c["band"]), f"{s:%Y-%m-%d}", f"{e - timedelta(days=1):%Y-%m-%d}", **kw)
    b = regime_bt.backtest(df, int(c["sma"]), float(c["band"]), f"{s:%Y-%m-%d}", f"{e - timedelta(days=1):%Y-%m-%d}",
                           spread_bps=op, **kw)
    conv = lambda k: {"ret": round(float(k["ret"]), 3), "trades": int(k.get("switches") or 0), "spread": None,  # noqa: E731
                      "cost": None, "dd": round(float(k["dd"]), 3)}
    return {"deployment": d["id"], "label": d["config"].get("label") or "ETF 择时", "symbol": sym,
            "window": f"{s:%Y-%m-%d} ~ {e:%Y-%m-%d}", "old": conv(a), "new": conv(b), "diff_pp": round(b["ret"] - a["ret"], 3)}


def build_report(actor: str | None = None) -> dict:
    """生成新旧口径对比报告（保存，不切换）。"""
    from . import market as mk
    ensure_tables()
    cand = proposal()
    if not cand["profiles"]:
        raise ValueError("还没有足够的报价样本（每个品种至少 10 天），稍后再生成报告")
    rows = []
    for dep in _deployed():
        try:
            x = _bt_pair(cand, dep)
        except Exception as e:  # noqa: BLE001
            log.warning("spread report backtest failed for #%s", dep["id"], exc_info=True)
            x = {"deployment": dep["id"], "label": f"#{dep['id']}", "skipped": f"回测失败：{type(e).__name__}: {str(e)[:80]}"}
        if x:
            rows.append(x)
    fixed = {"BTC/USD": mk.CRYPTO.spread_bps}
    lines = [f"# 点差模型：新旧口径回测对比（实测点差 v{cand['version']} 候选）", "",
             f"生成时间：{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC。样本区间：{cand['window']}。", "",
             "## 方法", "",
             "- 数据：Alpaca 历史报价（BTC/USD 用加密货币报价；美股用 SIP 全市场最优报价）。",
             "- 每个（品种, 日期, 时段）取一页报价（最多 1000 条），算完整点差 = (卖一 − 买一) ÷ 中间价；"
             "每个时段取各天中位数的中位数。",
             "- BTC/USD 按 24 个 UTC 小时分别估计；美股取 09:35（执行器下单时刻）作为开盘点差，其他时段的中位数作为盘中点差。"
             "日线回测在开盘成交，所以美股主要用开盘点差。",
             "- 回测窗口：训练期（加密货币为验证集开始前 365 天，美股为验证集开始前 3 年），不触及验证集和测试集。",
             "- 只替换点差；手续费、滑点不变。", "",
             "## 估计结果（完整点差，bps）", "",
             "| 品种 | 旧口径（固定） | 新口径：最低 / 中位 / 最高（按小时） | 美股开盘 | 样本天数 |", "| --- | --- | --- | --- | --- |"]
    for sym, p in cand["profiles"].items():
        old = fixed.get(sym, mk.US_EQUITY.spread_bps)
        us_open = f"{p[13]:.2f}" if sym != "BTC/USD" else "--"
        lines.append(f"| {sym} | {old:g} | {min(p):.2f} / {statistics.median(p):.2f} / {max(p):.2f} | {us_open} | "
                     f"{cand['info'][sym]['days']} |")
    if cand["missing"]:
        lines += ["", f"样本不足、仍用固定代理值的品种：{'、'.join(cand['missing'])}。"]
    lines += ["", "## 回测差异（同一版本、同一窗口）", "",
              "| 部署 | 品种 | 窗口 | 旧口径收益 % | 新口径收益 % | 差异（百分点） | 交易笔数 | 点差成本 旧 → 新（USD） |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for x in rows:
        if x.get("skipped"):
            lines.append(f"| #{x['deployment']} {x['label']} | {x.get('symbol') or '--'} | -- | -- | -- | -- | -- | {x['skipped']} |")
            continue
        o, n = x["old"], x["new"]
        sc = f"{o['spread']:,.2f} → {n['spread']:,.2f}" if o["spread"] is not None else "--（ETF 择时按收益率计）"
        lines.append(f"| #{x['deployment']} {x['label']} | {x['symbol']} | {x['window']} | {o['ret']:.2f} | {n['ret']:.2f} | "
                     f"{x['diff_pp']:+.2f} | {o['trades']} | {sc} |")
    lines += ["", "## 切换说明", "",
              "- 切换前生成的回测结果不重算、不覆盖，页面上标为「旧口径」。",
              "- 切换后新的回测（策略实验室、自动进化、挑战者检验、长周期体检、策略成绩单）都用新口径。",
              "- 新口径是冻结快照；之后再估计会成为 v" + str(cand["version"] + 1) + "，同样需要确认。"]
    md_text = "\n".join(lines) + "\n"
    rid = db.ex("INSERT INTO spread_reports(created_at,actor,candidate_json,rows_json,markdown,profiles_hash) VALUES(?,?,?,?,?,?)",
                (db.now_iso(), actor or "系统", json.dumps(cand, ensure_ascii=False), json.dumps(rows, ensure_ascii=False),
                 md_text, cand["hash"]))
    return {"id": rid, "candidate": cand, "rows": rows, "markdown": md_text}


def latest_report() -> dict | None:
    ensure_tables()
    r = db.q1("SELECT * FROM spread_reports ORDER BY id DESC LIMIT 1")
    if not r:
        return None
    return dict(r, candidate=json.loads(r["candidate_json"]), rows=json.loads(r["rows_json"]))


def enable(report_id: int, *, actor: str | None, role: str | None) -> str:
    """管理员确认后切换到报告里的候选口径。"""
    if role != "admin":
        raise PermissionError("只有管理员可以切换点差口径")
    ensure_tables()
    r = db.q1("SELECT * FROM spread_reports WHERE id=?", (report_id,))
    if not r:
        raise ValueError("报告不存在")
    cand = json.loads(r["candidate_json"])
    cur = current()
    if cur and cur.get("hash") == cand["hash"]:
        return f"已经是实测点差 v{cur['version']}"
    if cand["version"] != ((cur["version"] + 1) if cur else 1):
        raise ValueError("报告已过期（口径版本变化），请重新生成报告")
    snap = {"version": cand["version"], "profiles": cand["profiles"], "info": cand["info"], "window": cand["window"],
            "hash": cand["hash"], "report_id": report_id, "enabled_at": db.now_iso(), "enabled_by": actor or ""}
    db.set_setting("spread_basis", snap)
    for row in db.q("SELECT key FROM settings WHERE key LIKE 'rhythm_long::%'"):   # 长周期体检缓存按新口径重算
        db.set_setting(row["key"], None)
    try:
        from . import auth
        auth.audit(actor or "", "切换点差口径", "spread", f"实测点差 v{cand['version']}（报告 #{report_id}，"
                   f"{len(cand['profiles'])} 个品种，样本 {cand['window']}）")
    except Exception:  # noqa: BLE001
        log.exception("spread audit failed")
    from .paper import runner as pr
    pr.event(None, "INFO", f"回测成本口径切换为实测点差 v{cand['version']}（{'、'.join(cand['profiles'])}）。"
                           "之前的回测结果保留，标为旧口径")
    return f"已切换为实测点差 v{cand['version']}"
