"""中文自然语言 → Strategy Schema 的确定性规则解析器（优先使用已审核规则组件）。

无法识别的片段会原样列出为"待补充"，绝不猜测；只有校验通过的结果才可冻结为版本。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .schema import validate

TF_WORDS = [("5分钟", 5), ("15分钟", 15), ("30分钟", 30), ("1小时", 60), ("4小时", 240),
            ("日线", 1440), ("1天", 1440), ("一小时", 60), ("四小时", 240)]
NUM = r"(\d+(?:\.\d+)?)"

ENTRY_WORDS = r"(买入|做多|开仓|入场|建仓|开多)"
EXIT_WORDS = r"(卖出|平仓|离场|出场|清仓|止盈离场|平多)"
SHORT_WORDS = r"(做空|开空)"
UP_WORDS = r"(上穿|向上突破|向上穿越|金叉|上破)"
DOWN_WORDS = r"(下穿|向下跌破|向下穿越|死叉|下破|反向交叉)"
ABOVE_WORDS = r"(之上|上方|站上|高于|大于|突破|>|超过|上面)"
BELOW_WORDS = r"(之下|下方|跌破|低于|小于|<|下面|跌落)"


@dataclass
class ParseResult:
    schema: dict | None
    status: str                       # ok / incomplete / error
    warnings: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    method: str = "规则解析器（本地，无模型调用）"


def _norm(t: str) -> str:
    t = t.replace("（", "(").replace("）", ")").replace("，", ",").replace("％", "%").replace("：", ":")
    t = t.replace("ＥＭＡ", "EMA").replace("＜", "<").replace("＞", ">")
    t = re.sub(r"(?i)btc|比特币", "", t)
    t = re.sub(r"(?i)\bema", "EMA", t)
    t = re.sub(r"(?i)\bsma", "SMA", t)
    t = re.sub(r"(?i)\brsi", "RSI", t)
    t = re.sub(r"(?i)\bmacd", "MACD", t)
    t = re.sub(r"(?i)\bma(?=\s*\(?\d)", "SMA", t)
    t = re.sub(r"(\d+)\s*(?:日|周期|根)?均线", r"SMA\1", t)
    return t


class _Builder:
    def __init__(self):
        self.indicators: dict[str, dict] = {}
        self.uses_aux = False

    def ind(self, typ: str, params: dict, aux: bool) -> str:
        p = "_".join(f"{v:g}" for v in params.values())
        iid = f"{'aux_' if aux else ''}{typ.lower()}{p}".replace(".", "p")[:32]
        if iid not in self.indicators:
            self.indicators[iid] = {"id": iid, "type": typ, "tf": "aux" if aux else "main", "params": params}
        if aux:
            self.uses_aux = True
        return iid


def _cond(l, op, r, mult=1.0):
    left = {"ref": l}
    right = {"ref": r, "mult": mult} if isinstance(r, str) else {"value": float(r)}
    return {"left": left, "op": op, "right": right}


MA_TOKEN = re.compile(r"(EMA|SMA)\s*\(?\s*(\d+)\s*\)?")


CMP = r"(大于|高于|>|超过|小于|低于|<|跌破|上穿|下穿)"


def _cmp_op(w: str) -> str:
    return {"上穿": "cross_above", "下穿": "cross_below"}.get(w) or ("<" if w in ("小于", "低于", "<", "跌破") else ">")


def _bb_params(s: str) -> tuple[int, float]:
    m = re.search(r"\(\s*(\d+)\s*,\s*" + NUM + r"\s*\)", s)
    return (int(m.group(1)), float(m.group(2))) if m else (20, 2.0)


def _match_bb_extra(s: str, b: _Builder, aux: bool) -> list[dict] | None:
    """布林 %B / 带宽 / 带宽分位 / 收口，以及 N 周期涨幅（ROC）。"""
    per, sd = _bb_params(s)
    m = re.search(r"(?:%B|百分比B|布林位置)\s*(?:\(\s*\d+\s*,\s*" + NUM + r"\s*\))?\s*" + CMP + r"\s*" + NUM, s)
    if m:
        iid = b.ind("BB_PCTB", {"period": per, "std": sd}, aux)
        return [_cond(iid, _cmp_op(m.group(2)), float(m.group(3)))]
    m = re.search(r"带宽分位\s*(?:\(\s*(\d+)\s*\))?\s*" + CMP + r"\s*" + NUM, s)
    if m:
        lb = int(m.group(1)) if m.group(1) else 120
        iid = b.ind("BB_WIDTH_RANK", {"period": per, "std": sd, "lookback": lb}, aux)
        return [_cond(iid, _cmp_op(m.group(2)), float(m.group(3)))]
    m = re.search(r"带宽\s*(?:\(\s*\d+\s*,\s*" + NUM + r"\s*\))?\s*" + CMP + r"\s*" + NUM + r"\s*%?", s)
    if m and "布林" in s:
        iid = b.ind("BB_WIDTH", {"period": per, "std": sd}, aux)
        return [_cond(iid, _cmp_op(m.group(2)), float(m.group(3)))]
    if "布林" in s and "收口" in s:
        m = re.search(r"收口\s*\(\s*(\d+)\s*\)", s)
        recent = int(m.group(1)) if m else 5
        iid = b.ind("BB_SQUEEZE", {"period": per, "std": sd, "lookback": 120, "recent": recent}, aux)
        return [_cond(iid, "<=", 15.0)]
    m = re.search(r"(\d+)\s*(?:根|个|周期|日)?\s*(?:涨幅|涨跌幅|变化率|ROC)\s*" + CMP + r"\s*(-?\d+(?:\.\d+)?)\s*%?", s) or \
        re.search(r"ROC\s*\(\s*(\d+)\s*\)\s*" + CMP + r"\s*(-?\d+(?:\.\d+)?)\s*%?", s)
    if m:
        iid = b.ind("ROC", {"period": int(m.group(1))}, aux)
        return [_cond(iid, _cmp_op(m.group(2)), float(m.group(3)))]
    return None


def _match_sub(s: str, b: _Builder, aux: bool, warnings: list[str]) -> list[dict] | None:
    price = "aux.close" if aux else "close"
    extra = _match_bb_extra(s, b, aux)
    if extra:
        return extra
    # MACD 金叉/死叉
    if "MACD" in s:
        m = re.search(r"MACD\s*\(?\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)?", s)
        f, sl, sg = (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (12, 26, 9)
        a = b.ind("MACD", {"fast": f, "slow": sl, "signal": sg}, aux)
        c = b.ind("MACD_SIGNAL", {"fast": f, "slow": sl, "signal": sg}, aux)
        if re.search(UP_WORDS, s):
            return [_cond(a, "cross_above", c)]
        if re.search(DOWN_WORDS, s):
            return [_cond(a, "cross_below", c)]
    mas = MA_TOKEN.findall(s)
    if len(mas) >= 2:
        a = b.ind(mas[0][0], {"period": int(mas[0][1])}, aux)
        c = b.ind(mas[1][0], {"period": int(mas[1][1])}, aux)
        if re.search(DOWN_WORDS, s):
            return [_cond(a, "cross_below", c)]
        if re.search(UP_WORDS, s) or "突破" in s:
            return [_cond(a, "cross_above", c)]
        if re.search(ABOVE_WORDS, s):
            return [_cond(a, ">", c)]
        if re.search(BELOW_WORDS, s):
            return [_cond(a, "<", c)]
    # RSI
    m = re.search(r"RSI\s*\(?\s*(\d+)?\s*\)?\s*(?:值)?\s*(小于|低于|<|跌破|大于|高于|>|超过|突破|上穿|下穿)\s*" + NUM, s)
    if m:
        per = int(m.group(1)) if m.group(1) else 14
        r = b.ind("RSI", {"period": per}, aux)
        w = m.group(2)
        op = {"上穿": "cross_above", "下穿": "cross_below"}.get(w)
        if op is None:
            op = "<" if w in ("小于", "低于", "<", "跌破") else ">"
        return [_cond(r, op, float(m.group(3)))]
    # 布林带
    if "布林" in s or "BOLL" in s.upper():
        m = re.search(r"\(\s*(\d+)\s*,\s*" + NUM + r"\s*\)", s)
        per, sd = (int(m.group(1)), float(m.group(2))) if m else (20, 2.0)
        band = "BB_LOWER" if "下轨" in s else "BB_UPPER" if "上轨" in s else "BB_MID" if ("中轨" in s or "中线" in s) else None
        if band:
            bid = b.ind(band, {"period": per, "std": sd}, aux)
            if "触及" in s or "触碰" in s:
                if band == "BB_LOWER":
                    return [_cond("aux.low" if aux else "low", "<=", bid)]
                return [_cond("aux.high" if aux else "high", ">=", bid)]
            if re.search(BELOW_WORDS, s):
                return [_cond(price, "<", bid)]
            if re.search(r"(回到|回归|重回)", s) or re.search(ABOVE_WORDS, s):
                return [_cond(price, ">", bid)]
    # 唐奇安 / 前 N 根高低点
    m = re.search(r"(?:前|过去|最近)\s*(\d+)\s*(根|个|日|天|周期)?(?:K线)?(?:的)?(?:最)?(高点|高价|最高价|低点|低价|最低价)", s)
    m2 = re.search(r"(\d+)\s*(根|日|天|周期)?(?:K线)?\s*(新高|新低)", s)
    if m or m2:
        if m:
            n, unit, hl = int(m.group(1)), m.group(2), m.group(3)
        else:
            n, unit, hl = int(m2.group(1)), m2.group(2), m2.group(3)
        if unit in ("日", "天"):
            warnings.append(f"『{n}{unit}』按 {n} 根信号周期K线解释，如需按自然日请在参数中调整。")
        high = "高" in hl
        iid = b.ind("DONCHIAN_HIGH" if high else "DONCHIAN_LOW", {"period": n}, aux)
        if high:
            return [_cond(price, ">", iid)]
        return [_cond(price, "<", iid)]
    # 成交量放大
    m = re.search(r"成交量\s*(?:大于|超过|高于|>)\s*" + NUM + r"\s*倍\s*(?:的)?\s*(\d+)?\s*(?:周期|根)?\s*(?:平均成交量|均量|成交量均线)", s)
    if m:
        per = int(m.group(2)) if m.group(2) else 20
        v = b.ind("VOL_SMA", {"period": per}, aux)
        return [_cond("aux.volume" if aux else "volume", ">", v, float(m.group(1)))]
    # 价格 vs 均线
    if len(mas) == 1:
        a = b.ind(mas[0][0], {"period": int(mas[0][1])}, aux)
        if re.search(DOWN_WORDS, s):
            return [_cond(price, "cross_below", a)]
        if re.search(UP_WORDS, s):
            return [_cond(price, "cross_above", a)]
        if re.search(BELOW_WORDS, s):
            return [_cond(price, "<", a)]
        if re.search(ABOVE_WORDS, s):
            return [_cond(price, ">", a)]
    return None


def _strip_noise(s: str) -> str:
    s = re.sub(ENTRY_WORDS + "|" + EXIT_WORDS, "", s)
    s = re.sub(r"(时|的时候|则|就|时候|判断为?上升趋势|判断趋势|为上升趋势|上升趋势|下降趋势|当|如果|若|确认|信号)", "", s)
    s = re.sub(r"[\s,.;:、]", "", s)
    return s


def _split_clauses(t: str) -> list[str]:
    out, buf, depth = [], [], 0
    for ch in t:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch in ",;。\n；" and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out


def parse(text: str, default_name: str = "自定义策略") -> ParseResult:
    warnings: list[str] = []
    unresolved: list[str] = []
    if not text or not text.strip():
        return ParseResult(None, "error", errors=["策略描述为空"])
    t = _norm(text)
    b = _Builder()

    # 周期识别
    tfs = []
    for w, m in TF_WORDS:
        if re.search(r"(?<![\d.])" + re.escape(w), t) and m not in tfs:
            tfs.append(m)
    main_tf = aux_tf = None
    aux_word = None
    if len(tfs) >= 2:
        tfs_sorted = sorted(tfs)
        main_tf, aux_tf = tfs_sorted[0], tfs_sorted[-1]
        aux_word = [w for w, m in TF_WORDS if m == aux_tf]
    elif len(tfs) == 1:
        main_tf = tfs[0]

    sch: dict = {"name": default_name}
    # 风控类条款（全局抽取）
    m = re.search(r"(?<!移动)(?:固定)?止损\s*(?:为|设为|设置为)?\s*" + NUM + r"\s*%|" + NUM + r"\s*%\s*(?:的)?(?:固定)?止损", t)
    if m:
        sch["stop_loss_pct"] = float(m.group(1) or m.group(2))
    m = re.search(r"(?<!盈利)(?:固定)?止盈\s*(?:为|设为)?\s*" + NUM + r"\s*%|" + NUM + r"\s*%\s*(?:的)?(?:固定)?止盈", t)
    if m:
        sch["take_profit_pct"] = float(m.group(1) or m.group(2))
    m = re.search(r"(?:盈利|浮盈|获利)\s*(?:达到|超过)?\s*" + NUM + r"\s*%\s*(?:后|以后|时)?\s*(?:启动|开启|启用|使用)?\s*(?:移动|追踪|跟踪)止损", t)
    if m:
        act = float(m.group(1))
        m2 = re.search(r"(?:回撤|回落|移动止损距离|跟踪距离)\s*" + NUM + r"\s*%", t)
        if m2:
            trail = float(m2.group(1))
        else:
            trail = 1.5
            warnings.append("未指定移动止损回撤幅度，暂按 1.5% 设定，请在参数设置中确认。")
        sch["trailing"] = {"activate_pct": act, "trail_pct": trail}
    elif re.search(r"(移动|追踪|跟踪)止损", t):
        m2 = re.search(r"(?:回撤|回落)\s*" + NUM + r"\s*%", t)
        if m2:
            sch["trailing"] = {"activate_pct": 0.0, "trail_pct": float(m2.group(1))}
        else:
            unresolved.append("提到移动止损，但缺少启动条件或回撤幅度")
    m = re.search(r"(?:仓位|使用|投入)\s*" + NUM + r"\s*%\s*(?:的)?(?:资金|仓位)?", t)
    if m and "止" not in t[max(0, m.start() - 2):m.start()]:
        sch["alloc_pct"] = float(m.group(1))
    m = re.search(r"持仓\s*(?:超过|满|达到)\s*(\d+)\s*(?:根|个)", t)
    if m:
        sch["max_hold_bars"] = int(m.group(1))

    risk_pat = re.compile(r"[^,;。]*止损[^,;。]*%[^,;。]*|[^,;。]*%[^,;。]*止损[^,;。]*|[^,;。]*止盈[^,;。]*%[^,;。]*"
                          r"|[^,;。]*%[^,;。]*止盈[^,;。]*|[^,;。]*移动止损[^,;。]*|[^,;。]*回撤\s*\d[^,;。]*"
                          r"|仓位[^,;。]*|持仓超过[^,;。]*")
    entry_conds: list[dict] = []
    exit_conds: list[dict] = []
    exit_mode_any = True
    last_cross: dict | None = None

    clauses = [c for c in _split_clauses(t) if c.strip()]
    for clause in clauses:
        clause_core = risk_pat.sub("", clause)
        if not _strip_noise(clause_core):
            continue
        role = None
        if re.search(SHORT_WORDS, clause_core):
            warnings.append("本系统仅支持做多（BTC现货1倍），『做空』信号已按『平仓』处理。")
            role = "exit"
        elif re.search(EXIT_WORDS, clause_core):
            role = "exit"
        elif re.search(ENTRY_WORDS, clause_core):
            role = "entry"
        else:
            role = "filter"
        if last_cross is not None and ("反向交叉" in clause_core
                                       or _strip_noise(clause_core) in ("死叉", "下穿", "反向")):
            rev = dict(last_cross)
            rev["op"] = "cross_below" if last_cross["op"] == "cross_above" else "cross_above"
            exit_conds.append(rev)
            continue
        subs = [x for x in re.split(r"并且|而且|同时|且|或者|或|以及|和", clause_core) if _strip_noise(x)]
        for sub in subs:
            aux = bool(aux_word and any(re.search(r"(?<![\d.])" + re.escape(w), sub) for w in aux_word))
            res = _match_sub(sub, b, aux, warnings)
            if res is None:
                unresolved.append(sub.strip())
                continue
            for c in res:
                if c["op"].startswith("cross") and role != "exit":
                    last_cross = c
                if role == "exit":
                    exit_conds.append(c)
                else:
                    entry_conds.append(c)
            if role == "filter":
                warnings.append(f"『{sub.strip()}』未包含买入/卖出动作，已作为入场过滤条件。")

    if aux_tf:
        sch["aux_tf"] = aux_tf
        sch["signal_tf"] = main_tf
    elif main_tf:
        sch["signal_tf"] = main_tf
    if not b.uses_aux and aux_tf:
        sch.pop("aux_tf", None)
        warnings.append("识别到两个周期但没有条件引用较大周期，已忽略辅助周期。")

    sch["indicators"] = list(b.indicators.values())
    if entry_conds:
        sch["entry"] = {"mode": "all", "conditions": entry_conds[:8]}
    if exit_conds:
        sch["exit"] = {"mode": "any" if exit_mode_any else "all", "conditions": exit_conds[:8]}

    errors = []
    if not entry_conds:
        errors.append("未能识别入场规则（例如：EMA20上穿EMA60时买入）")
    if errors:
        return ParseResult(sch, "incomplete", warnings, unresolved, errors)
    obj, errs = validate(sch)
    if errs:
        return ParseResult(sch, "error", warnings, unresolved, errs)
    status = "incomplete" if unresolved else "ok"
    if unresolved:
        errors.append("存在无法识别的规则片段，请改写或在参数设置中补充后再冻结。")
    return ParseResult(obj.model_dump(), status, warnings, unresolved, errors)


# ---------------------------------------------------------------------------
# 「支持的写法」速查表（从指标白名单生成）与“最接近的可识别写法”建议
# ---------------------------------------------------------------------------

# 每种白名单指标的中文写法示例：(示例, 说明)。速查表按 INDICATOR_TYPES 逐个生成；没有写法的指标会标注“仅参数设置”。
SYNTAX: dict[str, list[tuple[str, str]]] = {
    "EMA": [("EMA20上穿EMA60时买入", "两条指数均线金叉 / 死叉（下穿）"), ("收盘价站上EMA50", "价格在均线之上")],
    "SMA": [("SMA10上穿SMA30时买入", "两条简单均线交叉；也可写“20日均线”"), ("收盘价跌破20日均线时卖出", "价格跌破均线")],
    "RSI": [("RSI(14)小于30时买入", "超卖；也可写“大于70”“上穿50”")],
    "BB_UPPER": [("收盘价突破布林上轨(20,2)时买入", "括号里是周期和标准差倍数，可省略（默认 20,2）")],
    "BB_MID": [("收盘价跌破布林中轨时卖出", "常用作趋势策略的离场条件")],
    "BB_LOWER": [("最低价触及布林下轨时买入", "“触及”= 当根最低价碰到下轨")],
    "BB_PCTB": [("%B大于50", "价格在布林带里的位置：0 = 下轨，100 = 上轨")],
    "BB_WIDTH": [("布林带宽小于5", "带宽 = (上轨 − 下轨) ÷ 中轨 × 100")],
    "BB_WIDTH_RANK": [("带宽分位小于15", "带宽在最近 120 根里的百分位；“带宽分位(200)小于10”可改回看根数")],
    "BB_SQUEEZE": [("布林带收口时买入", "最近 5 根里带宽分位最低值 ≤ 15（刚收口）；“收口(10)”可改最近根数")],
    "DONCHIAN_HIGH": [("突破前20根K线最高价时买入", "唐奇安通道突破；也可写“20日新高”")],
    "DONCHIAN_LOW": [("跌破前10根K线最低价时卖出", "跌破前 N 根最低价")],
    "ROC": [("10周期涨幅大于2%", "N 根 K 线的涨跌幅；也可写“ROC(10)大于2”")],
    "VOL_SMA": [("成交量大于1.5倍20周期平均成交量", "放量确认")],
    "MACD": [("MACD金叉时买入", "MACD 上穿信号线；“MACD(12,26,9)死叉时卖出”")],
    "MACD_SIGNAL": [("MACD死叉时卖出", "与 MACD 一起使用（金叉 / 死叉）")],
}
GENERAL_SYNTAX = [
    ("周期", "用4小时K线", "信号周期：5分钟 / 15分钟 / 30分钟 / 1小时 / 4小时 / 日线"),
    ("周期", "日线收盘价站上SMA50", "写两个周期时，较大的那个作为辅助周期，用来看大方向"),
    ("止损", "固定止损2%", "买入后亏损达到 2% 卖出"),
    ("止盈", "止盈8%", "盈利达到 8% 卖出"),
    ("移动止损", "盈利3%后启动移动止损，回撤1.5%离场", "先保护利润，再让利润奔跑"),
    ("仓位", "仓位50%", "每次使用可用资金的比例（只做多，不加杠杆）"),
    ("最长持仓", "持仓超过48根K线平仓", "到时间就卖出"),
    ("组合条件", "EMA20上穿EMA60且RSI(14)大于50时买入", "“且”= 同时满足；“或”= 任一满足"),
    ("离场", "反向交叉时平仓", "沿用上面的交叉条件，反方向时卖出"),
]


def cheatsheet() -> list[dict]:
    """速查表：先列通用写法，再按白名单逐个指标列出写法示例。"""
    from .schema import INDICATOR_CN, INDICATOR_TYPES
    rows = [{"类别": c, "写法": w, "说明": d, "指标": ""} for c, w, d in GENERAL_SYNTAX]
    for t in INDICATOR_TYPES:
        ex = SYNTAX.get(t)
        if not ex:
            rows.append({"类别": INDICATOR_CN.get(t, t), "写法": "（暂无中文写法）",
                         "说明": "可在「参数设置」或 DeepSeek 解析中使用", "指标": t})
            continue
        for w, d in ex:
            rows.append({"类别": INDICATOR_CN.get(t, t), "写法": w, "说明": d, "指标": t})
    return rows


def _recognized(fragment: str) -> bool:
    try:
        return _match_sub(_norm(fragment), _Builder(), False, []) is not None
    except Exception:  # noqa: BLE001
        return False


def suggest(fragment: str, k: int = 3) -> list[str]:
    """给一段识别不了的文字，返回最多 k 条“最接近的可识别写法”（每条都保证解析器能识别）。"""
    import difflib
    f = _norm(fragment or "")
    nums = [x for x in re.findall(NUM, f)]
    n1 = nums[0] if nums else None
    n2 = nums[1] if len(nums) > 1 else None
    role = ""
    if re.search(EXIT_WORDS, f):
        role = "卖出"
    elif re.search(ENTRY_WORDS, f):
        role = "买入"
    cands: list[str] = []
    up = bool(re.search(UP_WORDS + "|突破|站上|上方|之上|大于|高于", f))
    down = bool(re.search(DOWN_WORDS + "|跌破|下方|之下|小于|低于", f))
    if "MACD" in f:
        cands.append("MACD死叉" if ("死叉" in f or "绿柱" in f or down) else "MACD金叉")
    if re.search(r"均线|EMA|SMA|(?<![A-Z])MA(?!CD)|均价", f):
        a, c = (n1 or "20"), (n2 or "60")
        if n2 or re.search(UP_WORDS + "|" + DOWN_WORDS, f):
            cands += [f"EMA{a}{'下穿' if down and not up else '上穿'}EMA{c}"]
        cands += [f"收盘价{'跌破' if down and not up else '站上'}SMA{a}", f"EMA{a}上穿EMA{c}"]
    if "RSI" in f or "超买" in f or "超卖" in f or "强弱" in f:
        v = n2 or (n1 if n1 and float(n1) > 5 else None)
        if "超买" in f:
            cands.append("RSI(14)大于70")
        cands += [f"RSI(14){'大于' if up and not down else '小于'}{v or 30}", "RSI(14)上穿50"]
    if re.search(r"布林|BOLL|上轨|下轨|中轨|中线|%B|带宽|收口", f, re.I):
        if "%B" in f or "位置" in f:
            cands.append(f"%B{'小于' if down and not up else '大于'}{n1 or 50}")
        if "分位" in f:
            cands.append(f"带宽分位小于{n1 or 15}")
        elif "带宽" in f:
            cands.append(f"布林带宽{'大于' if up and not down else '小于'}{n1 or 5}")
        if "收口" in f or "收窄" in f:
            cands.append("布林带收口")
        band = "下轨" if "下轨" in f else "上轨" if "上轨" in f else "中轨"
        cands.append(f"收盘价{'跌破' if (down and not up) or band == '下轨' else '突破'}布林{band}")
    if "MACD" in f or "金叉" in f or "死叉" in f:
        cands.append("MACD死叉" if ("死叉" in f or down) else "MACD金叉")
    if re.search(r"新高|高点|最高|新低|低点|最低|通道", f):
        low = re.search(r"新低|低点|最低", f)
        cands.append(f"{'跌破' if low else '突破'}前{n1 or 20}根K线{'最低价' if low else '最高价'}")
    if re.search(r"成交量|放量|量能|volume", f, re.I):
        cands.append(f"成交量大于{n1 if n1 and float(n1) < 10 else 1.5}倍20周期平均成交量")
    if re.search(r"涨幅|涨跌幅|动量|变化率|ROC|涨了|上涨", f, re.I):
        cands.append(f"{n2 or 10}周期涨幅大于{n1 or 2}%")
    examples = [w for ex in SYNTAX.values() for w, _ in ex]
    cores = [re.sub(r"时(买入|卖出|平仓)$", "", w) for w in examples]
    cands += difflib.get_close_matches(f, cores, n=6, cutoff=0.0)
    out: list[str] = []
    for c in cands:
        c = re.sub(r"时(买入|卖出|平仓)$", "", c)
        if role == "买入" and not down and re.search(r"跌破|下穿|死叉|小于", c) and not re.search(r"RSI|%B|带宽", c):
            continue          # 方向和原文相反的写法不推荐
        if role == "卖出" and not up and re.search(r"突破|站上|上穿|金叉", c):
            continue
        full = f"{c}时{role}" if role else c
        if full not in out and _recognized(c):
            out.append(full)
        if len(out) >= k:
            break
    return out
