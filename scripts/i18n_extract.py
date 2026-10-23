"""开发工具：从源码提取界面中文片段，列出 aiquant/i18n_en.json 里还没有英文的部分。

用法：python scripts/i18n_extract.py            → 打印缺失片段数量
      python scripts/i18n_extract.py --dump F  → 把缺失片段（JSON 列表）写入 F
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HAS = re.compile(r"[\u3400-\u9fff]")
PH = re.compile(r"\{[^{}]*\}|%[-+ 0#,]*\d*(?:\.\d+)?[sdfr%]")
SQL = re.compile(r"^(SELECT|UPDATE|INSERT|DELETE|CREATE)\b")
REGEXY = re.compile(r"\\|\(\?|\[\^|\)\?|\([^)]*\|[^)]*\)")
SKIP_FILES = {"aiquant/strategy/parser.py"}            # 中文自然语言解析规则（正则），不是界面文字
PROMPT_FILES = {"aiquant/ai/deepseek.py", "aiquant/ai/optimizer.py"}   # 只取短的提示信息，跳过模型提示词


def fragments() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    files = [ROOT / "app.py", *sorted((ROOT / "aiquant").rglob("*.py"))]
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        if rel in SKIP_FILES:
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        docs = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.Expr) and \
                    isinstance(getattr(body[0], "value", None), ast.Constant):
                docs.add(id(body[0].value))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)) or id(node) in docs:
                continue
            for piece in PH.split(node.value):
                for line in piece.split("\n"):
                    p = line.strip()
                    if not HAS.search(p) or REGEXY.search(p) or SQL.match(p):
                        continue
                    if rel in PROMPT_FILES and len(p) > 45:
                        continue
                    out.setdefault(p, set()).add(rel)
    return out


def main() -> None:
    fr = fragments()
    dic_path = ROOT / "aiquant" / "i18n_en.json"
    have = json.loads(dic_path.read_text(encoding="utf-8")) if dic_path.exists() else {}
    have.update(json.loads((ROOT / "aiquant" / "i18n_skip.json").read_text(encoding="utf-8"))
                if (ROOT / "aiquant" / "i18n_skip.json").exists() else {})
    missing = sorted(k for k in fr if k not in have)
    print(f"fragments={len(fr)} translated={len(fr) - len(missing)} missing={len(missing)}")
    if "--dump" in sys.argv:
        Path(sys.argv[sys.argv.index("--dump") + 1]).write_text(
            json.dumps(missing, ensure_ascii=False, indent=0), encoding="utf-8")


if __name__ == "__main__":
    main()
