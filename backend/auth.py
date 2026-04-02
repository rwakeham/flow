import hmac
import os
from fastapi import HTTPException, Request, Response
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

FLOW_PASSWORD = os.environ.get("FLOW_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
COOKIE_NAME = "flow_session"
COOKIE_MAX_AGE = 86400 * 30  # 30 days


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(SECRET_KEY)


def verify_password(submitted: str) -> bool:
    return bool(FLOW_PASSWORD) and hmac.compare_digest(submitted, FLOW_PASSWORD)


def create_session_cookie(response: Response) -> None:
    token = _serializer().dumps("authenticated")
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
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
