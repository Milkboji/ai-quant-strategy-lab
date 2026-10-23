"""自动进化会尝试布林带用法（策略里没有布林指标时），已有布林指标的策略则调参。"""
from aiquant import evolve
from aiquant.strategy.schema import validate
from aiquant.strategy.templates import TEMPLATES


def _muts(name, tf):
    sch = dict(TEMPLATES[name]["schema"]); sch["signal_tf"] = tf
    o, _ = validate(sch)
    return evolve.mutations(o.model_dump())


def test_bollinger_candidates_added(env):
    whys = [w for _, w in _muts("趋势跟踪", 240)]
    assert any("布林离场" in w for w in whys) and any("布林入场过滤" in w for w in whys)
    for sch, why in _muts("趋势跟踪", 240):
        if "布林" in why:
            assert not validate(sch)[1]


def test_short_timeframe_no_bb_exit(env):
    whys = [w for _, w in _muts("趋势跟踪", 15)]
    assert not any("布林离场" in w for w in whys) and any("布林入场过滤" in w for w in whys)


def test_existing_bollinger_is_tuned_not_duplicated(env):
    whys = [w for _, w in _muts("均值回归", 240)]
    assert not any("增加布林" in w for w in whys)
    assert any(w.startswith("bb_") for w in whys)
