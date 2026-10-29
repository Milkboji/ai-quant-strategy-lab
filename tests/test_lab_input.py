"""策略实验室中文输入：速查表覆盖白名单、布林带新写法、改写建议与一键替换、大白话复述、确认后才能保存、ETF 版本。"""
import pytest

from aiquant import db
from aiquant.strategy import parser, registry
from aiquant.strategy.narrate import narrate
from aiquant.strategy.schema import INDICATOR_TYPES


def _with_entry_exit(w: str) -> str:
    if "买入" in w:
        return w + "，固定止损2%"
    if "卖出" in w:
        return "EMA20上穿EMA60时买入，" + w
    return w + "时买入，固定止损2%"


def test_cheatsheet_covers_whitelist_and_examples_parse():
    rows = parser.cheatsheet()
    assert {r["指标"] for r in rows if r["指标"]} == set(INDICATOR_TYPES)
    for r in rows:
        if not r["指标"] or "暂无" in r["写法"]:
            continue
        res = parser.parse(_with_entry_exit(r["写法"]))
        types = {i["type"] for i in (res.schema or {}).get("indicators", [])}
        assert res.status == "ok" and not res.unresolved, (r["写法"], res.errors, res.unresolved)
        assert r["指标"] in types, (r["写法"], types)
    bb = [r for r in rows if r["指标"].startswith("BB_")]
    assert {r["指标"] for r in bb} >= {"BB_PCTB", "BB_WIDTH", "BB_WIDTH_RANK", "BB_SQUEEZE", "BB_MID"}


def test_new_bollinger_and_roc_syntax():
    s = parser.parse("EMA9上穿EMA21且%B大于50时买入，跌破布林中轨时卖出，固定止损3%").schema
    pctb = next(i for i in s["indicators"] if i["type"] == "BB_PCTB")
    assert pctb["params"] == {"period": 20, "std": 2.0}
    c = next(c for c in s["entry"]["conditions"] if c["left"]["ref"] == pctb["id"])
    assert c["op"] == ">" and c["right"]["value"] == 50
    s = parser.parse("带宽分位(200)小于10时买入，止盈8%").schema
    r = next(i for i in s["indicators"] if i["type"] == "BB_WIDTH_RANK")
    assert r["params"]["lookback"] == 200
    s = parser.parse("布林带收口(10)时买入，止损2%").schema
    sq = next(i for i in s["indicators"] if i["type"] == "BB_SQUEEZE")
    assert sq["params"]["recent"] == 10 and s["entry"]["conditions"][0]["op"] == "<="
    s = parser.parse("布林带宽大于8时买入，止损2%").schema
    assert any(i["type"] == "BB_WIDTH" for i in s["indicators"])
    s = parser.parse("ROC(10)大于2时买入，止损2%").schema
    roc = next(i for i in s["indicators"] if i["type"] == "ROC")
    assert roc["params"]["period"] == 10 and s["entry"]["conditions"][0]["right"]["value"] == 2


def test_suggestions_are_recognizable_and_replace_fixes_text():
    for frag in ["均线多头排列时买入", "RSI超卖时买入", "突破20日高点", "跌到布林中线下面卖出", "macd红柱时买入", "乱写一通"]:
        sug = parser.suggest(frag)
        assert 1 <= len(sug) <= 3, frag
        for w in sug:
            core = w.replace("时买入", "").replace("时卖出", "")
            assert parser._recognized(core), (frag, w)
    assert parser.suggest("跌到布林中线下面卖出")[0].endswith("时卖出")
    text = "均线多头排列时买入，固定止损2%"
    res = parser.parse(text)
    assert res.status == "incomplete" and res.unresolved
    frag = res.unresolved[0]
    fixed = text.replace(frag, parser.suggest(frag)[0], 1) if frag in text else text
    res2 = parser.parse(fixed)
    assert res2.status == "ok" and not res2.unresolved, (fixed, res2.unresolved)


def test_narrate_covers_every_part():
    s = parser.parse("4小时EMA20上穿EMA60且RSI(14)大于50时买入，跌破布林中轨时卖出，固定止损2%，止盈10%，"
                     "盈利3%后启动移动止损，回撤1.5%离场，仓位50%").schema
    n = dict(narrate(s))
    assert "4 小时" in n["周期"]
    assert "买入信号：20 周期指数均线 从下往上穿过 60 周期指数均线" in n["入场"]
    assert "14 周期 RSI 高于 50" in n["过滤条件"]
    assert "收盘价 低于 布林中轨" in n["离场"]
    assert "亏损达到 2%" in n["止损止盈"] and "盈利达到 10%" in n["止损止盈"] and "回撤 1.5%" in n["止损止盈"]
    assert "50%" in n["仓位"] and "不加杠杆" in n["仓位"]
    assert narrate({"bad": 1}) == []


def test_confirmation_required_before_saving(env):
    n = 2
    s = parser.parse("SMA10上穿SMA30时买入，固定止损4%", "确认测试").schema
    s["name"] = "确认测试"
    registry.save_draft(n, text="SMA10上穿SMA30时买入，固定止损4%", draft_schema=s,
                        parse_info={"status": "ok", "errors": [], "warnings": [], "unresolved": [], "method": "测试",
                                    "text": "SMA10上穿SMA30时买入，固定止损4%"})
    assert not registry.draft_confirmed(n)
    from aiquant.ui import page_lab
    ok, msg = page_lab.ensure_frozen(n)
    assert not ok and "确认无误" in msg
    registry.confirm_draft(n, "tester")
    assert registry.draft_confirmed(n)
    ok, msg = page_lab.ensure_frozen(n)
    assert ok, msg
    v = registry.get_version(registry.get_slot(n)["current_version_id"])
    assert v["name"] == "确认测试"
    ok, msg = page_lab.ensure_frozen(n)                    # 与已冻结版本相同：不需要再次确认
    assert ok and "相同" in msg
    s2 = dict(s, stop_loss_pct=5.0)                        # 草稿一改，确认失效
    registry.save_draft(n, draft_schema=s2)
    assert not registry.draft_confirmed(n)


def test_etf_versions_freeze_deploy_lock_and_permissions(env):
    from aiquant import challenger, console
    from aiquant.paper import etf_trend, etf_versions
    cfg0 = dict(db.get_setting("etf_trend_config") or {})
    try:
        cur = etf_versions.running()
        assert cur["sma"] == int(etf_trend.get_config()["sma"])
        with pytest.raises(PermissionError):
            etf_versions.freeze(160, 0.03, confirmed=True, actor="g", role="viewer")   # 观察员只读；交易员可改
        with pytest.raises(ValueError):
            etf_versions.freeze(160, 0.03, confirmed=False, actor="boss", role="admin")
        with pytest.raises(ValueError):
            etf_versions.freeze(5, 0.03, confirmed=True, actor="boss", role="admin")
        v = etf_versions.freeze(160, 0.03, confirmed=True, actor="boss", role="admin")
        assert etf_versions.freeze(160, 0.03, confirmed=True, actor="boss", role="admin")["id"] == v["id"]
        assert etf_trend.get_config()["sma"] == cur["sma"]      # 冻结不等于部署
        with pytest.raises(ValueError):
            etf_versions.deploy(v["id"], confirmed=False, actor="boss", role="admin")
        with pytest.raises(PermissionError):
            etf_versions.deploy(v["id"], confirmed=True, actor="t", role="viewer")
        res = etf_versions.deploy(v["id"], confirmed=True, actor="boss", role="admin")
        assert res["result"] == "已部署"
        c = etf_trend.get_config()
        assert c["sma"] == 160 and abs(c["band"] - 0.03) < 1e-9
        assert etf_versions.running()["id"] == v["id"] and etf_versions.lock_state()
        assert "锁定" in (challenger._etf_block() or "")
        assert any(x["action"] == "部署此版本" and x["strategy_key"] == "etf" for x in console.audit_log(50))
        challenger._etf_activate({"sma": 170, "band": 0.02}, "测试晋级")
        assert any(x["sma"] == 170 and x["source"] == "challenger" for x in etf_versions.versions())
        n = dict(etf_versions.narrate(160, 0.03))
        assert "160 日均线" in n["入场"] and "3%" in n["过滤条件"]
    finally:
        db.set_setting("etf_trend_config", cfg0)
        db.set_setting("etf_version_lock", None)
