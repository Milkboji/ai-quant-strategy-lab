"""账号与权限：迁移、密码策略、角色、停用、删除保护、会话失效、锁定、来源判断。"""
import json

import pytest

from aiquant import auth, db


@pytest.fixture()
def af(env, tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_FILE", tmp_path / "auth.json")
    auth._ready = None
    auth.ensure()
    for t in ("users", "auth_log", "user_audit"):
        db.ex(f"DELETE FROM {t}")
    yield auth
    auth._ready = None


def test_legacy_migration_keeps_password(env, tmp_path, monkeypatch):
    import secrets
    salt = secrets.token_hex(16)
    legacy = {"secret": secrets.token_hex(32), "users": {"milk": {"salt": salt, "hash": auth._hash("Old2026pass", salt),
                                                                 "iter": auth.ITER, "gen": 3}}}
    (tmp_path / "auth.json").write_text(json.dumps(legacy))
    monkeypatch.setattr(auth, "AUTH_FILE", tmp_path / "auth.json")
    auth._ready = None
    auth.ensure()
    db.ex("DELETE FROM users WHERE username<>'milk'")
    u = auth.get_user("milk")
    assert u and u["role"] == "admin" and "hash" not in u
    assert auth.verify("milk", "Old2026pass", "1.1.1.1")[0]
    assert "users" not in json.loads((tmp_path / "auth.json").read_text())       # 文件里不再保存密码哈希
    auth._ready = None
    db.ex("DELETE FROM users")


def test_create_and_password_policy(af):
    with pytest.raises(ValueError):
        af.create_user("a", "Quant2026pass", "admin", "t")               # 用户名太短
    with pytest.raises(ValueError):
        af.create_user("bob", "short1", "viewer", "t")
    with pytest.raises(ValueError):
        af.create_user("bob", "bob2026quantx", "viewer", "t")            # 含用户名
    with pytest.raises(ValueError):
        af.create_user("bob", "Quant2026pass", "boss", "t")              # 角色不对
    pw = af.create_user("bob", None, "viewer", "t")                      # 临时密码
    assert len(pw) == 12 and not af.password_problem(pw, "bob")
    assert af.get_user("bob")["must_change"] == 1
    with pytest.raises(ValueError):
        af.create_user("bob", None, "viewer", "t")                       # 重名
    assert af.verify("bob", pw, "2.2.2.2")[0]
    af.set_password("bob", "NewQuant2026x", "bob")
    assert af.get_user("bob")["must_change"] == 0
    with pytest.raises(ValueError):
        af.set_password("bob", "NewQuant2026x", "bob")                   # 与当前相同
    assert any(a["action"] == "创建账号" for a in af.audit_log())


def test_roles_and_last_admin_protection(af):
    af.create_user("boss", "Quant2026pass", "admin", "t", must_change=False)
    af.create_user("tom", "Quant2026pass", "trader", "boss", must_change=False)
    with pytest.raises(ValueError):
        af.set_role("boss", "viewer", "boss")                            # 唯一管理员不能降级
    with pytest.raises(ValueError):
        af.set_disabled("boss", True, "tom")                             # 唯一管理员不能停用
    with pytest.raises(ValueError):
        af.delete_user("boss", "tom")
    with pytest.raises(ValueError):
        af.delete_user("tom", "tom")                                     # 不能删自己
    af.set_role("tom", "admin", "boss")
    af.set_role("boss", "viewer", "tom")                                 # 有别的管理员后可以
    assert af.role_of("boss") == "viewer" and af.can("boss", "paper") and not af.can("boss", "users")
    assert af.can("tom", "users") and af.can(None, "settings")           # 本机 = 管理员
    assert not af.can("nobody", "lab")


def test_sessions_invalidated_by_admin_actions(af):
    af.create_user("boss", "Quant2026pass", "admin", "t", must_change=False)
    af.create_user("amy", "Quant2026pass", "trader", "boss", must_change=False)
    for action in (lambda: af.revoke_sessions("amy", "boss"), lambda: af.set_role("amy", "viewer", "boss"),
                   lambda: af.reset_password("amy", "boss")):
        tok, gen = af.make_token("amy"), af.user_gen("amy")
        assert af.check_token(tok) == "amy" and af.session_valid("amy", gen)
        action()
        assert af.check_token(tok) is None and not af.session_valid("amy", gen)
    assert af.get_user("amy")["must_change"] == 1                         # 重置后必须改密码
    af.set_password("amy", "Zeta2026quantX", "amy")
    tok = af.make_token("amy")
    af.set_disabled("amy", True, "boss")
    assert af.check_token(tok) is None
    ok, msg = af.verify("amy", "Zeta2026quantX", "3.3.3.3")
    assert not ok and "停用" in msg
    af.set_disabled("amy", False, "boss")
    assert af.verify("amy", "Zeta2026quantX", "3.3.3.3")[0]
    af.delete_user("amy", "boss")
    assert af.check_token(af.make_token("amy")) is None and af.get_user("amy") is None


def test_token_tamper_and_expiry(af):
    af.create_user("boss", "Quant2026pass", "admin", "t", must_change=False)
    t = af.make_token("boss")
    u, exp, gen, sig = t.split("|")
    assert af.check_token(f"boss|{int(exp) + 999}|{gen}|{sig}") is None
    assert af.check_token(af.make_token("boss", days=-1)) is None
    assert af.check_token("garbage") is None and af.check_token(None) is None


def test_lockout(af):
    af.create_user("boss", "Quant2026pass", "admin", "t", must_change=False)
    for _ in range(5):
        af.verify("boss", "bad-pass-123", "9.9.9.9")
    ok, msg = af.verify("boss", "Quant2026pass", "9.9.9.9")
    assert not ok and "分钟" in msg
    assert not af.verify("boss", "Quant2026pass", "8.8.8.8")[0]          # 按账号锁定
    assert af.get_user("boss")["last_login_at"] is None


def test_request_origin_and_settings(af):
    local = af.request_info({"Host": "127.0.0.1:8501"})
    assert not local["remote"] and not af.login_required(local)
    cf = af.request_info({"Host": "quant.20261023.xyz", "Cf-Connecting-Ip": "5.6.7.8", "Cf-Ray": "x"})
    assert cf["remote"] and cf["ip"] == "5.6.7.8" and af.login_required(cf)
    assert af.request_info({"Host": "127.0.0.1", "CF-Ray": "abc"})["remote"]
    af.update_settings("boss", require_cf_access=True, cf_allowed_emails=["me@example.com"], session_days=3)
    assert af.cf_access_problem(cf) and af.cf_access_problem(dict(cf, cf_email="Me@Example.com")) is None
    assert af.settings()["session_days"] == 3
    af.update_settings("boss", require_local=True)
    assert af.login_required(local)
    assert any(a["action"] == "修改安全设置" for a in af.audit_log())
