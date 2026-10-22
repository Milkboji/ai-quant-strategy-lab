"""备份与维护（不复制任何密钥文件）。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config


def code_hashes() -> dict:
    out = {}
    for p in sorted((config.ROOT / "aiquant").rglob("*.py")):
        out[str(p.relative_to(config.ROOT))] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    return out


def backup() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dst = config.BACKUP_DIR / ts
    dst.mkdir(parents=True, exist_ok=True)
    if config.DB_PATH.exists():
        src = sqlite3.connect(str(config.DB_PATH))
        out = sqlite3.connect(str(dst / "aiquant.db"))
        with out:
            src.backup(out)
        out.close(); src.close()
    (dst / "code_hashes.json").write_text(json.dumps(code_hashes(), indent=2), encoding="utf-8")
    return dst
