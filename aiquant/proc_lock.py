"""跨进程独占锁：同一时间只允许一个进程运行后台交易循环。

背景（2026-10-30）：重启时同时起了两个服务进程，两个交易循环同时写同一个行情缓存文件，把文件写坏了。
锁用操作系统的文件锁（Windows msvcrt.locking / 其他 fcntl.flock），进程退出（包括崩溃）时由系统自动释放，
不会留下「死锁文件」。拿不到锁的进程不启动交易循环。
"""
from __future__ import annotations

import os
from pathlib import Path

from . import config

_held: dict[str, object] = {}


def path(name: str) -> Path:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    return config.LOG_DIR / f"{name}.lock"


def acquire(name: str) -> bool:
    """非阻塞地拿锁；本进程已持有时返回 True。拿到后一直持有到进程退出。"""
    if name in _held:
        return True
    p = path(name)
    f = open(p, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False
    try:
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
    except OSError:
        pass
    _held[name] = f
    return True


def held(name: str) -> bool:
    return name in _held


def holder(name: str) -> int | None:
    """持锁进程号（只作显示用）。"""
    try:
        return int(path(name).read_text().strip() or 0) or None
    except Exception:  # noqa: BLE001
        return None


def release(name: str) -> None:
    f = _held.pop(name, None)
    if f is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    f.close()
