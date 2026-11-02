"""
Request size limits: the body-size middleware (direct ASGI calls, including
through a real FastAPI route) and the caps on request lists. No DB, no network.
"""

from __future__ import annotations

import asyncio
import json
import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-" * 8)

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from pydantic import BaseModel, ValidationError  # noqa: E402

from body_limit import BodyLimit  # noqa: E402


def call(app, path="/", body=b"", chunks=None, declare=True, method="POST", content_type=b"application/json",
         cookie=None):
    """Run one request through an ASGI app; returns (status, json body, how many body chunks the app read)."""
    parts = chunks if chunks is not None else [body]
    headers = [(b"content-type", content_type)]
    if cookie is not None:
        headers.append((b"cookie", cookie.encode()))
    if declare:
        headers.append((b"content-length", str(sum(len(p) for p in parts)).encode()))
    scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(), "query_string": b"",
             "headers": headers, "http_version": "1.1", "scheme": "http", "server": ("t", 80), "client": ("c", 1),
             "root_path": ""}
    queue = [{"type": "http.request", "body": p, "more_body": i < len(parts) - 1} for i, p in enumerate(parts)]
    read = []
    sent = []

    async def receive():
        if queue:
            read.append(1)
            return queue.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body_out = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], (json.loads(body_out) if body_out else None), len(read)


class Body(BaseModel):
    data: str


def fastapi_app(default=100, paths=None, signed_in=None, public_limit=None):
    app = FastAPI()

    @app.post("/echo")
    def echo(body: Body):
        return {"n": len(body.data)}

    @app.post("/big")
    def big(body: Body):
        return {"n": len(body.data)}

    extra = {"signed_in": signed_in} if signed_in else {}
    if public_limit is not None:
        extra["public_limit"] = public_limit
    app.add_middleware(BodyLimit, default=default, paths=paths or {}, **extra)
    return app


def payload(n):
    return json.dumps({"data": "x" * n}).encode()


def test_small_bodies_pass():
    status, out, _ = call(fastapi_app(), "/echo", payload(10))
    assert status == 200 and out == {"n": 10}


def test_declared_oversize_body_is_refused_before_it_is_read():
    status, out, reads = call(fastapi_app(), "/echo", payload(500))
    assert status == 413 and "too large" in out["detail"]
    assert reads == 0


def test_undeclared_body_is_counted_as_it_arrives():
    chunks = [payload(500)[i:i + 50] for i in range(0, len(payload(500)), 50)]
    status, out, reads = call(fastapi_app(), "/echo", chunks=chunks, declare=False)
    assert status == 413 and "too large" in out["detail"]       # FastAPI passes the 413 through, not a 400
    assert reads < len(chunks)                                  # stopped reading once over the limit


def test_named_paths_get_their_own_limit():
    app = fastapi_app(default=100, paths={"/big": 1000})
    assert call(app, "/big", payload(500))[0] == 200
    assert call(app, "/echo", payload(500))[0] == 413
    assert call(app, "/big/", payload(500))[0] != 413         # a trailing slash is the same path


def test_non_http_scopes_pass_through():
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    asyncio.run(BodyLimit(inner, default=1)({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_the_app_wires_the_limits():
    import pathlib
    src = pathlib.Path(__file__).with_name("main.py").read_text()
    assert "app.add_middleware(BodyLimit" in src
    assert src.index("app.add_middleware(BodyLimit") < src.index("app.add_middleware(SecurityHeaders)")
    assert '"/api/import/batch/plan"' in src


# ── Caps on request lists ────────────────────────────────────────────────────

def _too_long(model, **fields):
    with pytest.raises(ValidationError, match="at most"):
        model(**fields)


def test_list_caps():
    from routers import backup, balances, cards, data, import_profiles, register
    _too_long(cards.ReviewBody, items=[{"txn_id": i} for i in range(10_001)])
    _too_long(cards.ReviewMark, txn_id=1, codes=["large"] * 21)
    _too_long(register.BulkAddToLedgerBody, bank_txn_ids=list(range(10_001)))
    _too_long(backup.DeleteBackupsBody, ids=["x"] * 1001)
    row = {"account_id": 1, "value": 1.0}
    _too_long(balances.PreviewBody, period="2026-11-01", entries=[{"amount": 1.0}] * 1001)
    _too_long(balances.ApplyBody, period="2026-11-01", rows=[row] * 1001)
    _too_long(data.AccountCreate, name="x", account_type="asset", last4=["1234"] * 51)
    _too_long(data.AccountUpdate, last4=["1234"] * 51)
    _too_long(import_profiles.ProfileBody, name="x", spec={}, link=[{"kind": "cash", "account_id": 1}] * 501)
    # what the UI sends stays well inside
    assert len(cards.ReviewBody(items=[{"txn_id": i} for i in range(5000)]).items) == 5000
    assert register.BulkAddToLedgerBody(bank_txn_ids=list(range(5000)))


def test_card_account_filter_is_capped():
    from fastapi import HTTPException

    from routers import cards
    assert cards._parse_ids("1,2,3", None) == [1, 2, 3]
    with pytest.raises(HTTPException) as e:
        cards._parse_ids(",".join(str(i) for i in range(cards.MAX_ACCOUNT_IDS + 1)), None)
    assert e.value.status_code == 422


def test_too_many_rows_is_a_413_through_fastapi():
    import import_profile as ip
    from body_limit import too_many_rows

    app = FastAPI()

    @app.post("/read")
    def read():
        raise ip.TooManyRows(ip.MAX_ROWS)

    app.add_exception_handler(ip.TooManyRows, too_many_rows)
    status, out, _ = call(app, "/read", b"{}")
    assert status == 413 and "50,000 rows" in out["detail"]
    import pathlib
    assert "app.add_exception_handler(import_profile.TooManyRows, too_many_rows)" in \
        pathlib.Path(__file__).with_name("main.py").read_text()


# ── Without a session, only a small body is accepted ─────────────────────────

def _signed_in(scope):
    from body_limit import cookie_value
    return cookie_value(scope, "flow_session") == "good"


def test_a_signed_out_request_cannot_send_a_large_body():
    app = fastapi_app(default=10_000, signed_in=_signed_in, public_limit=200)
    status, body, read = call(app, "/echo", payload(5_000))
    assert status == 401 and body == {"detail": "Not authenticated"} and read == 0      # refused before reading
    status, _, _ = call(app, "/echo", chunks=[payload(150)[:100], payload(150)[100:] + b" " * 300], declare=False)
    assert status == 401                                                                # chunked: once past it
    assert call(app, "/echo", payload(50))[0] == 200                                    # a login-sized body is fine
    assert call(app, "/echo", payload(5_000), cookie="other=1; flow_session=bad")[0] == 401


def test_a_signed_in_request_gets_the_normal_limit():
    app = fastapi_app(default=10_000, signed_in=_signed_in, public_limit=200)
    assert call(app, "/echo", payload(5_000), cookie="flow_session=good")[0] == 200
    assert call(app, "/echo", payload(20_000), cookie="flow_session=good")[0] == 413


def test_the_app_checks_the_real_session_cookie():
    import auth
    from body_limit import cookie_value
    token = auth.session_token()
    scope = {"headers": [(b"cookie", f"theme=dark; {auth.COOKIE_NAME}={token}".encode())]}
    assert auth.session_valid(cookie_value(scope, auth.COOKIE_NAME))
    odd = f"bad cookie; x={{y; {auth.COOKIE_NAME}={token}".encode()          # another site's odd cookie first
    assert cookie_value({"headers": [(b"cookie", odd)]}, auth.COOKIE_NAME) == token
    assert cookie_value({"headers": []}, auth.COOKIE_NAME) is None
    import pathlib
    src = pathlib.Path(__file__).with_name("main.py").read_text()
    assert "signed_in=lambda scope: session_valid(cookie_value(scope, COOKIE_NAME))" in src
