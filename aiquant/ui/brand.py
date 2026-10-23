"""品牌：Logo（K 线 × 进化螺旋）与口号。素材在 assets/ 目录。"""
from __future__ import annotations

import functools
import itertools
from pathlib import Path

from ..i18n import pick

ASSETS = Path(__file__).resolve().parents[2] / "assets"   # 随代码分发（不依赖数据目录）
FAVICON = ASSETS / "favicon-64.png"
_ids = itertools.count()

NAME = "AI Quant Strategy Lab"
SLOGAN_ZH, SLOGAN_EN = "让策略自己进化", "Let your strategy evolve."
PILLARS_ZH, PILLARS_EN = "严格回测 · 自动进化 · 模拟盘验证", "Rigorous backtests · Self-evolving · Paper-proven"


def slogan() -> str:
    return pick(SLOGAN_ZH, SLOGAN_EN)


def pillars() -> str:
    return pick(PILLARS_ZH, PILLARS_EN)


@functools.lru_cache(maxsize=1)
def _mark_src() -> str:
    try:
        return (ASSETS / "logo-mark.svg").read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="16" '
                'fill="#4F46E5"/></svg>')


def mark_svg(size: int = 36, cls: str = "aq-mark") -> str:
    """内联 SVG Logo（每次生成独立的渐变 id，避免同一页面多个 Logo 互相影响）。"""
    gid = f"aqg{next(_ids)}"
    svg = _mark_src().replace('id="aqg"', f'id="{gid}"').replace("url(#aqg)", f"url(#{gid})")
    svg = " ".join(line.strip() for line in svg.splitlines())   # 单行：避免 Markdown 把缩进当成代码块
    return svg.replace("<svg ", f'<svg class="{cls}" width="{size}" height="{size}" ', 1)


def favicon():
    return str(FAVICON) if FAVICON.exists() else "📈"
