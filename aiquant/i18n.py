"""中英文切换（Chinese / English）。

做法：界面代码继续用中文书写；切到英文时，在 Streamlit 显示层统一翻译。
- 词典：aiquant/i18n_en.json（中文片段 → 英文）。由 scripts/i18n_extract.py 从源码中的中文字符串提取。
- 翻译只改"显示"，不改控件返回值：下拉 / 单选 / 分段控件通过 format_func 翻译显示文字，返回值仍是原来的中文选项，
  业务代码里的比较、数据库里存的值都不受影响。输入框里用户写的内容（value）不翻译。
- 动态文字（f-string 拼出来的句子、数据库里的日志）按"最长片段优先"逐段替换；如果一段文字大部分都不在词典里
  （例如 DeepSeek 生成的中文分析），保持原文，不做半中半英的拼接。
- 语言选择：网址参数 ?lang=en / zh → Cookie（aq_lang）→ 浏览器语言（中文浏览器默认中文，其余默认英文）。
"""
from __future__ import annotations

import functools
import json
import re
import threading
from pathlib import Path

LANGS = {"zh": "中文", "en": "English"}
COOKIE = "aq_lang"
_DICT_FILE = Path(__file__).with_name("i18n_en.json")
_CJK = re.compile(r"[㐀-鿿]")
_PUNCT = [("，", ", "), ("。", ". "), ("：", ": "), ("；", "; "), ("！", "! "), ("？", "? "), ("、", ", "),
          ("（", " ("), ("）", ") "), ("【", "["), ("】", "]"), ("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'"),
          ("《", '"'), ("》", '"'), ("「", '"'), ("」", '"'), ("『", '"'), ("』", '"'), ("～", "~"), ("％", "%"),
          ("＋", "+"), ("－", "-"), ("　", " "), ("…", "..."), ("—", " - ")]
_lock = threading.Lock()
_D: dict[str, str] | None = None
_FIRST: dict[str, int] = {}
MISSING: set[str] = set()


def _load() -> dict[str, str]:
    global _D
    if _D is None:
        with _lock:
            if _D is None:
                try:
                    d = json.loads(_DICT_FILE.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    d = {}
                d = {k: v for k, v in d.items() if k and isinstance(v, str)}
                d.update({k: v for k, v in _derive(d).items() if k not in d})
                for k in d:
                    _FIRST[k[0]] = max(_FIRST.get(k[0], 0), len(k))
                _D = d
    return _D


_TAG = re.compile(r'<[^>]*>|^[^<>]*">|^>')


def _derive(d: dict[str, str]) -> dict[str, str]:
    """从含 HTML 的词条里拆出纯文字词条（中英文标签结构一致时按顺序配对），让拼接出来的 HTML 也能翻译。"""
    out: dict[str, str] = {}
    for k, v in d.items():
        if "<" not in k and ">" not in k:
            continue
        tk, tv = _TAG.findall(k), _TAG.findall(v)
        nk = [x.strip() for x in _TAG.split(k)]
        nv = [x.strip() for x in _TAG.split(v)]
        if len(tk) != len(tv) or len(nk) != len(nv):
            continue
        for a, b in zip(nk, nv):
            if a and _CJK.search(a) and b and a not in out:
                out[a] = b
    return out


# ---------------------------------------------------------------------------
# 当前语言
# ---------------------------------------------------------------------------

def _ctx_ok() -> bool:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx(suppress_warning=True) is not None
    except Exception:  # noqa: BLE001
        return False


def lang() -> str:
    if not _ctx_ok():
        return "zh"
    try:
        import streamlit as st
        return st.session_state.get("lang", "zh")
    except Exception:  # noqa: BLE001
        return "zh"


def is_en() -> bool:
    return lang() == "en"


def pick(zh: str, en: str) -> str:
    """直接给出两种语言的文字（用于品牌口号等需要精心措辞的地方）。"""
    return en if is_en() else zh


def detect(query_lang: str | None, cookie_lang: str | None, accept_language: str | None) -> str:
    for v in (query_lang, cookie_lang):
        if v in LANGS:
            return v
    al = (accept_language or "").lower()
    if not al or al.startswith("zh") or "zh" in al.split(",")[0]:
        return "zh"
    return "en"


# ---------------------------------------------------------------------------
# 翻译
# ---------------------------------------------------------------------------

def _post(s: str) -> str:
    for a, b in _PUNCT:
        s = s.replace(a, b)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r" +([,;:!?)])", r"\1", s)
    s = re.sub(r" +\.(?=\s|$|<)", ".", s)
    s = re.sub(r"(?<=[A-Za-z)]):(?=[A-Za-z0-9(\"])", ": ", s)
    s = re.sub(r"(?<=[A-Za-z0-9%])\((?=[A-Za-z0-9])", " (", s)
    s = re.sub(r"(\d) ×", r"\1×", s)
    s = re.sub(r"(\d) -(day|year|period|candle|trade|bar)", r"\1-\2", s)
    s = re.sub(r"\( +", "(", s)
    return s


class Raw(str):
    """标记为"保持原文"的字符串（例如用户自己写的中文策略描述）。"""


def keep(s: str) -> str:
    return Raw(s) if isinstance(s, str) else s


def translate(s: str) -> str:
    """中文 → 英文（与当前语言无关）。"""
    if not s or isinstance(s, Raw) or not _CJK.search(s):
        return s
    return _translate(str(s))


_SPLIT = re.compile(r"(<[^>]*>)")
_TITLE = re.compile(r'(title=")([^"]*)(")')


@functools.lru_cache(maxsize=20000)
def _translate(s: str) -> str:
    d = _load()
    core = s.strip()
    if core not in d and "<" in s and ">" in s:
        # HTML：逐个文字节点翻译（每段单独判断），标签原样保留，title 提示文字也翻译
        out = []
        for part in _SPLIT.split(s):
            if part.startswith("<") and part.endswith(">"):
                out.append(_TITLE.sub(lambda m: m.group(1) + _text(m.group(2)) + m.group(3), part)
                           if "title=" in part and _CJK.search(part) else part)
            else:
                out.append(_text(part))
        return "".join(out)
    return _text(s)


@functools.lru_cache(maxsize=20000)
def _text(s: str) -> str:
    if not s or not _CJK.search(s):
        return s
    d = _load()
    core = s.strip()
    if core in d:
        lead = s[:len(s) - len(s.lstrip())]
        tail = s[len(s.rstrip()):]
        return lead + _post(d[core]).strip() + tail
    out: list[str] = []
    i, n = 0, len(s)
    total = len(_CJK.findall(s))
    covered = 0
    while i < n:
        ch = s[i]
        m = _FIRST.get(ch, 0)
        hit = None
        for L in range(min(m, n - i), 0, -1):
            seg = s[i:i + L]
            if seg in d:
                hit = seg
                break
        if hit is None:
            out.append(ch)
            i += 1
            continue
        rep = d[hit]
        prev = out[-1][-1:] if out and out[-1] else ""
        nxt = s[i + len(hit):i + len(hit) + 1]
        if rep and prev and (prev.isalnum() or prev in ")%") and rep[0].isalnum():
            rep = " " + rep
        if rep and nxt and nxt.isascii() and nxt.isalnum() and rep[-1].isalnum():
            rep = rep + " "
        out.append(rep)
        covered += len(_CJK.findall(hit))
        i += len(hit)
    if total and covered / total < 0.5:     # 大部分不认识（多半是模型生成的中文长文）→ 保持原文
        _miss(core)
        return s
    res = "".join(out)
    if _CJK.search(res):
        _miss(core)
    return _post(res)


def _miss(core: str) -> None:
    key = core[:160]
    if key in MISSING:
        return
    MISSING.add(key)
    import os
    if os.environ.get("AIQUANT_I18N_DEBUG") == "1":
        try:
            from . import config
            with open(config.LOG_DIR / "i18n_missing.txt", "a", encoding="utf-8") as f:
                f.write(key.replace("\n", " ") + "\n")
        except Exception:  # noqa: BLE001
            pass


def tr(s):
    """按当前语言翻译；非字符串原样返回。"""
    if isinstance(s, str) and s and is_en():
        return translate(s)
    return s


def tr_obj(o):
    """翻译列表 / 字典里的字符串（用于 tabs、captions 等）。"""
    if not is_en():
        return o
    if isinstance(o, str):
        return translate(o)
    if isinstance(o, (list, tuple)):
        return type(o)(tr_obj(x) for x in o)
    return o


def tr_df(data):
    """表格：翻译列名和文字单元格（不改原对象）。"""
    if not is_en() or data is None:
        return data
    try:
        import pandas as pd
    except Exception:  # noqa: BLE001
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        data = pd.DataFrame(data)
    if isinstance(data, pd.DataFrame):
        df = data.copy()
        df.columns = [translate(c) if isinstance(c, str) else c for c in df.columns]
        if len(df) <= 5000:
            for c in df.columns:
                if df[c].dtype == object or str(df[c].dtype).startswith(("string", "str")):
                    df[c] = df[c].map(lambda v: translate(v) if isinstance(v, str) else v)
        if isinstance(df.index, pd.Index) and df.index.dtype == object:
            df.index = [translate(v) if isinstance(v, str) else v for v in df.index]
        return df
    if isinstance(data, pd.Series):
        s = data.map(lambda v: translate(v) if isinstance(v, str) else v)
        if isinstance(s.name, str):
            s.name = translate(s.name)
        return s
    return data


_FIG_SKIP = {"customdata", "ids", "uid", "meta", "legendgroup", "type", "mode", "xaxis", "yaxis", "hoverinfo",
             "xref", "yref", "color", "colorscale", "family", "fill"}


def _walk(o, key=""):
    if isinstance(o, str):
        return translate(o) if key not in _FIG_SKIP else o
    if isinstance(o, dict):
        return {k: (_walk(v, k)) for k, v in o.items()}
    if isinstance(o, list):
        return [_walk(v, key) for v in o]
    return o


def tr_fig(fig):
    if not is_en() or fig is None:
        return fig
    try:
        import plotly.graph_objects as go
        if isinstance(fig, go.Figure):
            return go.Figure(_walk(fig.to_plotly_json()))
    except Exception:  # noqa: BLE001
        pass
    return fig


# ---------------------------------------------------------------------------
# Streamlit 显示层补丁
# ---------------------------------------------------------------------------

_TEXT_KW = ("label", "help", "placeholder", "text", "title", "body", "caption")
_EXEMPT_KEYS = ("lang_",)
_OPTION_WIDGETS = ("radio", "selectbox", "multiselect", "select_slider", "segmented_control", "pills")
_TEXT0 = ("markdown", "caption", "title", "header", "subheader", "text", "info", "success", "warning", "error",
          "toast", "button", "form_submit_button", "download_button", "link_button", "checkbox", "toggle",
          "text_input", "number_input", "text_area", "date_input", "time_input", "slider", "expander", "popover",
          "color_picker", "file_uploader", "spinner", "status", "chat_input") + _OPTION_WIDGETS


def _exempt(kw) -> bool:
    k = kw.get("key")
    return isinstance(k, str) and k.startswith(_EXEMPT_KEYS)


def _wrap(name: str, fn):
    if getattr(fn, "_aq_i18n", False):
        return fn

    def w(*a, **kw):
        if not is_en() or _exempt(kw):
            return fn(*a, **kw)
        # 第一个位置参数是 DeltaGenerator（绑定前的类方法）时跳过它
        off = 1 if a and a[0].__class__.__name__ == "DeltaGenerator" else 0
        a = list(a)
        if name in _TEXT0 and len(a) > off and isinstance(a[off], str):
            a[off] = translate(a[off])
        for k in _TEXT_KW:
            if isinstance(kw.get(k), str):
                kw[k] = translate(kw[k])
        if name in _OPTION_WIDGETS:
            ff = kw.get("format_func") or str
            kw["format_func"] = lambda o, _ff=ff: translate(str(_ff(o)))
            if "captions" in kw and kw["captions"]:
                kw["captions"] = [translate(c) if isinstance(c, str) else c for c in kw["captions"]]
        elif name == "metric":
            for i in range(off, min(off + 3, len(a))):
                if isinstance(a[i], str):
                    a[i] = translate(a[i])
            for k in ("value", "delta"):
                if isinstance(kw.get(k), str):
                    kw[k] = translate(kw[k])
        elif name == "tabs" and len(a) > off:
            a[off] = [translate(t) if isinstance(t, str) else t for t in a[off]]
        elif name in ("dataframe", "table"):
            if len(a) > off:
                a[off] = tr_df(a[off])
            elif "data" in kw:
                kw["data"] = tr_df(kw["data"])
            cc = kw.get("column_config")
            if isinstance(cc, dict):
                kw["column_config"] = {(translate(k) if isinstance(k, str) else k): v for k, v in cc.items()}
            if isinstance(kw.get("column_order"), (list, tuple)):
                kw["column_order"] = [translate(c) if isinstance(c, str) else c for c in kw["column_order"]]
        elif name == "plotly_chart":
            if len(a) > off:
                a[off] = tr_fig(a[off])
            elif "figure_or_data" in kw:
                kw["figure_or_data"] = tr_fig(kw["figure_or_data"])
        elif name == "write":
            a = [translate(x) if isinstance(x, str) else x for x in a]
        elif name == "progress":
            if len(a) > off + 1 and isinstance(a[off + 1], str):
                a[off + 1] = translate(a[off + 1])
        elif name == "page_link":
            pass
        return fn(*a, **kw)

    functools.update_wrapper(w, fn)
    w._aq_i18n = True
    return w


_installed = False


def install() -> None:
    """给 Streamlit 的显示函数加上翻译（幂等）。"""
    global _installed
    if _installed:
        return
    import streamlit as st
    from streamlit.delta_generator import DeltaGenerator as DG
    names = _TEXT0 + ("metric", "tabs", "dataframe", "table", "plotly_chart", "write", "progress", "page_link")
    for nm in names:
        if hasattr(DG, nm):
            setattr(DG, nm, _wrap(nm, getattr(DG, nm)))
        if hasattr(st, nm):
            setattr(st, nm, _wrap(nm, getattr(st, nm)))

    orig_page = st.Page

    def Page(*a, **kw):   # noqa: N802
        if is_en() and isinstance(kw.get("title"), str):
            kw["title"] = translate(kw["title"])
        return orig_page(*a, **kw)
    st.Page = Page

    orig_nav = st.navigation

    def navigation(pages, *a, **kw):
        if is_en() and isinstance(pages, dict):
            pages = {(translate(k) if isinstance(k, str) else k): v for k, v in pages.items()}
        return orig_nav(pages, *a, **kw)
    st.navigation = navigation

    orig_dialog = st.dialog

    def dialog(title="", *da, **dkw):
        def deco(fn):
            @functools.wraps(fn)
            def run(*a, **k):
                return orig_dialog(tr(title), *da, **dkw)(fn)(*a, **k)
            return run
        return deco
    st.dialog = dialog
    _installed = True


# ---------------------------------------------------------------------------
# 语言开关
# ---------------------------------------------------------------------------

def init_from_request() -> str:
    """每次页面运行时确定语言（网址参数优先，其次 Cookie，再次浏览器语言）。"""
    import streamlit as st
    q = None
    try:
        q = st.query_params.get("lang")
    except Exception:  # noqa: BLE001
        pass
    if q in LANGS:
        st.session_state["lang"] = q
    elif "lang" not in st.session_state:
        try:
            ck = dict(st.context.cookies).get(COOKIE)
            al = dict(st.context.headers).get("Accept-Language") or dict(st.context.headers).get("accept-language")
        except Exception:  # noqa: BLE001
            ck = al = None
        st.session_state["lang"] = detect(None, ck, al)
    return st.session_state["lang"]


def switcher(key: str = "lang_switch") -> None:
    """语言切换按钮：中文 / EN。"""
    import streamlit as st
    import streamlit.components.v1 as components
    cur = st.session_state.get("lang", "zh")
    choice = st.segmented_control("Language", ["zh", "en"], default=cur, key=key, label_visibility="collapsed",
                                  format_func=lambda x: "中文" if x == "zh" else "EN")
    if st.session_state.pop("_lang_set_cookie", None):
        components.html(f"<script>window.parent.document.cookie='{COOKIE}={cur}; path=/; max-age=31536000; "
                        "samesite=lax';</script>", height=0)
    if choice and choice != cur:
        st.session_state["lang"] = choice
        st.session_state["_lang_set_cookie"] = True
        try:
            st.query_params["lang"] = choice
        except Exception:  # noqa: BLE001
            pass
        st.rerun()
