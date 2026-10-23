"""登录：密码哈希、锁定、签名 Cookie、来源判断、Cloudflare Access 白名单。"""
import time

import pytest

from aiquant import auth, db


@pytest.fixture()
def af(env, tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_FILE", tmp_path / "auth.json")
    db.ex("CREATE TABLE IF NOT EXISTS auth_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, epoch REAL, "
          "username TEXT, ip TEXT, ok INTEGER, note TEXT)")
    db.ex("DELETE FROM auth_log")
    return auth


def test_password_rules_and_hash_storage(af):
    with pytest.raises(ValueError):
        af.set_user("admin", "short1")
    with pytest.raises(ValueError):
        af.set_user("admin", "onlyletterslong")
    af.set_user("admin", "Quant2026pass")
    raw = af.AUTH_FILE.read_text()
    assert "Quant2026pass" not in raw and af.has_users()
    assert af.verify("admin", "Quant2026pass", "1.2.3.4")[0]
    assert not af.verify("admin", "wrong-pass-1", "1.2.3.4")[0]
    assert not af.verify("nobody", "Quant2026pass", "1.2.3.4")[0]


def test_lockout_after_five_failures(af):
    af.set_user("admin", "Quant2026pass")
    for _ in range(5):
        af.verify("admin", "bad-pass-123", "9.9.9.9")
    ok, msg = af.verify("admin", "Quant2026pass", "9.9.9.9")
    assert not ok and "分钟" in msg                     # 正确密码也被锁
    ok, _ = af.verify("admin", "Quant2026pass", "8.8.8.8")
    assert not ok                                       # 按账号锁定，换 IP 也不行


def test_token_sign_expire_and_invalidate(af):
    af.set_user("admin", "Quant2026pass")
    t = af.make_token("admin")
    assert af.check_token(t) == "admin"
    u, exp, gen, sig = t.split("|")
    assert af.check_token(f"admin|{int(exp) + 999}|{gen}|{sig}") is None     # 篡改到期时间
    assert af.check_token(af.make_token("admin", days=-1)) is None             # 已过期
    af.set_user("admin", "NewQuant2026x")                                      # 改密码 → 旧令牌作废
    assert af.check_token(t) is None
    assert af.check_token(None) is None and af.check_token("garbage") is None


def test_request_origin_and_cf_access(af):
    local = af.request_info({"Host": "127.0.0.1:8501"})
    assert not local["remote"] and not af.login_required(local)
    cf = af.request_info({"Host": "quant.20261023.xyz", "Cf-Connecting-Ip": "5.6.7.8", "Cf-Ray": "x"})
    assert cf["remote"] and cf["ip"] == "5.6.7.8" and af.login_required(cf)
    sneaky = af.request_info({"Host": "127.0.0.1", "CF-Ray": "abc"})          # 经 Tunnel 转发时 Host 可能是本机
    assert sneaky["remote"]
    af.update_settings(require_cf_access=True, cf_allowed_emails=["me@example.com"])
    assert af.cf_access_problem(cf)                                             # 没有验证邮箱
    ok = dict(cf, cf_email="Me@Example.com")
    assert af.cf_access_problem(ok) is None
    assert af.cf_access_problem(dict(cf, cf_email="x@y.com"))
    assert af.cf_access_problem(local) is None                                  # 本机不检查
    af.update_settings(require_local=True)
    assert af.login_required(local)
