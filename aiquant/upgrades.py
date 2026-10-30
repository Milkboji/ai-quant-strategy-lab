"""一次性策略升级（幂等）：服务启动后由后台循环执行，每项只执行一次，结果写入 settings。

升级不直接改动模拟盘，而是登记为“挑战者”，走系统自己的检验流程（快速检验：更早的 BTC 历史 + ETH；
不确定时转为实时前向比较），通过后在空仓时切换。
"""
from __future__ import annotations

import copy
import json
import logging

from . import db

log = logging.getLogger("aiquant.upgrades")

BB_MID_EXIT_CHANGE = "布林带：日线收盘跌破中轨（20 日均线）即离场"


def _is_daily_ema_cross(sch: dict, tf: int) -> bool:
    ids = {i["id"]: i for i in sch.get("indicators", [])}
    if tf != 1440 or "ema9" not in ids or "ema21" not in ids:
        return False
    ex = (sch.get("exit") or {}).get("conditions", [])
    has_cross_exit = any(c["op"] == "cross_below" and c["left"].get("ref") == "ema9" for c in ex)
    has_mid_exit = any(i["type"] == "BB_MID" for i in sch.get("indicators", []))
    return has_cross_exit and not has_mid_exit


def bb_mid_exit_candidate(sch: dict) -> dict:
    d = copy.deepcopy(sch)
    d["indicators"].append({"id": "bb_mid", "type": "BB_MID", "params": {"period": 20, "std": 2}, "tf": "main",
                            "source": "close"})
    d["exit"]["conditions"].append({"left": {"ref": "close", "mult": 1.0, "value": None}, "op": "<",
                                    "right": {"ref": "bb_mid", "mult": 1.0, "value": None}})
    d["exit"]["mode"] = "any"
    h = d.get("hypothesis") or ""
    d["hypothesis"] = (h + " " if h else "") + "日线收盘跌破布林中轨时趋势转弱，提前离场以减少利润回吐。"
    return d


def _bb_mid_exit() -> dict:
    """日线 EMA9/21 趋势策略：增加“收盘跌破布林中轨即离场”。
    研究：2021-03 → 2026-10 BTC 与 ETH 各 6 个年度（含交易节奏规则），中轨周期 15–40 全部优于现役（docs/bollinger.md）。"""
    from . import challenger, evolve, research
    from .data import market_data as md
    from .paper import runner as pr
    from .strategy import registry
    from .strategy.schema import validate
    out = []
    r = research.get_research()
    tr = evolve._windows()["train"]
    for row in db.q("SELECT id FROM paper_deployments WHERE broker='alpaca_paper' AND is_test=0 "
                    "AND status IN ('running','paused_entries') ORDER BY id"):
        dep = pr.get_deployment(row["id"])
        champ = registry.get_version(dep["version_id"])
        sch = dict(champ["schema"])
        tf = int(dep["config"].get("signal_tf") or sch.get("signal_tf") or 15)
        sch["signal_tf"] = tf
        if not _is_daily_ema_cross(sch, tf):
            continue
        cand = bb_mid_exit_candidate(sch)
        if validate(cand)[1]:
            continue
        brief = {"score": None, "return_pct": None, "max_dd_pct": None, "trades": None}
        try:
            ds = md.get_dataset(*tr, source=r["source"])
            e = evolve.evaluate(md.load_dataset(ds), cand, tr[0], tr[1], r)
            brief = evolve._brief(e)
        except Exception as ex:  # noqa: BLE001
            log.info("train eval for upgrade failed: %s", ex)
        dep_row = db.q1("SELECT * FROM paper_deployments WHERE id=?", (dep["id"],))
        cid = challenger.register_btc(dep_row, champ, cand, BB_MID_EXIT_CHANGE, brief, False)
        out.append(f"#{dep['id']} → 挑战者 #{cid}")
    return {"registered": out}


def _my_system_v1() -> dict:
    """我的交易系统 v1：加密货币上限 → 56%，部署加密货币（试用期）。美股按 10-30 下午指令不部署（收益回撤比 0.15 vs ETF 择时 1.02）。"""
    from . import my_system
    return my_system.initial_deploy()


def _my_system_adjust_1030() -> dict:
    """10-30 下午：下线美股 v1（如已部署；有持仓等离场后停）、加密货币只保留 BTC/ETH、关闭保本止损（冻结新版本部署）。"""
    from . import my_system
    return my_system.adjust_1030()


UPGRADES = [("bb_mid_exit_v1", _bb_mid_exit), ("my_system_v1_deploy", _my_system_v1),
            ("my_system_adjust_1030", _my_system_adjust_1030)]


def run_pending() -> list[str]:
    done = []
    for key, fn in UPGRADES:
        k = f"upgrade::{key}"
        if db.get_setting(k):
            continue
        try:
            res = fn()
            db.set_setting(k, {"at": db.now_iso(), "result": res})
            done.append(f"{key}: {json.dumps(res, ensure_ascii=False)}")
            log.info("upgrade %s done: %s", key, res)
        except Exception as e:  # noqa: BLE001
            log.exception("upgrade %s failed", key)
            db.set_setting(k, {"at": db.now_iso(), "error": str(e)[:300]})
    return done
