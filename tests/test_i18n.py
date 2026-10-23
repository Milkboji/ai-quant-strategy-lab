"""中英文切换：词典覆盖、动态句子翻译、HTML、保持原文、语言判断。"""
import json
import subprocess
import sys
from pathlib import Path

from aiquant import i18n

ROOT = Path(__file__).resolve().parent.parent


def test_dictionary_covers_all_ui_strings():
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "i18n_extract.py")], capture_output=True, text=True,
                         check=True).stdout
    assert "missing=0" in out, out


def test_dictionary_is_valid():
    d = json.loads((ROOT / "aiquant" / "i18n_en.json").read_text(encoding="utf-8"))
    assert len(d) > 1500
    assert all(isinstance(v, str) for v in d.values())
    bad = [v for v in d.values() if i18n._CJK.search(v)]
    assert not bad, bad[:5]


def test_dynamic_sentences_and_html():
    t = i18n.translate
    assert t("运行中的策略") == "Running strategies"
    assert t("已成交 4 笔") == "Filled 4 trades"
    assert t("策略位1·策略1 v3 自动进化：已切换") == "Slot 1·Strategy 1 v3 Auto-evolution: switched"
    assert t('<div class="aq-section"><span class="bar"></span>系统设置</div>') == \
        '<div class="aq-section"><span class="bar"></span>System Settings</div>'
    h = t('<span class="aq-pill green">已解析</span> <span title="无">无</span>')
    assert "Parsed" in h and 'title="None"' in h and "<span" in h
    assert t("美股 ETF 趋势择时：QQQ 150 日均线 → QLD（2 倍纳指）") == \
        "US ETF trend timing: QQQ 150-day MA → QLD (2x Nasdaq)"


def test_untranslatable_text_kept_original():
    long_ai = "这个策略在上涨趋势里买入，这是模型写的一段很长的分析文字，大部分词语在词典里都没有对应翻译内容。"
    assert i18n.translate(long_ai) == long_ai
    raw = i18n.keep("日线EMA9上穿EMA21时买入")
    assert i18n.translate(raw) == raw
    assert i18n.translate("BTC/USD 2026") != "" and i18n.translate("abc") == "abc"


def test_language_detection():
    assert i18n.detect("en", "zh", "zh-CN") == "en"
    assert i18n.detect(None, "en", "zh-CN") == "en"
    assert i18n.detect(None, None, "zh-CN,zh;q=0.9") == "zh"
    assert i18n.detect(None, None, "en-US,en;q=0.9") == "en"
    assert i18n.detect(None, None, None) == "zh"
    assert i18n.tr("运行中的策略") == "运行中的策略"      # 没有 Streamlit 会话时默认中文、不翻译
