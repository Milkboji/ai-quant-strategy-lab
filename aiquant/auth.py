"""账号与权限（后端）。

存储
- 用户保存在 SQLite 表 users：PBKDF2-SHA256（26 万次迭代）+ 每人独立随机盐，不保存明文密码。
- 会话签名密钥和安全设置保存在 config/auth.json（已在 .gitignore 中排除）。旧版只存在 auth.json 里的账号会自动迁移为管理员。

角色
- admin  管理员：全部功能 + 用户管理 + 系统设置
- trader 交易员：全部交易与研究功能，不能管理用户和系统设置
- viewer 观察员：只读（总览、实时看盘、布林带、模拟盘与进化记录的查看页面）

安全
- 本机访问（127.0.0.1 且没有经过 Cloudflare）默认免登录并视为管理员；外网访问一律需要登录。
- 同一账号或同一来源 IP 15 分钟内输错 5 次 → 锁定 15 分钟；被停用的账号不能登录。
- 登录状态保存在签名 Cookie（HMAC-SHA256）。改密码、重置密码、停用、改角色、"退出所有设备"都会让该账号已有的登录全部失效。
- 管理员创建账号或重置密码时生成临时密码，对方首次登录必须修改。
- 登录尝试写入 auth_log，管理操作写入 user_audit（都不含密码）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import string
import time

from . import config, db

AUTH_FILE = config.CONFIG_DIR / "auth.json"
COOKIE = "aq_session"
ITER = 260_000
MAX_FAILS, WINDOW_SEC, LOCK_SEC = 5, 900, 900
SESSION_DAYS = 7
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
LOCAL_USER = "本机"

ROLES = {"admin": "管理员", "trader": "交易员", "viewer": "观察员"}
ROLE_DESC = {"admin": "全部功能，可管理用户和系统设置",
             "trader": "全部交易与研究功能，不能管理用户和系统设置",
             "viewer": "只读：总览、看盘、布林带、模拟盘与进化记录"}
# 每个角色可见的页面（与 app.py 中的页面 key 对应）
ROLE_PAGES = {
    "admin": {"home", "live", "boll", "paper", "evolve", "lab", "versions", "history", "data", "settings",
              "account", "users"},
    "trader": {"home", "live", "boll", "paper", "evolve", "lab", "versions", "history", "data", "account"},
    "viewer": {"home", "live", "boll", "paper", "evolve", "account"},   # 观察员：页面只读（按钮全部禁用）
}
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-一-鿿]{2,32}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY, display_name TEXT, role TEXT NOT NULL DEFAULT 'viewer',
  salt TEXT NOT NULL, hash TEXT NOT NULL, iter INTEGER NOT NULL, gen INTEGER NOT NULL DEFAULT 1,
  disabled INTEGER NOT NULL DEFAULT 0, must_change INTEGER NOT NULL DEFAULT 0,
  created_at TEXT, created_by TEXT, updated_at TEXT, pw_changed_at TEXT,
  last_login_at TEXT, last_login_ip TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS auth_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, epoch REAL,
  username TEXT, ip TEXT, ok INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS user_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, actor TEXT,
  action TEXT, target TEXT, detail TEXT, ip TEXT);
"""
_ready: tuple | None = None


# ---------------------------------------------------------------------------
# 存储与迁移
# ---------------------------------------------------------------------------

def _load_file() -> dict:
    try:
        d = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        d = {}
    d.setdefault("require_local", False)
    d.setdefault("require_cf_access", False)
    d.setdefault("cf_allowed_emails", [])
    d.setdefault("session_days", SESSION_DAYS)
    if not d.get("secret"):
        d["secret"] = secrets.token_hex(32)
        _save_file(d)
    return d


def _save_file(d: dict) -> None:
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, AUTH_FILE)


def ensure() -> None:
    """建表并迁移旧版 auth.json 里的账号（幂等）。"""
    global _ready
    key = (str(config.DB_PATH), str(AUTH_FILE))
    if _ready == key:
        return
    with db.tx() as c:
        c.executescript(SCHEMA)
    d = _load_file()
    legacy = d.get("users") or {}
    if legacy:
        for name, u in legacy.items():
            if not db.q1("SELECT 1 FROM users WHERE username=?", (name,)):
                db.ex("INSERT INTO users(username,display_name,role,salt,hash,iter,gen,created_at,created_by,updated_at,"
                      "pw_changed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (name, name, "admin", u["salt"], u["hash"], int(u.get("iter", ITER)), int(u.get("gen", 1)),
                       u.get("updated_at") or db.now_iso(), "迁移", db.now_iso(), u.get("updated_at") or db.now_iso()))
                audit("系统", "迁移账号", name, "旧版账号迁移为管理员")
        try:   # 迁移前备份一份（含哈希，不含明文），确认无误后可手动删除
            bak = AUTH_FILE.with_suffix(".json.bak")
            if not bak.exists():
                bak.write_text(json.dumps({"users": legacy}, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        d.pop("users", None)
        _save_file(d)
    _ready = key


def _hash(pw: str, salt: str, it: int = ITER) -> str:
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), it).hex()


def _clean(u: dict | None) -> dict | None:
    if not u:
        return None
    u = dict(u)
    u.pop("salt", None)
    u.pop("hash", None)
    u["role_cn"] = ROLES.get(u.get("role"), u.get("role"))
    return u


def has_users() -> bool:
    ensure()
    return bool(db.q1("SELECT 1 FROM users LIMIT 1"))


def get_user(username: str) -> dict | None:
    ensure()
    return _clean(db.q1("SELECT * FROM users WHERE username=?", ((username or "").strip(),)))


def list_users() -> list[dict]:
    ensure()
    return [_clean(u) for u in db.q("SELECT * FROM users ORDER BY role='admin' DESC, username")]


def _admins_active(exclude: str | None = None) -> int:
    r = db.q1("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0 AND username<>?", (exclude or "",))
    return int(r["n"])


def password_problem(pw: str, username: str = "") -> str | None:
    if len(pw or "") < 10:
        return "密码至少 10 位"
    if pw.isdigit() or pw.isalpha():
        return "密码需要同时包含字母和数字"
    if username and username.lower() in pw.lower():
        return "密码不能包含用户名"
    if len(set(pw)) < 5:
        return "密码太简单（重复字符太多）"
    return None


def temp_password() -> str:
    """12 位临时密码：大小写字母 + 数字，去掉容易看错的字符。"""
    alpha = "".join(c for c in string.ascii_letters + string.digits if c not in "0OolI1")
    while True:
        p = "".join(secrets.choice(alpha) for _ in range(12))
        if any(c.isdigit() for c in p) and any(c.isalpha() for c in p):
            return p


def audit(actor: str, action: str, target: str = "", detail: str = "", ip: str = "") -> None:
    db.ex("INSERT INTO user_audit(ts,actor,action,target,detail,ip) VALUES(?,?,?,?,?,?)",
          (db.now_iso(), (actor or "")[:32], action[:40], (target or "")[:32], (detail or "")[:200], (ip or "")[:64]))


# ---------------------------------------------------------------------------
# 用户管理（调用方负责检查操作者是管理员）
# ---------------------------------------------------------------------------

def create_user(username: str, password: str | None, role: str, actor: str, display_name: str = "",
                must_change: bool = True, note: str = "") -> str:
    """创建账号；password 为空时生成临时密码。返回实际密码（仅此一次显示给管理员）。"""
    ensure()
    username = (username or "").strip()
    if not USERNAME_RE.match(username) or username == LOCAL_USER:
        raise ValueError("用户名为 2–32 位，可用字母、数字、中文、下划线、点和横线")
    if role not in ROLES:
        raise ValueError("角色不正确")
    if db.q1("SELECT 1 FROM users WHERE username=?", (username,)):
        raise ValueError("用户名已存在")
    pw = password or temp_password()
    p = password_problem(pw, username)
    if p:
        raise ValueError(p)
    salt = secrets.token_hex(16)
    now = db.now_iso()
    db.ex("INSERT INTO users(username,display_name,role,salt,hash,iter,gen,disabled,must_change,created_at,created_by,"
          "updated_at,pw_changed_at,note) VALUES(?,?,?,?,?,?,1,0,?,?,?,?,?,?)",
          (username, display_name.strip() or username, role, salt, _hash(pw, salt), ITER, 1 if must_change else 0,
           now, actor, now, now, note[:200]))
    audit(actor, "创建账号", username, f"角色 {ROLES[role]}" + ("，首次登录需改密码" if must_change else ""))
    return pw


def set_password(username: str, password: str, actor: str, must_change: bool = False) -> None:
    ensure()
    u = db.q1("SELECT * FROM users WHERE username=?", (username,))
    if not u:
        raise ValueError("账号不存在")
    p = password_problem(password, username)
    if p:
        raise ValueError(p)
    if not must_change and hmac.compare_digest(_hash(password, u["salt"], int(u["iter"])), u["hash"]):
        raise ValueError("新密码不能与当前密码相同")
    salt = secrets.token_hex(16)
    now = db.now_iso()
    db.ex("UPDATE users SET salt=?, hash=?, iter=?, gen=gen+1, must_change=?, updated_at=?, pw_changed_at=? "
          "WHERE username=?", (salt, _hash(password, salt), ITER, 1 if must_change else 0, now, now, username))
    audit(actor, "修改密码" if actor == username else "重置密码", username,
          "已作废该账号所有登录状态" + ("，下次登录需改密码" if must_change else ""))


def reset_password(username: str, actor: str) -> str:
    pw = temp_password()
    set_password(username, pw, actor, must_change=True)
    return pw


def set_role(username: str, role: str, actor: str) -> None:
    ensure()
    if role not in ROLES:
        raise ValueError("角色不正确")
    u = get_user(username)
    if not u:
        raise ValueError("账号不存在")
    if u["role"] == "admin" and role != "admin" and _admins_active(exclude=username) == 0:
        raise ValueError("至少要保留一个可用的管理员")
    db.ex("UPDATE users SET role=?, gen=gen+1, updated_at=? WHERE username=?", (role, db.now_iso(), username))
    audit(actor, "修改角色", username, f"{u['role_cn']} → {ROLES[role]}")


def set_disabled(username: str, disabled: bool, actor: str) -> None:
    ensure()
    u = get_user(username)
    if not u:
        raise ValueError("账号不存在")
    if disabled and username == actor:
        raise ValueError("不能停用自己的账号")
    if disabled and u["role"] == "admin" and _admins_active(exclude=username) == 0:
        raise ValueError("至少要保留一个可用的管理员")
    db.ex("UPDATE users SET disabled=?, gen=gen+1, updated_at=? WHERE username=?",
          (1 if disabled else 0, db.now_iso(), username))
    audit(actor, "停用账号" if disabled else "启用账号", username)


def update_profile(username: str, actor: str, display_name: str | None = None, note: str | None = None) -> None:
    ensure()
    if display_name is not None:
        db.ex("UPDATE users SET display_name=?, updated_at=? WHERE username=?",
              (display_name.strip()[:32] or username, db.now_iso(), username))
    if note is not None:
        db.ex("UPDATE users SET note=?, updated_at=? WHERE username=?", (note[:200], db.now_iso(), username))
    audit(actor, "修改资料", username)


def revoke_sessions(username: str, actor: str) -> None:
    ensure()
    db.ex("UPDATE users SET gen=gen+1, updated_at=? WHERE username=?", (db.now_iso(), username))
    audit(actor, "退出所有设备", username)


def delete_user(username: str, actor: str) -> None:
    ensure()
    u = get_user(username)
    if not u:
        raise ValueError("账号不存在")
    if username == actor:
        raise ValueError("不能删除自己的账号")
    if u["role"] == "admin" and _admins_active(exclude=username) == 0:
        raise ValueError("至少要保留一个可用的管理员")
    db.ex("DELETE FROM users WHERE username=?", (username,))
    audit(actor, "删除账号", username, f"角色 {u['role_cn']}")


# 兼容旧接口：首个账号为管理员；已存在则修改密码
def set_user(username: str, password: str) -> None:
    ensure()
    if get_user(username):
        set_password(username, password, username)
    else:
        create_user(username, password, "admin" if not has_users() else "viewer", "本机", must_change=False)


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------

def settings() -> dict:
    d = _load_file()
    return {k: d[k] for k in ("require_local", "require_cf_access", "cf_allowed_emails", "session_days")}


def update_settings(actor: str = "本机", **kw) -> None:
    d = _load_file()
    changed = []
    for k in ("require_local", "require_cf_access", "cf_allowed_emails", "session_days"):
        if k in kw and d.get(k) != kw[k]:
            d[k] = kw[k]
            changed.append(k)
    _save_file(d)
    if changed:
        ensure()
        audit(actor, "修改安全设置", "", "、".join(changed))


# ---------------------------------------------------------------------------
# 登录校验、锁定、日志
# ---------------------------------------------------------------------------

def _log(user: str, ip: str, ok: bool, note: str = "") -> None:
    db.ex("INSERT INTO auth_log(ts,epoch,username,ip,ok,note) VALUES(?,?,?,?,?,?)",
          (db.now_iso(), time.time(), (user or "")[:32], (ip or "")[:64], 1 if ok else 0, note[:120]))


def locked(user: str, ip: str) -> int:
    """返回剩余锁定秒数（0 = 未锁定）。按账号和来源 IP 分别计算。"""
    ensure()
    now = time.time()
    for col, val in (("username", user), ("ip", ip)):
        if not val or val == "local":
            continue
        rows = db.q(f"SELECT epoch, ok FROM auth_log WHERE {col}=? AND epoch>? ORDER BY epoch DESC LIMIT 20",
                    (val, now - WINDOW_SEC - LOCK_SEC))
        fails = []
        for r in rows:
            if r["ok"]:
                break
            fails.append(r["epoch"])
        recent = [f for f in fails if f > fails[0] - WINDOW_SEC] if fails else []
        if len(recent) >= MAX_FAILS and recent[0] + LOCK_SEC > now:
            return int(recent[0] + LOCK_SEC - now)
    return 0


def verify(user: str, pw: str, ip: str = "") -> tuple[bool, str]:
    ensure()
    user = (user or "").strip()
    wait = locked(user, ip)
    if wait:
        return False, f"尝试次数过多，请 {wait // 60 + 1} 分钟后再试"
    u = db.q1("SELECT * FROM users WHERE username=?", (user,))
    if u:
        ok = hmac.compare_digest(_hash(pw or "", u["salt"], int(u["iter"])), u["hash"])
    else:   # 用户不存在时也做一次哈希，避免通过响应时间猜用户名
        _hash(pw or "", "00" * 16)
        ok = False
    if ok and u["disabled"]:
        _log(user, ip, False, "账号已停用")
        return False, "账号已停用，请联系管理员"
    _log(user, ip, ok, "" if ok else "密码错误或用户不存在")
    if ok:
        db.ex("UPDATE users SET last_login_at=?, last_login_ip=? WHERE username=?", (db.now_iso(), ip[:64], user))
        if int(u["iter"]) < ITER:   # 旧参数自动升级
            salt = secrets.token_hex(16)
            db.ex("UPDATE users SET salt=?, hash=?, iter=? WHERE username=?", (salt, _hash(pw, salt), ITER, user))
    return (True, "") if ok else (False, "用户名或密码不正确")


def login_log(limit: int = 200, username: str | None = None) -> list[dict]:
    ensure()
    if username:
        return db.q("SELECT ts,username,ip,ok,note FROM auth_log WHERE username=? ORDER BY id DESC LIMIT ?",
                    (username, limit))
    return db.q("SELECT ts,username,ip,ok,note FROM auth_log ORDER BY id DESC LIMIT ?", (limit,))


def audit_log(limit: int = 200) -> list[dict]:
    ensure()
    return db.q("SELECT ts,actor,action,target,detail,ip FROM user_audit ORDER BY id DESC LIMIT ?", (limit,))


# ---------------------------------------------------------------------------
# 会话令牌（签名 Cookie）
# ---------------------------------------------------------------------------

def make_token(user: str, days: int | None = None) -> str:
    ensure()
    d = _load_file()
    u = db.q1("SELECT gen FROM users WHERE username=?", (user,))
    gen = u["gen"] if u else 0
    exp = int(time.time() + (days if days is not None else int(d["session_days"])) * 86400)
    body = f"{user}|{exp}|{gen}"
    sig = hmac.new(bytes.fromhex(d["secret"]), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}|{sig}"


def check_token(tok: str | None) -> str | None:
    if not tok or tok.count("|") != 3:
        return None
    ensure()
    user, exp, gen, sig = tok.split("|")
    d = _load_file()
    good = hmac.new(bytes.fromhex(d["secret"]), f"{user}|{exp}|{gen}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(good, sig):
        return None
    u = db.q1("SELECT gen, disabled FROM users WHERE username=?", (user,))
    try:
        if not u or u["disabled"] or str(u["gen"]) != gen or int(exp) < time.time():
            return None
    except ValueError:
        return None
    return user


def session_valid(user: str, gen: int | None) -> bool:
    """会话中的用户是否仍然有效（账号未删除、未停用、没有被强制退出）。"""
    if user == LOCAL_USER:
        return True
    u = db.q1("SELECT gen, disabled FROM users WHERE username=?", (user,))
    return bool(u) and not u["disabled"] and (gen is None or int(u["gen"]) == int(gen))


def user_gen(user: str) -> int | None:
    u = db.q1("SELECT gen FROM users WHERE username=?", (user,))
    return int(u["gen"]) if u else None


# ---------------------------------------------------------------------------
# 请求来源与权限
# ---------------------------------------------------------------------------

def request_info(headers: dict) -> dict:
    h = {str(k).lower(): v for k, v in (headers or {}).items()}
    host = (h.get("host") or "").split(":")[0].lower()
    via_cf = any(k.startswith("cf-") for k in h)
    remote = via_cf or (host not in LOCAL_HOSTS and host != "")
    return {"remote": remote, "host": host, "ip": h.get("cf-connecting-ip") or h.get("x-forwarded-for", "") or "local",
            "cf_email": h.get("cf-access-authenticated-user-email", ""),
            "https": h.get("x-forwarded-proto") == "https" or via_cf}


def login_required(info: dict) -> bool:
    return info["remote"] or bool(_load_file()["require_local"])


def cf_access_problem(info: dict) -> str | None:
    d = _load_file()
    if not info["remote"] or not d["require_cf_access"]:
        return None
    em = (info["cf_email"] or "").lower()
    if not em:
        return "这个地址需要先通过 Cloudflare 邮箱验证码登录。"
    allow = [e.lower() for e in d["cf_allowed_emails"]]
    if allow and em not in allow:
        return "这个邮箱没有访问权限。"
    return None


def role_of(user: str | None) -> str:
    if user in (None, LOCAL_USER):
        return "admin"
    u = db.q1("SELECT role FROM users WHERE username=?", (user,))
    return u["role"] if u else "viewer"


def can(user: str | None, page: str) -> bool:
    return page in ROLE_PAGES.get(role_of(user), set())
