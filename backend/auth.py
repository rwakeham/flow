"""
Session-cookie authentication.

CSRF: the session cookie is `samesite=lax`, so a page on another site can't
make the browser send it with a POST; and `security_headers.SameOrigin` refuses
a state-changing request whose `Sec-Fetch-Site` (or `Origin`) says it came from
another origin, which covers a page on the same host name but another port
(the same *site* to a browser, so Lax alone lets it through).

The password lives in the database as a scrypt hash (settings_store; FLOW_PASSWORD
in .env sets it on the first start and resets it when set later). Sessions end
when it changes: the signed cookie carries a keyed hash of the current password
hash (`_password_tag`), and `require_auth` compares it with the hash the app runs
with now. The tag is an HMAC under SECRET_KEY, so the cookie (signed, not
encrypted) gives nothing to brute-force a password with.
The tag also covers a session generation (`set_session_epoch`, stored in
flow_settings): "Sign out everywhere" bumps it, which ends every existing cookie
at once, including copies made before a sign-out. Each cookie also carries a
random session id (`s`): "Sign out" revokes that one id (`revoke_session`, the
list kept in flow_settings until the cookies would have expired anyway), so a
copy of a signed-out cookie is no longer valid, while other devices stay in.

Startup refuses a missing or placeholder SECRET_KEY, a placeholder FLOW_PASSWORD,
and an install with no password anywhere (`check_settings` and
`require_password`, called from main.py's lifespan; deploy.sh runs the same
checks before building).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
import threading
import time
from collections import deque

from fastapi import HTTPException, Request, Response
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

import settings_store

# The first start's password, and a reset when set later (settings_store.import_from_env copies
# it into the database); blank once the password is kept in Flow.
FLOW_PASSWORD = os.environ.get("FLOW_PASSWORD", "")

try:
    SECRET_KEY = os.environ["SECRET_KEY"]
except KeyError as exc:
    raise RuntimeError(
        "SECRET_KEY environment variable is required. "
        "Generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
    ) from exc

COOKIE_NAME = "flow_session"
COOKIE_MAX_AGE = 86400 * 30  # 30 days
SESSION_VERSION = 2          # 2: the cookie carries a session id that "Sign out" can revoke

# Cookies are marked Secure only when explicitly enabled (e.g. behind HTTPS).
# Default off so the app works out of the box over plain HTTP.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").lower() in ("1", "true", "yes")

# Values shipped in .env.example (and earlier versions of it). Keep deploy.sh's
# PLACEHOLDERS list in step with these.
PLACEHOLDER_SECRETS = {"replacewithsecretrandomhex", "changeme"}
PLACEHOLDER_PASSWORDS = {"mysecretpassword", "changeme", "password"}
MIN_SECRET_LENGTH = 32

# Login throttle: max N failed attempts per client within WINDOW seconds.
_LOGIN_MAX_FAILURES = 5
_LOGIN_WINDOW_SECONDS = 300  # 5 minutes
_LOGIN_MAX_CLIENTS = 10_000  # oldest entries are dropped past this
_login_failures: dict[str, deque[float]] = {}
_login_lock = threading.Lock()


class SettingsError(RuntimeError):
    pass


def check_settings(secret_key: str | None = None, password: str | None = None) -> None:
    """Refuse to start with a short or placeholder SECRET_KEY, or a placeholder FLOW_PASSWORD (a blank
    one is fine: the password is kept in the database once Flow has started; see require_password)."""
    secret_key = SECRET_KEY if secret_key is None else secret_key
    password = FLOW_PASSWORD if password is None else password
    problems = []
    if secret_key.strip().lower() in PLACEHOLDER_SECRETS or len(secret_key) < MIN_SECRET_LENGTH:
        problems.append(f"SECRET_KEY must be a random value of at least {MIN_SECRET_LENGTH} characters "
                        "(generate one with: openssl rand -hex 32)")
    if password and password.strip().lower() in PLACEHOLDER_PASSWORDS:
        problems.append("FLOW_PASSWORD is still the example value")
    if problems:
        raise SettingsError("; ".join(problems) + ". Run ./setup.sh to fix .env, then ./deploy.sh.")


def password_problem(password: str) -> str | None:
    """Why a password can't be set (blank or an example value), or None."""
    if not password:
        return "The password is blank"
    if password.strip().lower() in PLACEHOLDER_PASSWORDS:
        return "That is the example password; choose another"
    return None


def require_password(stored_hash: str, env_password: str | None = None) -> None:
    """After the first start's import: refuse to run with no password anywhere."""
    env_password = FLOW_PASSWORD if env_password is None else env_password
    if not stored_hash and not env_password:
        raise SettingsError("No dashboard password: Flow has none stored and FLOW_PASSWORD is not set in .env. "
                            "Run ./setup.sh (it asks for one), then ./deploy.sh.")


# ── Trusted proxies ───────────────────────────────────────────────────────────

def parse_proxies(value: str) -> list:
    """FLOW_TRUSTED_PROXIES: comma-separated IPs or CIDR ranges. Bad entries are an error."""
    nets = []
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError as exc:
            raise SettingsError(f"FLOW_TRUSTED_PROXIES: {part!r} is not an IP address or range") from exc
    return nets


TRUSTED_PROXIES = parse_proxies(os.environ.get("FLOW_TRUSTED_PROXIES", ""))


def _trusted(ip: str, proxies) -> bool:
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    return any(addr in net for net in proxies)


def client_ip(peer: str | None, forwarded_for: str | None, proxies=None) -> str:
    """
    The client a login attempt is counted against. The direct peer, unless it is a
    trusted proxy: then the right-most X-Forwarded-For entry that isn't one (entries
    to its left can be written by anyone, so they are never believed).
    """
    proxies = TRUSTED_PROXIES if proxies is None else proxies
    peer = peer or "unknown"
    if not proxies or not forwarded_for or not _trusted(peer, proxies):
        return peer
    hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
    for hop in reversed(hops):
        if not _trusted(hop, proxies):
            return hop
    return hops[0] if hops else peer


def request_client_ip(request: Request) -> str:
    return client_ip(request.client.host if request.client else None, request.headers.get("x-forwarded-for"))


# ── Passwords and sessions ────────────────────────────────────────────────────

def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(SECRET_KEY)


_session_epoch = 0      # loaded from flow_settings at startup; one uvicorn worker, so one copy


def set_session_epoch(epoch: int) -> None:
    global _session_epoch
    _session_epoch = int(epoch)


def session_epoch() -> int:
    return _session_epoch


def password_hash() -> str:
    """The stored scrypt hash of the dashboard password (settings_store), "" before the first start."""
    return settings_store.password_hash()


def _password_tag(stored_hash: str | None = None, epoch: int | None = None) -> str:
    stored_hash = password_hash() if stored_hash is None else stored_hash
    epoch = _session_epoch if epoch is None else epoch
    msg = f"flow-session:{epoch}:".encode() + stored_hash.encode()
    return hmac.new(SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()[:16]


def verify_password(submitted: str) -> bool:
    stored = password_hash()
    return bool(stored) and settings_store.check_password(submitted, stored)


def login_lock(client: str) -> tuple[bool, int]:
    """
    (locked, retry_after_seconds) for a client, without recording anything. Checked BEFORE the
    password: a locked-out client gets 429 whatever it sends, so the lockout can't be used to
    tell a right guess (which used to get 200) from a wrong one (429).
    """
    now = time.monotonic()
    cutoff = now - _LOGIN_WINDOW_SECONDS
    with _login_lock:
        history = _login_failures.get(client)
        if not history:
            return (False, 0)
        while history and history[0] < cutoff:
            history.popleft()
        if len(history) >= _LOGIN_MAX_FAILURES:
            return (True, max(1, int(history[0] + _LOGIN_WINDOW_SECONDS - now)))
        return (False, 0)


def record_login_attempt(client: str, success: bool) -> tuple[bool, int]:
    """
    Record a login attempt and return (locked, retry_after_seconds).
    `locked` is True if the caller has hit the failure limit and the request
    should be rejected with 429. A successful attempt clears the client's history.
    """
    now = time.monotonic()
    cutoff = now - _LOGIN_WINDOW_SECONDS
    with _login_lock:
        if success:
            _login_failures.pop(client, None)
            return (False, 0)

        history = _login_failures.get(client)
        if history is None:
            while len(_login_failures) >= _LOGIN_MAX_CLIENTS:
                _login_failures.pop(next(iter(_login_failures)))   # oldest first (insertion order)
            history = _login_failures[client] = deque()
        while history and history[0] < cutoff:
            history.popleft()
        history.append(now)

        if len(history) > _LOGIN_MAX_FAILURES:
            retry_after = max(1, int(history[0] + _LOGIN_WINDOW_SECONDS - now))
            return (True, retry_after)
        return (False, 0)


def session_token() -> str:
    return _serializer().dumps({"v": SESSION_VERSION, "p": _password_tag(), "s": secrets.token_urlsafe(12)})


def _load(token: str | None) -> dict | None:
    """The cookie's payload if the signature and age check out (whatever the password tag says)."""
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return data if isinstance(data, dict) and data.get("v") == SESSION_VERSION else None


def session_valid(token: str | None) -> bool:
    data = _load(token)
    return (data is not None and isinstance(data.get("p"), str) and hmac.compare_digest(data["p"], _password_tag())
            and isinstance(data.get("s"), str) and data["s"] not in _revoked)


# ── Revoked sessions ("Sign out") ─────────────────────────────────────────────
# session id -> the wall-clock time its cookie would have expired anyway; entries past that are
# dropped. Loaded from flow_settings at startup (one uvicorn worker, so one copy), saved by main.py
# after each sign-out. "Sign out everywhere" bumps the epoch instead, which ends every id at once.

_revoked: dict[str, float] = {}


def set_revoked(entries: dict[str, float] | None, now: float | None = None) -> None:
    global _revoked
    now = time.time() if now is None else now
    _revoked = {str(s): float(t) for s, t in (entries or {}).items() if float(t) > now}


def revoked_sessions() -> dict[str, float]:
    return dict(_revoked)


def revoke_session(token: str | None, now: float | None = None) -> str | None:
    """End this cookie's session: its id is refused from now on, until the cookie's own expiry.
    Returns the id, or None for a cookie that isn't a valid session (nothing to revoke)."""
    now = time.time() if now is None else now
    data = _load(token)
    for sid, until in list(_revoked.items()):
        if until <= now:
            del _revoked[sid]
    if data is None or not isinstance(data.get("s"), str):
        return None
    _revoked[data["s"]] = now + COOKIE_MAX_AGE
    return data["s"]


def create_session_cookie(response: Response) -> None:
    response.set_cookie(
        COOKIE_NAME,
        session_token(),
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        max_age=COOKIE_MAX_AGE,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME)


def require_auth(request: Request) -> None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not session_valid(token):
        raise HTTPException(status_code=401, detail="Session expired or invalid")
