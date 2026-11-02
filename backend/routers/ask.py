"""
/api/ask: the Ask chat panel.

POST /api/ask streams NDJSON events (status, text, reset, then one of final,
declined or error). The browser keeps the conversation; the server stores only
a question count per day (shown per month), plus the AI privacy log rows for every call.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

import ask
import ask_data
import ask_files
import card_ai
from auth import require_auth
from database import SessionLocal, get_db
from models import AiRequestLog

router = APIRouter(prefix="/api/ask", dependencies=[Depends(require_auth)])
MAX_ATTACH_BYTES = 10 * 1024 * 1024
log = logging.getLogger("flow.ask")

# ── Monthly count ────────────────────────────────────────────────────────────
# Stored per day; shown per calendar month. There is no limit: the count is for
# keeping an eye on use, and every call's tokens are in the AI privacy log.

def _bump(db: Session, delta: int) -> int:
    n = db.execute(text("""
        INSERT INTO ask_usage (day, count) VALUES (:d, GREATEST(:n, 0))
        ON CONFLICT (day) DO UPDATE SET count = GREATEST(ask_usage.count + :n, 0)
        RETURNING count
    """), {"d": date.today(), "n": delta}).scalar()
    db.commit()
    return int(n)


def _month_total(db: Session, first: date) -> int:
    nxt = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    return int(db.execute(text("SELECT COALESCE(SUM(count), 0) FROM ask_usage WHERE day >= :a AND day < :b"),
                          {"a": first, "b": nxt}).scalar() or 0)


def meter(db: Session) -> dict:
    this = date.today().replace(day=1)
    last = date(this.year - (this.month == 1), (this.month - 2) % 12 + 1, 1)
    return {"month": this.isoformat()[:7], "this_month": _month_total(db, this),
            "last_month": _month_total(db, last)}


# ── Logging ──────────────────────────────────────────────────────────────────

def make_logger(db: Session):
    """The ask.LogFn: one ai_request_log row per API call, request and response in full."""
    def log(purpose: str, kwargs: dict, resp, exc, meta: dict) -> None:
        row = AiRequestLog(purpose=purpose, model=kwargs.get("model") or card_ai.model_name(card_ai.TASK_ASK),
                           prompt_version=ask.PROMPT_VERSION, payload_json=json.dumps(card_ai._jsonable(meta), sort_keys=True),
                           item_count=int(meta.get("round", 1)), status="error",
                           key_fingerprint=card_ai.key_fingerprint())
        card_ai.record_request(row, kwargs)
        card_ai.record_response(row, resp)
        if exc is not None:
            message, _fatal, kind = card_ai._describe_error(exc, card_ai.TASK_ASK)
            card_ai._finish_log(row, status="error", error=message, kind=kind)
        else:
            refused = getattr(resp, "stop_reason", None) == "refusal"
            card_ai._finish_log(row, usage=getattr(resp, "usage", None), status="refusal" if refused else "ok")
        db.add(row)
        db.commit()
    return log


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/status")
def ask_status(db: Session = Depends(get_db)):
    problem = card_ai.key_problem(db) if card_ai.enabled() else None
    return {"enabled": card_ai.enabled(), "reason": card_ai.disabled_reason(), "model": card_ai.model_name(card_ai.TASK_ASK),
            "paused": problem["message"] if problem else None, "areas": card_ai.areas_enabled(), **meter(db)}


NO_AREAS = "Every data area is switched off for the AI under Settings → AI, so the Assistant has nothing to answer from."


def _line(ev: dict) -> bytes:
    return (json.dumps(ev) + "\n").encode()


@router.post("/attach")
async def attach_file(file: UploadFile = File(...), kind: str | None = Form(None)):
    """
    Read an attached statement file here, on this server: the outline and draft summary
    the model may see, and the full draft the browser keeps for the format editor.
    Nothing is stored and nothing is sent anywhere.
    """
    content = await file.read(MAX_ATTACH_BYTES + 1)
    if len(content) > MAX_ATTACH_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
    try:
        return ask_files.summarize(content, file.filename or "upload", kind or None)
    except Exception as exc:  # noqa: BLE001 - any unreadable file gets the same answer
        raise HTTPException(status_code=422, detail=f"Could not read this file as a statement: {exc}")


@router.post("")
async def ask_question(request: Request):
    if not card_ai.enabled():
        raise HTTPException(status_code=409, detail=card_ai.disabled_reason())
    if not any(card_ai.areas_enabled().values()):
        raise HTTPException(status_code=409, detail=NO_AREAS)
    try:
        body = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    try:
        history = ask.validate_history(body.get("messages"))
    except ask.AskError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    page = body.get("page") if isinstance(body.get("page"), dict) else {}
    try:
        attachment = ask_files.checked_attachment(body.get("attachment"))
    except ask_files.AttachmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return StreamingResponse(_stream(history, page, attachment), media_type="application/x-ndjson")


def _stream(history: list[dict], page: dict, attachment: dict | None = None):
    """Runs after the request's own session is gone, so it opens its own."""
    db = SessionLocal()
    counted = {"n": 0}
    try:
        yield from _run(db, history, page, counted, attachment)
    except Exception:  # noqa: BLE001 - the panel must always get a final event
        log.exception("ask failed")
        db.rollback()
        if counted["n"]:
            _bump(db, -1)
        yield _line({"type": "error", "message": "Something went wrong answering this question.", "meter": meter(db)})
    finally:
        db.close()


def _run(db: Session, history: list[dict], page: dict, counted: dict, attachment: dict | None = None):
    problem = card_ai.key_problem(db)
    if problem:      # not counted: nothing is sent
        yield _line({"type": "error", "paused": True, "meter": meter(db),
                     "message": problem["message"] + " Update ANTHROPIC_API_KEY in .env and redeploy; "
                                "Re-run AI under Cards → Settings (or Try again on the Cards page) tries the key once."})
        return
    _bump(db, 1)
    counted["n"] = 1
    meta = {"question_id": uuid.uuid4().hex[:12], "question": history[-1]["text"]}
    ai_log = make_logger(db)
    client = card_ai.client_factory()
    areas = card_ai.areas_enabled()          # what the AI may see: an area that is off is never read
    data = ask_data.load(db, areas)
    previous = ask_data.realias(history[-2]["text"], data) if len(history) >= 2 else ""
    categories = sorted({r.cat_l1 for r in data.rows})
    question = ask_data.realias(history[-1]["text"], data)
    meta["question"] = question
    meta["attachment"] = bool(attachment)
    meta["areas"] = areas
    # A question with a statement file attached is about setting up its import format: no topic check.
    if not attachment and ask.topic_gate(client, question, previous, categories, ai_log, meta) is False:
        # Declined questions still count: the check cost a call.
        yield _line({"type": "declined", "message": ask.declined_message(areas), "meter": meter(db)})
        return
    for ev in ask.answer(client, data, history, page, ai_log, meta, attachment, areas):
        if ev["type"] == "error":
            _bump(db, -1)       # no answer: refund the question
            counted["n"] = 0
        if ev["type"] in ("final", "declined", "error"):
            ev = {**ev, "meter": meter(db)}
        yield _line(ev)
