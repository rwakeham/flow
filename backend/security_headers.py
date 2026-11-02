"""
Security headers on every HTTP response, and the same-origin check on every
state-changing request (plain ASGI middlewares, so the Assistant's streamed
answers pass through untouched).

The Content-Security-Policy still allows inline script and style, because
index.html's app script and many onclick handlers are inline; moving them into
/static would let 'unsafe-inline' go. Everything, Chart.js included, is served by Flow itself.

``SameOrigin``: the session cookie's SameSite=Lax keeps a page on another *site*
from posting with it, but to a browser another port on the same host name is the
same site, so another web app on the machine could. A POST, PUT, PATCH or DELETE
is therefore refused (403) when the browser says it came from elsewhere:
``Sec-Fetch-Site`` other than ``same-origin`` / ``none`` (every current browser
sends it), or failing that an ``Origin`` (or ``Referer``) whose host isn't the
``Host`` the request was addressed to (or ``X-Forwarded-Host``, for a reverse
proxy that rewrites Host). A request with none of those headers (curl, a
script) is not a browser carrying a cookie across origins, and passes.
"""

from __future__ import annotations

import json

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SAME_SITE_OK = {b"same-origin", b"none"}
CROSS_SITE_DETAIL = "Cross-site request refused"


def _host_of(url: bytes) -> bytes | None:
    """The lower-cased host[:port] of an absolute URL, or None for "null" and anything else."""
    url = url.strip().lower()
    scheme, sep, rest = url.partition(b"://")
    if not sep or not scheme.isalpha():
        return None
    host = rest.split(b"/", 1)[0].split(b"?", 1)[0].split(b"#", 1)[0]
    if b"@" in host:
        host = host.rsplit(b"@", 1)[1]
    return host or None


def cross_site(method: str, headers) -> bool:
    """True when a state-changing request came from another origin, by the browser's own account."""
    if method.upper() in SAFE_METHODS:
        return False
    h = {k.lower(): v for k, v in headers}
    site = h.get(b"sec-fetch-site")
    if site is not None:
        return site.strip().lower() not in SAME_SITE_OK
    source = h.get(b"origin") or h.get(b"referer")
    if source is None:
        return False
    host = _host_of(source)
    if host is None:
        return True
    targets = {v.strip().lower() for v in (h.get(b"host"), h.get(b"x-forwarded-host")) if v}
    return host not in targets


class SameOrigin:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and cross_site(scope.get("method", "GET"), scope.get("headers", [])):
            body = json.dumps({"detail": CROSS_SITE_DETAIL}).encode()
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])

HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"x-frame-options", b"DENY"),
]
_NAMES = {name for name, _ in HEADERS}


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                kept = [(k, v) for k, v in message.get("headers", []) if k.lower() not in _NAMES]
                message = {**message, "headers": kept + HEADERS}
            await send(message)

        await self.app(scope, receive, send_with_headers)
