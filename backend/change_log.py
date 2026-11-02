"""
The change log: one row per change to Flow's data (imports, restores, edits, rule and settings
changes, applied Assistant proposals), shown under Settings → Change log. ``record`` adds the row
to the route's session, so it is saved with the change by the route's own commit and never
without it. ``via`` says who did it: "ui" (the user, in Flow), "assistant" (the browser applying
an Assistant proposal sends the X-Flow-Via header), "schedule" (the backup loop, an AI run) or
"startup" (a migration). The header is read by ``read_via``, an app-wide dependency that MUST stay
``async``: a sync dependency runs in a threadpool with a copy of the context, so a contextvar set
there never reaches the route, while an async one runs in the request's task and the sync route
inherits it. Background and startup writers pass ``via`` explicitly. The table stays out of
backups (backup_service) and is capped at MAX_ROWS (``prune``, from the backup loop).
"""
from __future__ import annotations

import json
from contextvars import ContextVar

from fastapi import Header
from sqlalchemy import text
from sqlalchemy.orm import Session

from models import ChangeLog

VIA = ("ui", "assistant", "schedule", "startup")
HEADER = "X-Flow-Via"
MAX_ROWS = 20_000
CLIP = 60

_via: ContextVar[str] = ContextVar("flow_via", default="ui")


async def read_via(x_flow_via: str | None = Header(None)) -> None:
    """App-wide dependency: who is making this request, from the X-Flow-Via header (allowed values
    only; anything else is the user). Async on purpose: see the module docstring."""
    _via.set(x_flow_via if x_flow_via in VIA else "ui")


def current_via() -> str:
    return _via.get()


def clip(value, n: int = CLIP) -> str:
    """A name for a summary line: one line, at most ``n`` characters."""
    s = " ".join(str(value or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def money(amount) -> str:
    try:
        return f"{float(amount):,.2f}"
    except (TypeError, ValueError):
        return str(amount)


def record(db: Session, kind: str, action: str, summary: str, detail: dict | None = None, via: str | None = None) -> ChangeLog:
    """Add one row to the session; the caller's commit saves it with the change it describes."""
    row = ChangeLog(kind=kind[:16], action=action[:16], summary=clip(summary, 300),
                    detail_json=json.dumps(detail, default=str) if detail else None,
                    via=via if via in VIA else current_via())
    db.add(row)
    return row


def prune(db: Session, keep: int = MAX_ROWS) -> int:
    """Delete everything but the newest ``keep`` rows (by id); returns how many went."""
    cutoff = db.execute(text("SELECT id FROM change_log ORDER BY id DESC OFFSET :keep LIMIT 1"),
                        {"keep": keep}).scalar()
    if cutoff is None:
        return 0
    n = db.execute(text("DELETE FROM change_log WHERE id <= :cutoff"), {"cutoff": cutoff}).rowcount
    db.commit()
    return n or 0


def rows_json(rows: list[ChangeLog]) -> list[dict]:
    out = []
    for r in rows:
        try:
            detail = json.loads(r.detail_json) if r.detail_json else None
        except ValueError:
            detail = None
        out.append({"id": r.id, "created_at": r.created_at.isoformat() if r.created_at else None, "kind": r.kind,
                    "action": r.action, "summary": r.summary, "via": r.via, "detail": detail})
    return out
