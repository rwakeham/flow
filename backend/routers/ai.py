"""
/api/ai: the AI settings every feature shares (the Anthropic API key, whether AI calls are
on, each feature's model, the extra redact words: settings_store) and the AI privacy log,
every request and response in full. Card categorization's own progress stays under
/api/cards/ai/status.

A value still set in .env wins while it is there (setup.sh writes the key for the first
start): the dialog shows it as such and a change here is refused until it is removed.
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import card_ai
import settings_store as st
import change_log
from auth import require_auth
from database import get_db
from models import AiRequestLog

router = APIRouter(prefix="/api/ai", dependencies=[Depends(require_auth)])

MODEL_RE = re.compile(r"^[A-Za-z0-9._:@/-]{1,80}$")        # the same shape setup.sh accepted
REDACT_RE = re.compile(r"^[A-Za-z0-9 ,.'-]{0,500}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,400}$")


def _env_locked(var: str) -> HTTPException:
    return HTTPException(status_code=409, detail=st.env_locked_message(var))


def settings_payload() -> dict:
    """Every setting with its value and where it comes from, for the dialog's form."""
    out = {}
    for key, var in st.SETTING_ENV.items():
        out[key] = {"value": st.setting(key), "source": st.setting_source(key), "env_var": var}
    key = st.api_key()
    out["api_key"] = {"set": bool(key), "last4": key[-4:] if key else "", "source": st.api_key_source(),
                      "env_var": "ANTHROPIC_API_KEY", "unreadable": st.api_key_unreadable()}
    out["password"] = {"source": st.password_source(), "env_var": "FLOW_PASSWORD"}
    return out


@router.get("/status")
def ai_status(db: Session = Depends(get_db)):
    return {
        "enabled": card_ai.enabled(), "reason": card_ai.disabled_reason(),
        "problem": card_ai.key_problem(db) if card_ai.enabled() else None,
        "default_model": card_ai.DEFAULT_MODEL, "models": card_ai.models_in_use(),
        "settings": settings_payload(),
    }


class SettingsBody(BaseModel):
    ai_model: str | None = Field(default=None, max_length=80)
    ai_model_categorize: str | None = Field(default=None, max_length=80)
    ai_model_ask: str | None = Field(default=None, max_length=80)
    ai_enabled: bool | None = None
    ai_redact: str | None = Field(default=None, max_length=500)


def checked_settings(body: SettingsBody) -> dict[str, str]:
    """The settings to store, validated; a key left out of the body is left alone."""
    changes: dict[str, str] = {}
    for key in st.MODEL_SETTINGS:
        value = getattr(body, key)
        if value is None:
            continue
        value = value.strip()
        if value and not MODEL_RE.match(value):
            raise HTTPException(status_code=422, detail=f"A model name is letters, digits, dots and dashes "
                                                        f"(e.g. {card_ai.DEFAULT_MODEL}), not {value!r}.")
        changes[key] = value
    if body.ai_enabled is not None:
        changes[st.AI_ENABLED] = "1" if body.ai_enabled else "0"
    if body.ai_redact is not None:
        words = ", ".join(w.strip() for w in body.ai_redact.split(",") if w.strip())
        if not REDACT_RE.match(words):
            raise HTTPException(status_code=422, detail="Redact words are letters, digits, spaces and commas only.")
        changes[st.AI_REDACT] = words
    return changes


@router.put("/settings")
def put_settings(body: SettingsBody, db: Session = Depends(get_db)):
    changes = checked_settings(body)
    for key in changes:
        if st.setting_source(key) == "env":
            raise _env_locked(st.SETTING_ENV[key])
    if changes:
        change_log.record(db, "ai", "settings", "AI settings changed: " + ", ".join(sorted(changes)), {"keys": sorted(changes)})
    for key, value in changes.items():
        st.save_setting(db, key, value)          # commits (the change-log row with it)
    return ai_status(db)


class KeyBody(BaseModel):
    key: str = Field(max_length=400)


@router.put("/key")
def put_key(body: KeyBody, db: Session = Depends(get_db)):
    if st.api_key_source() == "env":
        raise _env_locked("ANTHROPIC_API_KEY")
    key = "".join(body.key.split())
    if not KEY_RE.match(key):
        raise HTTPException(status_code=422, detail="That doesn't look like an API key (letters, digits, - and _ only).")
    change_log.record(db, "ai", "key", "Anthropic API key saved")
    st.save_secret(db, st.API_KEY, key)          # commits (the change-log row with it)
    return ai_status(db)


@router.delete("/key")
def delete_key(db: Session = Depends(get_db)):
    if st.api_key_source() == "env":
        raise _env_locked("ANTHROPIC_API_KEY")
    change_log.record(db, "ai", "key_removed", "Anthropic API key removed")
    st.save_secret(db, st.API_KEY, None)         # commits (the change-log row with it)
    return ai_status(db)


@router.post("/key/test")
def test_key(db: Session = Depends(get_db)):
    """One tiny request with the configured key; its row in the privacy log says how it went."""
    if not st.api_key():
        raise HTTPException(status_code=409, detail=card_ai.disabled_reason() or "No API key is set.")
    ok, message = card_ai.test_key(db)
    return {"ok": ok, "message": message, "status": ai_status(db)}


@router.get("/log")
def ai_log(limit: int = 50, db: Session = Depends(get_db)):
    rows = db.query(AiRequestLog).order_by(AiRequestLog.id.desc()).limit(max(1, min(limit, 500))).all()
    return [{
        "id": r.id, "purpose": r.purpose, "model": r.model, "prompt_version": r.prompt_version,
        "item_count": r.item_count, "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
        "cache_read_tokens": r.cache_read_tokens, "status": r.status, "error": r.error,
        "error_kind": r.error_kind,
        "payload": json.loads(r.payload_json),
        "request": json.loads(r.request_json) if r.request_json else None,
        "response": json.loads(r.response_json) if r.response_json else None,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]
