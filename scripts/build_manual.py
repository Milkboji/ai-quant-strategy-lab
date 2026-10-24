"""从在线文档导出的 Markdown 重新生成《使用说明》PDF 和 Word。

在线文档（Claude Docs）导出时图片会被替换成「[image: 标题]」占位符，这里按 docs/manual/images.json 把标题换回
docs/manual/img/ 里的截图；Mermaid 图用 mmdc 渲染成 PNG（没有 mmdc 时保留代码块）。
PDF：pandoc 生成 HTML → Playwright（Chromium）打印 A4；Word：pandoc 直接生成 .docx。

用法：python scripts/build_manual.py 导出的.md [输出目录，默认 docs/] [--rev N]
输出：AI_Quant_Strategy_Lab_使用说明.pdf / .docx，并把文档版本写入 docs/manual/manual_rev.json。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAN = ROOT / "docs" / "manual"
NAME = "AI_Quant_Strategy_Lab_使用说明"
CSS = """
@page{size:A4;margin:16mm 15mm 18mm}
body{font-family:'Noto Sans CJK SC','Microsoft YaHei',sans-serif;color:#0F172A;font-size:10.5pt;line-height:1.7}
h1{font-size:24pt;margin:0 0 4px;color:#1E1B4B}
h1+p{color:#64748B;margin-top:0}
h2{font-size:16pt;color:#4C1D95;border-bottom:2px solid #DDD6FE;padding-bottom:4px;margin-top:28px;break-before:page}
h1+p+p+h2{break-before:auto}
h3{font-size:12.5pt;color:#1E3A8A;margin-top:20px;break-after:avoid}
img{max-width:100%;max-height:225mm;object-fit:contain;display:block;margin:10px auto;border:1px solid #E2E8F0;border-radius:6px;break-inside:avoid}
table{border-collapse:collapse;width:100%;margin:10px 0;font-size:9.5pt}
th,td{border:1px solid #CBD5E1;padding:5px 8px;vertical-align:top;text-align:left}
th{background:#F1F5F9} tr{break-inside:avoid}
code{font-family:'Noto Sans Mono CJK SC',monospace;background:#F1F5F9;padding:1px 4px;border-radius:3px;font-size:9pt}
blockquote{margin:12px 0;padding:8px 14px;background:#F5F3FF;border-left:4px solid #7C3AED;color:#334155}
blockquote p{margin:0} li{margin:3px 0}
"""


def prepare(md: str, work: Path) -> str:
    """占位符 → 截图；Mermaid → PNG；去掉文档里的 @提及。"""
    images = json.loads((MAN / "images.json").read_text(encoding="utf-8"))
    img_dir = work / "img"
    shutil.copytree(MAN / "img", img_dir, dirs_exist_ok=True)

    def img(m):
        alt = m.group(1).strip()
        f = images.get(alt)
        return f"![{alt}](img/{f})" if f and (img_dir / f).exists() else f"（图：{alt}）"
    md = re.sub(r"(?:&#91;|\[)image:\s*(.*?)\\?\]", img, md)
    md = re.sub(r"^((?:[A-Z][a-z]{2} \d{1,2}, \d{4})|\d{4}-\d{2}-\d{2}) · @\S+\s*$",
                lambda m: m.group(1), md, count=1, flags=re.M)
    n = 0

    def mermaid(m):
        nonlocal n
        n += 1
        src, png = work / f"diagram{n}.mmd", work / f"diagram{n}.png"
        src.write_text(m.group(1), encoding="utf-8")
        cfg = work / "mmd.json"
        cfg.write_text(json.dumps({"theme": "neutral", "themeVariables": {"fontFamily": "Noto Sans CJK SC, sans-serif"}}))
        try:
            subprocess.run(["mmdc", "-i", str(src), "-o", str(png), "-c", str(cfg), "-s", "3", "-b", "white", "-w", "1100"],
                           check=True, capture_output=True, timeout=120)
            return f"![系统总览图](diagram{n}.png)"
        except Exception:  # noqa: BLE001
            return m.group(0)
    return re.sub(r"```mermaid\n(.*?)```", mermaid, md, flags=re.S)


def build(md_path: Path, out_dir: Path, rev: int | None = None) -> dict:
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        md = prepare(md_path.read_text(encoding="utf-8"), work)
        (work / "manual.md").write_text(md, encoding="utf-8")
        subprocess.run(["pandoc", "manual.md", "-f", "gfm", "-t", "html", "-o", "body.html"], cwd=work, check=True)
        logo = (ROOT / "assets" / "logo-mark.svg").read_text(encoding="utf-8") if (ROOT / "assets" / "logo-mark.svg").exists() else ""
        html = (f"<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><style>{CSS}</style></head><body>"
                f"<div style='width:56px;margin-bottom:10px'>{logo}</div>{(work / 'body.html').read_text(encoding='utf-8')}</body></html>")
        (work / "manual.html").write_text(html, encoding="utf-8")
        pdf, docx = out_dir / f"{NAME}.pdf", out_dir / f"{NAME}.docx"
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page()
            pg.goto((work / "manual.html").as_uri())
            pg.wait_for_load_state("networkidle")
            pg.pdf(path=str(pdf), format="A4", print_background=True, display_header_footer=True,
                   header_template="<span></span>",
                   footer_template='<div style="font-size:8px;color:#94A3B8;width:100%;text-align:center">'
                                   'AI Quant Strategy Lab 使用说明 · <span class="pageNumber"></span> / <span class="totalPages"></span></div>',
                   margin={"top": "16mm", "bottom": "18mm", "left": "15mm", "right": "15mm"})
            b.close()
        subprocess.run(["pandoc", "manual.md", "-f", "gfm", "-o", str(docx), "--resource-path", str(work)], cwd=work, check=True)
    info = {"rev": rev, "built_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "pdf": pdf.name, "docx": docx.name}
    (MAN / "manual_rev.json").write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    return info


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    rev = None
    if "--rev" in sys.argv:
        rev = int(sys.argv[sys.argv.index("--rev") + 1])
        args = [a for a in args if a != str(rev)]
    if not args:
        print(__doc__)
        return 2
    info = build(Path(args[0]), Path(args[1]) if len(args) > 1 else ROOT / "docs", rev)
    print(json.dumps(info, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
