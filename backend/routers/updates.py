"""
/api/version: the running version (backend/VERSION) and the update check (update_check.py),
as the Sign out ▾ menu shows them. Signed in only: /api/health stays bare, so an
unauthenticated caller can't read which version (and so which known fixes) a server runs.

Every reply has the same shape, {"version", "update": update_check.status()}, so the menu
redraws from any of them; a check by hand adds "ok" and "message".
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import settings_store as st
import update_check
from auth import require_auth
from database import get_db
from version import APP_VERSION

router = APIRouter(prefix="/api/version", dependencies=[Depends(require_auth)])


def payload() -> dict:
    return {"version": APP_VERSION, "update": update_check.status()}


@router.get("")
def get_version():
    return payload()


@router.post("/check")
def check_now(db: Session = Depends(get_db)):
    """Sign out ▾ → Check for updates: one check now, whatever the daily switch says."""
    ok, message = update_check.run_check(db)
    return {**payload(), "ok": ok, "message": message}


class DismissBody(BaseModel):
    version: str = Field(max_length=32)


@router.post("/dismiss")
def dismiss(body: DismissBody, db: Session = Depends(get_db)):
    """Hide the notice for this release until a newer one appears."""
    if not update_check.dismiss(db, body.version):
        raise HTTPException(status_code=422, detail="That is not a version.")
    return payload()


class SettingsBody(BaseModel):
    check: bool


@router.put("/settings")
def put_settings(body: SettingsBody, db: Session = Depends(get_db)):
    """The daily switch; refused while FLOW_UPDATE_CHECK in .env decides it."""
    if st.setting_source(st.UPDATE_CHECK) == "env":
        raise HTTPException(status_code=409, detail="FLOW_UPDATE_CHECK is set in .env and wins while it is there. "
                                                    "Remove it from .env (./setup.sh offers to) and run ./deploy.sh, "
                                                    "then change it here.")
    st.save_setting(db, st.UPDATE_CHECK, "1" if body.check else "0")
    return payload()
