"""运行版本与磁盘版本：启动时记录当前运行代码的 commit；守护进程每次自检比较磁盘上的 commit。

磁盘版本来源（按顺序）：
1. 项目目录是 git 仓库：git rev-parse HEAD（提交数用 git rev-list --count 计算）；
2. 否则读 BUILD_INFO.json（部署时由 scripts/write_build_info.py 生成：commit、subject、committed_at、history 最近提交列表）。
不一致时写 settings.pending_update，全站顶部显示“有更新待生效（x 个提交）”，管理员可安全重启（aiquant/safe_restart.py）。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import config, db

BUILD_FILE = "BUILD_INFO.json"
NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}


def _git(root: Path, *args: str) -> str | None:
    if not (root / ".git").exists():
        return None
    try:
        r = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, timeout=10, **NO_WINDOW)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def disk_build(root: Path | None = None) -> dict | None:
    """磁盘上的代码版本：{"commit", "short", "subject", "committed_at", "source", "history"}；无法确定时 None。"""
    root = Path(root or config.ROOT)
    sha = _git(root, "rev-parse", "HEAD")
    if sha:
        info = (_git(root, "log", "-1", "--format=%s%x1f%cI") or "").split("\x1f")
        return {"commit": sha, "short": sha[:7], "subject": info[0] if info else "",
                "committed_at": info[1] if len(info) > 1 else None, "source": "git", "history": None}
    f = root / BUILD_FILE
    if f.exists():
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if d.get("commit"):
                d.setdefault("short", d["commit"][:7])
                d["source"] = "file"
                return d
        except Exception:  # noqa: BLE001
            return None
    return None


def commits_between(old: str, new: str, root: Path | None = None, history: list | None = None) -> int | None:
    """old → new 之间的提交数；无法确定时 None。"""
    if old == new:
        return 0
    root = Path(root or config.ROOT)
    n = _git(root, "rev-list", "--count", f"{old}..{new}")
    if n and n.isdigit():
        return int(n)
    if history:
        try:
            return history.index(old)          # history 新的在前：old 的位置 = 之后的提交数
        except ValueError:
            return None
    return None


def record_running(pid: int | None = None) -> dict | None:
    """服务启动时调用：记录当前运行的代码版本。"""
    b = disk_build()
    rec = {k: v for k, v in (b or {}).items() if k != "history"}
    rec.update(at=db.now_iso(), pid=pid or os.getpid(), known=bool(b))
    db.set_setting("running_build", rec)
    db.set_setting("pending_update", None)
    return rec


def running_build() -> dict | None:
    return db.get_setting("running_build")


def check_pending() -> dict | None:
    """守护进程每次自检时调用：磁盘版本与运行版本不一致 → settings.pending_update；一致 → 清除。"""
    disk = disk_build()
    run = running_build()
    if not disk:
        db.set_setting("pending_update", None)
        return None
    if run and run.get("commit") == disk["commit"]:
        if db.get_setting("pending_update"):
            db.set_setting("pending_update", None)
        return None
    count = commits_between(run["commit"], disk["commit"], history=disk.get("history")) if run and run.get("commit") else None
    pu = {"disk_commit": disk["commit"], "disk_short": disk["short"], "disk_subject": disk.get("subject", ""),
          "running_commit": (run or {}).get("commit"), "running_short": (run or {}).get("short"),
          "count": count, "checked_at": db.now_iso()}
    db.set_setting("pending_update", pu)
    return pu


def pending_update() -> dict | None:
    return db.get_setting("pending_update")
