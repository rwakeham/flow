"""
auth.py: placeholder secrets refused, sessions that end when the password
changes, the login throttle behind a trusted proxy, and its size cap. Pure.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-" * 8)

import pytest  # noqa: E402
from itsdangerous import URLSafeTimedSerializer  # noqa: E402

import auth  # noqa: E402
import settings_store  # noqa: E402

GOOD_KEY = "a" * 64


@pytest.mark.parametrize("key,password,problem", [
    ("replacewithsecretrandomhex", "correct horse", "SECRET_KEY"),
    ("short", "correct horse", "SECRET_KEY"),
    (GOOD_KEY, "mysecretpassword", "FLOW_PASSWORD"),
    (GOOD_KEY, "MySecretPassword", "FLOW_PASSWORD"),
])
def test_placeholder_settings_are_refused(key, password, problem):
    with pytest.raises(auth.SettingsError, match=problem):
        auth.check_settings(key, password)


def test_good_settings_pass():
    auth.check_settings(GOOD_KEY, "correct horse")
    auth.check_settings(GOOD_KEY, "")                   # blank: the password is kept in the database


def test_an_install_with_no_password_anywhere_refuses_to_start():
    with pytest.raises(auth.SettingsError, match="setup.sh"):
        auth.require_password("", "")
    auth.require_password(settings_store.hash_password("x"), "")
    auth.require_password("", "first start")


def test_password_problem():
    assert auth.password_problem("") and auth.password_problem("ChangeMe") and auth.password_problem("password")
    assert auth.password_problem("correct horse") is None


@pytest.fixture
def password(monkeypatch):
    settings_store.testing_state(secrets_={settings_store.PASSWORD_HASH: settings_store.hash_password("first password")})
    yield monkeypatch
    settings_store.testing_state()


def test_a_session_ends_when_the_password_changes(password):
    token = auth.session_token()
    assert auth.session_valid(token)
    settings_store.testing_state(secrets_={settings_store.PASSWORD_HASH: settings_store.hash_password("second password")})
    assert not auth.session_valid(token)
    assert auth.session_valid(auth.session_token())
    settings_store.testing_state()
    assert not auth.verify_password("second password")  # no password stored at all: nothing signs in


def test_old_style_and_forged_cookies_are_refused(password):
    assert not auth.session_valid(URLSafeTimedSerializer(auth.SECRET_KEY).dumps("authenticated"))
    assert not auth.session_valid(URLSafeTimedSerializer("another key" * 4).dumps(
        {"v": 1, "p": auth._password_tag()}))
    assert not auth.session_valid("garbage")
    assert not auth.session_valid(None)


def test_the_cookie_does_not_carry_a_plain_password_hash(password):
    import hashlib
    token = auth.session_token()
    payload = URLSafeTimedSerializer(auth.SECRET_KEY).loads(token)
    assert payload["p"] not in hashlib.sha256(b"first password").hexdigest()   # keyed by SECRET_KEY
    assert "first password" not in token and settings_store.password_hash() not in token


def test_verify_password(password):
    assert auth.verify_password("first password")
    assert not auth.verify_password("first passwor")
    assert not auth.verify_password("pässword")        # non-ASCII input doesn't raise


PROXIES = auth.parse_proxies("10.0.0.0/8, 192.168.1.5")


@pytest.mark.parametrize("peer,xff,expected", [
    ("203.0.113.9", "198.51.100.1", "203.0.113.9"),                     # not a proxy: its header is ignored
    ("10.0.0.2", "198.51.100.1", "198.51.100.1"),                       # trusted proxy: the client it saw
    ("10.0.0.2", "1.2.3.4, 198.51.100.1", "198.51.100.1"),              # left entries are the client's own claim
    ("10.0.0.2", "198.51.100.1, 192.168.1.5", "198.51.100.1"),          # chained trusted proxies are skipped
    ("10.0.0.2", None, "10.0.0.2"),
    ("10.0.0.2", "not-an-ip", "not-an-ip"),
])
def test_client_ip_behind_trusted_proxies(peer, xff, expected):
    assert auth.client_ip(peer, xff, PROXIES) == expected


def test_no_trusted_proxies_means_the_peer():
    assert auth.client_ip("10.0.0.2", "198.51.100.1", []) == "10.0.0.2"


def test_bad_proxy_setting_is_an_error():
    with pytest.raises(auth.SettingsError):
        auth.parse_proxies("10.0.0.0/8, example.com")


def test_throttle_locks_after_repeated_failures(monkeypatch):
    monkeypatch.setattr(auth, "_login_failures", {})
    results = [auth.record_login_attempt("198.51.100.7", False)[0] for _ in range(6)]
    assert results == [False] * 5 + [True]
    assert auth.record_login_attempt("198.51.100.8", False) == (False, 0)
    auth.record_login_attempt("198.51.100.7", True)
    assert auth.record_login_attempt("198.51.100.7", False) == (False, 0)


def test_throttle_table_is_capped(monkeypatch):
    monkeypatch.setattr(auth, "_login_failures", {})
    monkeypatch.setattr(auth, "_LOGIN_MAX_CLIENTS", 3)
    for i in range(5):
        auth.record_login_attempt(f"client-{i}", False)
    assert list(auth._login_failures) == ["client-2", "client-3", "client-4"]


def test_a_locked_client_is_refused_before_its_password_is_checked(monkeypatch):
    """The lockout must not answer a right guess differently from a wrong one."""
    monkeypatch.setattr(auth, "_login_failures", {})
    for _ in range(auth._LOGIN_MAX_FAILURES):
        assert auth.login_lock("198.51.100.9") == (False, 0)
        auth.record_login_attempt("198.51.100.9", False)
    locked, retry = auth.login_lock("198.51.100.9")
    assert locked and retry > 0
    assert auth.login_lock("198.51.100.10") == (False, 0)              # other clients unaffected


def test_login_checks_the_lock_first():
    import pathlib
    src = pathlib.Path(__file__).with_name("main.py").read_text()
    body = src[src.index("def login("):src.index("def logout(")]
    assert body.index("login_lock(") < body.index("verify_password(")
    assert "if not locked:" in body                                     # the password is only checked when not locked


def test_sign_out_everywhere_ends_every_cookie(password):
    old_epoch = auth.session_epoch()
    try:
        token = auth.session_token()
        assert auth.session_valid(token)
        auth.set_session_epoch(old_epoch + 1)
        assert not auth.session_valid(token)                            # copies of the old cookie too
        assert auth.session_valid(auth.session_token())                 # signing in again works
    finally:
        auth.set_session_epoch(old_epoch)


def test_sign_out_revokes_that_cookie_only(password, monkeypatch):
    monkeypatch.setattr(auth, "_revoked", {})
    phone, laptop = auth.session_token(), auth.session_token()
    assert auth.session_valid(phone) and auth.session_valid(laptop)
    sid = auth.revoke_session(phone, now=1_000.0)
    assert sid and not auth.session_valid(phone)                        # a copy of it is no good either
    assert auth.session_valid(laptop)                                   # other devices stay signed in
    assert auth.revoked_sessions() == {sid: 1_000.0 + auth.COOKIE_MAX_AGE}
    assert auth.revoke_session("garbage") is None and auth.revoke_session(None) is None
    # Entries are dropped once the cookie would have expired anyway; the list never grows for good.
    auth.revoke_session(laptop, now=1_000.0 + auth.COOKIE_MAX_AGE + 1)
    assert sid not in auth.revoked_sessions() and not auth.session_valid(laptop)
    auth.set_revoked({"old": 5.0, "live": 10.0 ** 12}, now=100.0)
    assert auth.revoked_sessions() == {"live": 10.0 ** 12}


def test_a_cookie_without_a_session_id_is_refused(password, monkeypatch):
    """Version 1 cookies (before "Sign out" could revoke one) carry no id: they are signed out once."""
    monkeypatch.setattr(auth, "_revoked", {})
    old = URLSafeTimedSerializer(auth.SECRET_KEY).dumps({"v": 1, "p": auth._password_tag()})
    assert not auth.session_valid(old)
    no_id = URLSafeTimedSerializer(auth.SECRET_KEY).dumps({"v": auth.SESSION_VERSION, "p": auth._password_tag()})
    assert not auth.session_valid(no_id)
