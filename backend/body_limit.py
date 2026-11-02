"""
Request body size limit (a plain ASGI middleware).

Routes cap the files they read, but Starlette has already saved a multipart
upload to a temporary file by then, and a JSON body is read whole before
validation. So every request body is capped here first:

* a declared Content-Length over the limit is refused with 413 before any of
  the body is read;
* a body without one (chunked) is counted as it arrives and refused with 413
  once it passes the limit. FastAPI re-raises an HTTPException raised while it
  reads the body, so the client gets the same 413 either way.

The limit is DEFAULT_LIMIT, with larger limits for named paths (bulk import
plans carry several files at once).

Those limits are for a signed-in browser. FastAPI reads (and spools to disk) a
request body before its auth dependency runs, so without a valid session cookie
a request gets only PUBLIC_LIMIT (room for the login form) and anything larger
is refused with 401 before it is read: nobody who hasn't signed in can make the
server receive a large upload.
"""

from __future__ import annotations

import json
from typing import Callable

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from starlette.requests import cookie_parser

MB = 1024 * 1024
PUBLIC_LIMIT = 64 * 1024          # without a session: the login form and nothing large


def too_large_detail(limit: int) -> str:
    return f"Request too large (max {limit // MB} MB)"


def cookie_value(scope, name: str) -> str | None:
    """The named cookie from an ASGI scope's headers, or None. Starlette's own parser, the one
    behind request.cookies (so require_auth reads the same value): lenient, so another site's
    odd cookie on the same host can't hide Flow's."""
    for key, value in scope.get("headers", []):
        if key == b"cookie":
            return cookie_parser(value.decode("latin-1")).get(name)
    return None


class BodyLimit:
    def __init__(self, app, default: int, paths: dict[str, int] | None = None,
                 signed_in: Callable[[dict], bool] | None = None, public_limit: int = PUBLIC_LIMIT):
        self.app = app
        self.default = default
        self.paths = paths or {}
        self.signed_in = signed_in        # scope -> bool; None trusts every request (tests)
        self.public_limit = public_limit

    def limit_for(self, path: str) -> int:
        return self.paths.get(path.rstrip("/") or "/", self.default)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit, status = self.limit_for(scope.get("path", "")), 413
        if self.signed_in is not None and limit > self.public_limit and not self.signed_in(scope):
            limit, status = self.public_limit, 401
        declared = None
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
                break
        if declared is not None and declared > limit:
            await _refuse(send, limit, status)
            return

        received = 0

        async def counted_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(status_code=status, detail=_detail(limit, status))
            return message

        await self.app(scope, counted_receive, send)


async def too_many_rows(request, exc) -> JSONResponse:
    """Exception handler for import_profile.TooManyRows: a file with more rows than Flow reads is 413."""
    return JSONResponse(status_code=413, content={"detail": str(exc)})


def _detail(limit: int, status: int) -> str:
    return "Not authenticated" if status == 401 else too_large_detail(limit)


async def _refuse(send, limit: int, status: int = 413) -> None:
    body = json.dumps({"detail": _detail(limit, status)}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                            (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": body})
