"""部署工具：在 git 仓库里生成 BUILD_INFO.json（commit、提交说明、时间、最近 300 个提交），随代码一起复制到运行目录。

运行目录不是 git 仓库时，系统靠这个文件判断“磁盘上的代码版本”，并与正在运行的版本比较（aiquant/build.py）。
用法：python scripts/write_build_info.py [输出路径，默认项目根目录/BUILD_INFO.json]
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def git(*a: str) -> str:
    return subprocess.run(["git", *a], cwd=str(ROOT), capture_output=True, text=True, check=True).stdout.strip()


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "BUILD_INFO.json"
    sha = git("rev-parse", "HEAD")
    subject, when = git("log", "-1", "--format=%s%x1f%cI").split("\x1f")
    history = git("rev-list", "--max-count=300", "HEAD").splitlines()
    info = {"commit": sha, "short": sha[:7], "subject": subject, "committed_at": when, "history": history}
    out.write_text(json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{out}: {sha[:7]} {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
