"""系统登录（第二层；第一层是 Cloudflare Access 邮箱验证码）。

- 账号保存在 config/auth.json：PBKDF2-SHA256（26 万次迭代）+ 随机盐，不保存明文。
- 本机访问（127.0.0.1 / localhost 且没有经过 Cloudflare）默认免登录，方便桌面图标直接打开；
  经 Cloudflare Tunnel 来的外网访问一律要登录。可在 config/auth.json 里把 require_local 设为 true 让本机也要登录。
- 登录状态保存在签名 Cookie（HMAC-SHA256，默认 7 天），刷新页面不用重新登录；退出登录或改密码后作废。
- 同一账号或同一来源 IP 15 分钟内输错 5 次 → 锁定 15 分钟。每次登录尝试写入 auth_log 表（不含密码）。
- 外网访问时若开启 require_cf_access，还会检查 Cloudflare Access 的已验证邮箱是否在白名单里。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time

from . import config, db

AUTH_FILE = config.CONFIG_DIR / "auth.json"
COOKIE = "aq_session"
ITER = 260_000
MAX_FAILS, WINDOW_SEC, LOCK_SEC = 5, 900, 900
SESSION_DAYS = 7
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------

def _load() -> dict:
    try:
        d = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        d = {}
    d.setdefault("users", {})
    d.setdefault("require_local", False)
    d.setdefault("require_cf_access", False)
    d.setdefault("cf_allowed_emails", [])
    if not d.get("secret"):
        d["secret"] = secrets.token_hex(32)
        _save(d)
    return d


def _save(d: dict) -> None:
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, AUTH_FILE)


def _hash(pw: str, salt: str, it: int = ITER) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), it).hex()


def has_users() -> bool:
    return bool(_load()["users"])


def password_problem(pw: str) -> str | None:
    if len(pw) < 10:
        return "密码至少 10 位"
    if pw.isdigit() or pw.isalpha():
        return "密码需要同时包含字母和数字"
    return None


def set_user(username: str, password: str) -> None:
    username = username.strip()
    if not username or len(username) > 32:
        raise ValueError("用户名为 1–32 个字符")
    p = password_problem(password)
    if p:
        raise ValueError(p)
    d = _load()
    salt = secrets.token_hex(16)
    old = d["users"].get(username, {})
    d["users"][username] = {"salt": salt, "hash": _hash(password, salt), "iter": ITER,
                            "gen": int(old.get("gen", 0)) + 1, "updated_at": db.now_iso()}
    _save(d)


def settings() -> dict:
    d = _load()
    return {k: d[k] for k in ("require_local", "require_cf_access", "cf_allowed_emails")}


def update_settings(**kw) -> None:
    d = _load()
    for k in ("require_local", "require_cf_access", "cf_allowed_emails"):
        if k in kw:
            d[k] = kw[k]
    _save(d)


# ---------------------------------------------------------------------------
# 校验、锁定、日志
# ---------------------------------------------------------------------------

def _ensure_log() -> None:
    db.ex("CREATE TABLE IF NOT EXISTS auth_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, epoch REAL, "
          "username TEXT, ip TEXT, ok INTEGER, note TEXT)")


def _log(user: str, ip: str, ok: bool, note: str = "") -> None:
    _ensure_log()
    db.ex("INSERT INTO auth_log(ts,epoch,username,ip,ok,note) VALUES(?,?,?,?,?,?)",
          (db.now_iso(), time.time(), user[:32], ip[:64], 1 if ok else 0, note[:120]))


def locked(user: str, ip: str) -> int:
    """返回剩余锁定秒数（0 = 未锁定）。"""
    _ensure_log()
    now = time.time()
    for col, val in (("username", user), ("ip", ip)):
        if not val:
            continue
        rows = db.q(f"SELECT epoch, ok FROM auth_log WHERE {col}=? AND epoch>? ORDER BY epoch DESC LIMIT 20",
                    (val, now - WINDOW_SEC - LOCK_SEC))
        fails = []
        for r in rows:
            if r["ok"]:
                break
            fails.append(r["epoch"])
        # 连续失败中，与最近一次失败相隔不超过 15 分钟的次数
        recent = [f for f in fails if fails and f > fails[0] - WINDOW_SEC]
        if len(recent) >= MAX_FAILS and recent[0] + LOCK_SEC > now:
            return int(recent[0] + LOCK_SEC - now)
    return 0


def verify(user: str, pw: str, ip: str = "") -> tuple[bool, str]:
    user = (user or "").strip()
    wait = locked(user, ip)
    if wait:
        return False, f"尝试次数过多，请 {wait // 60 + 1} 分钟后再试"
    u = _load()["users"].get(user)
    ok = bool(u) and hmac.compare_digest(_hash(pw or "", u["salt"], int(u.get("iter", ITER))), u["hash"])
    if not u:   # 用户不存在时也做一次哈希，避免通过响应时间猜用户名
        _hash(pw or "", "00" * 16)
    _log(user, ip, ok, "" if ok else "密码错误或用户不存在")
    return (True, "") if ok else (False, "用户名或密码不正确")


# ---------------------------------------------------------------------------
# 会话令牌（签名 Cookie）
# ---------------------------------------------------------------------------

def make_token(user: str, days: int = SESSION_DAYS) -> str:
    d = _load()
    gen = d["users"].get(user, {}).get("gen", 0)
    exp = int(time.time() + days * 86400)
    body = f"{user}|{exp}|{gen}"
    sig = hmac.new(bytes.fromhex(d["secret"]), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}|{sig}"


def check_token(tok: str | None) -> str | None:
    if not tok or tok.count("|") != 3:
        return None
    user, exp, gen, sig = tok.split("|")
    d = _load()
    good = hmac.new(bytes.fromhex(d["secret"]), f"{user}|{exp}|{gen}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, sig):
        return None
    u = d["users"].get(user)
    if not u or str(u.get("gen", 0)) != gen or int(exp) < time.time():
        return None
    return user


# ---------------------------------------------------------------------------
# 请求来源判断
# ---------------------------------------------------------------------------

def request_info(headers: dict) -> dict:
    h = {str(k).lower(): v for k, v in (headers or {}).items()}
    host = (h.get("host") or "").split(":")[0].lower()
    via_cf = any(k.startswith("cf-") for k in h)
    remote = via_cf or (host not in LOCAL_HOSTS and host != "")
    return {"remote": remote, "host": host, "ip": h.get("cf-connecting-ip") or h.get("x-forwarded-for", "") or "local",
            "cf_email": h.get("cf-access-authenticated-user-email", ""), "https": h.get("x-forwarded-proto") == "https"
            or via_cf}


def login_required(info: dict) -> bool:
    return info["remote"] or bool(_load()["require_local"])


def cf_access_problem(info: dict) -> str | None:
    d = _load()
    if not info["remote"] or not d["require_cf_access"]:
        return None
    em = (info["cf_email"] or "").lower()
    if not em:
        return "这个地址需要先通过 Cloudflare 邮箱验证码登录。"
    allow = [e.lower() for e in d["cf_allowed_emails"]]
    if allow and em not in allow:
        return "这个邮箱没有访问权限。"
    return None
