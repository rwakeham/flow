"""The security headers middleware and the app without public API docs. No DB, no network."""

from __future__ import annotations

import asyncio

import pytest

import security_headers as sh


def run(app_headers):
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": app_headers})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message):
        sent.append(message)

    asyncio.run(sh.SecurityHeaders(app)({"type": "http"}, None, send))
    return dict(sent[0]["headers"]), sent[1]


def test_headers_are_added_and_the_body_passes_through():
    headers, body = run([(b"content-type", b"text/html")])
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"x-frame-options"] == b"DENY"
    assert headers[b"referrer-policy"] == b"same-origin"
    assert b"frame-ancestors 'none'" in headers[b"content-security-policy"]
    assert headers[b"content-type"] == b"text/html" and body["body"] == b"ok"


def test_a_header_the_app_set_is_replaced_not_duplicated():
    sent_headers, _ = run([(b"X-Frame-Options", b"SAMEORIGIN")])
    assert sent_headers.get(b"X-Frame-Options") is None and sent_headers[b"x-frame-options"] == b"DENY"


def test_non_http_scopes_are_untouched():
    called = []

    async def app(scope, receive, send):
        called.append(scope["type"])

    asyncio.run(sh.SecurityHeaders(app)({"type": "lifespan"}, None, None))
    assert called == ["lifespan"]


def test_csp_allows_only_what_the_page_loads():
    assert "default-src 'self'" in sh.CSP
    assert "http" not in sh.CSP and "*" not in sh.CSP.replace("'", "")      # no outside sources at all


# ── SameOrigin: state-changing requests from another origin are refused ──────

def _hdrs(**kw):
    return [(k.replace("_", "-").encode(), v.encode()) for k, v in kw.items()]


@pytest.mark.parametrize("method,headers,refused", [
    ("GET", _hdrs(sec_fetch_site="cross-site", origin="http://evil.example"), False),   # reads are not state changes
    ("POST", _hdrs(sec_fetch_site="same-origin"), False),
    ("POST", _hdrs(sec_fetch_site="none"), False),                                        # typed into the address bar
    ("POST", _hdrs(sec_fetch_site="same-site", host="flow.local:8000", origin="http://flow.local:9000"), True),  # another port on the host
    ("POST", _hdrs(sec_fetch_site="cross-site"), True),
    ("DELETE", _hdrs(sec_fetch_site="cross-site"), True),
    # Older browsers: Origin against Host
    ("POST", _hdrs(host="flow.local:8000", origin="http://flow.local:8000"), False),
    ("POST", _hdrs(host="flow.local:8000", origin="HTTP://Flow.Local:8000/"), False),
    ("POST", _hdrs(host="flow.local:8000", origin="http://flow.local:9000"), True),
    ("POST", _hdrs(host="flow.local:8000", origin="null"), True),                          # sandboxed or data: page
    ("POST", _hdrs(host="flow.local:8000", referer="http://flow.local:8000/"), False),
    ("POST", _hdrs(host="flow.local:8000", referer="http://other.local/page"), True),
    ("POST", _hdrs(host="app:8000", x_forwarded_host="flow.example.com", origin="https://flow.example.com"), False),  # proxy rewrote Host
    ("POST", _hdrs(host="flow.local:8000"), False),                                       # curl: no browser context
])
def test_cross_site(method, headers, refused):
    assert sh.cross_site(method, headers) is refused


def test_same_origin_middleware_refuses_with_403_and_passes_the_rest():
    sent, called = [], []

    async def app(scope, receive, send):
        called.append(scope.get("method", scope["type"]))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message):
        sent.append(message)

    mw = sh.SameOrigin(app)
    asyncio.run(mw({"type": "http", "method": "POST", "headers": _hdrs(sec_fetch_site="cross-site")}, None, send))
    assert called == [] and sent[0]["status"] == 403 and b"Cross-site" in sent[1]["body"]
    sent.clear()
    asyncio.run(mw({"type": "http", "method": "POST", "headers": _hdrs(sec_fetch_site="same-origin")}, None, send))
    assert called == ["POST"] and sent[0]["status"] == 200
    asyncio.run(mw({"type": "lifespan"}, None, send))                     # non-HTTP scopes pass straight through
    assert called[-1] == "lifespan"


def test_the_app_installs_same_origin_outside_the_body_limit():
    import pathlib
    src = pathlib.Path(__file__).with_name("main.py").read_text()
    assert src.index("add_middleware(BodyLimit") < src.index("add_middleware(SameOrigin)") < src.index("add_middleware(SecurityHeaders)")
