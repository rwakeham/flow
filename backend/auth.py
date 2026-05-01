"""
Session-cookie authentication.

CSRF: state-changing endpoints rely on the session cookie's `samesite=lax`
attribute as the CSRF defense. Same-site lax blocks cross-site POSTs so a
malicious page cannot trigger mutating requests with the user's cookie. This
is acceptable for a single-user self-hosted app; a multi-user deployment
should add a CSRF token in addition.
"""

import hmac
import os
import threading
import time
from collections import deque

from fastapi import HTTPException, Request, Response
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

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

# Cookies are marked Secure unless explicitly disabled (e.g. local dev over HTTP).
COOKIE_SECURE = os.environ.get("COOKIE_INSECURE", "").lower() not in ("1", "true", "yes")

# Login throttle: max N failed attempts per IP within WINDOW seconds.
_LOGIN_MAX_FAILURES = 5
_LOGIN_WINDOW_SECONDS = 300  # 5 minutes
_login_failures: dict[str, deque[float]] = {}
_login_lock = threading.Lock()


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(SECRET_KEY)


def verify_password(submitted: str) -> bool:
    return bool(FLOW_PASSWORD) and hmac.compare_digest(submitted, FLOW_PASSWORD)


def record_login_attempt(client_ip: str, success: bool) -> tuple[bool, int]:
    """
    Record a login attempt and return (locked, retry_after_seconds).
    `locked` is True if the caller has hit the failure limit and the request
    should be rejected with 429. A successful attempt clears the IP's history.
    """
    now = time.monotonic()
    cutoff = now - _LOGIN_WINDOW_SECONDS
    with _login_lock:
        if success:
            _login_failures.pop(client_ip, None)
            return (False, 0)

        history = _login_failures.setdefault(client_ip, deque())
        while history and history[0] < cutoff:
            history.popleft()
        history.append(now)

        if len(history) > _LOGIN_MAX_FAILURES:
            retry_after = max(1, int(history[0] + _LOGIN_WINDOW_SECONDS - now))
            return (True, retry_after)
        return (False, 0)


def create_session_cookie(response: Response) -> None:
    token = _serializer().dumps("authenticated")
    response.set_cookie(
        COOKIE_NAME,
        token,
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
    try:
        _serializer().loads(token, max_age=COOKIE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=401, detail="Session expired or invalid")
