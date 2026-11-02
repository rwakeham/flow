"""GET /api/changes: the change log (change_log.py), newest first, a page at a time (keyset by id)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

import change_log
from auth import require_auth
from database import get_db
from models import ChangeLog

router = APIRouter(prefix="/api/changes", dependencies=[Depends(require_auth)])


@router.get("")
def list_changes(limit: int = Query(100, ge=1, le=500), before: int | None = Query(None, ge=1),
                 db: Session = Depends(get_db)):
    q = db.query(ChangeLog)
    if before is not None:
        q = q.filter(ChangeLog.id < before)
    rows = q.order_by(ChangeLog.id.desc()).limit(limit).all()
    return {"items": change_log.rows_json(rows), "more": len(rows) == limit}
