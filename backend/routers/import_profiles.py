"""
/api/import-profiles: the saved import formats, their account links, drafting a
format from a sample file and previewing a file with a format.

A format is an ``import_profile.Spec`` stored as JSON (``import_profiles``) and
linked to the accounts that use it (``import_profile_links``, by account kind:
cash register, card or balance-sheet account). Formats hold no transaction data
(only header text and settings), so exporting one shares nothing personal; an
imported one is marked ``needs_preview`` until it has read a real file.
"""

from __future__ import annotations


import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

import exports
import change_log
from auth import require_auth
from database import get_db
import import_infer
import import_profile as ip
import import_profile_resolve as resolve
from models import Account, CardAccount, ImportProfile, ImportProfileLink, RegisterAccount

router = APIRouter(prefix="/api/import-profiles", dependencies=[Depends(require_auth)])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_FORMAT_JSON = 64 * 1024
PREVIEW_ROWS = 200
ACCOUNT_KINDS = {"cash": RegisterAccount, "card": CardAccount, "balance": Account}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _spec(data: dict) -> ip.Spec:
    try:
        return ip.Spec.from_json(data)
    except ip.SpecError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except TypeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid format: {exc}")


def _account(db: Session, kind: str, account_id: int):
    model = ACCOUNT_KINDS.get(kind)
    if model is None:
        raise HTTPException(status_code=422, detail="Account kind must be cash, card or balance.")
    acct = db.get(model, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return acct


def _profile(db: Session, profile_id: int) -> ImportProfile:
    row = db.get(ImportProfile, profile_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Import format not found")
    return row


def _json(db: Session, row: ImportProfile) -> dict:
    names = {}
    for kind, model in ACCOUNT_KINDS.items():
        names[kind] = {a.id: a.name for a in db.query(model.id, model.name).all()}
    links = db.query(ImportProfileLink).filter_by(profile_id=row.id).all()
    return {
        "id": row.id, "name": row.name, "kind": row.kind, "institution": row.institution or "",
        "notes": row.notes or "", "spec": json.loads(row.spec_json), "fingerprint": row.fingerprint,
        "version": row.version, "needs_preview": row.needs_preview,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "links": [{"kind": l.account_kind, "account_id": l.account_id,
                   "account": names.get(l.account_kind, {}).get(l.account_id, "(deleted)")} for l in links],
    }


async def _read(file: UploadFile) -> tuple[bytes, str]:
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
    return content, file.filename or "upload"


class LinkBody(BaseModel):
    kind: str
    account_id: int


class ProfileBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    institution: str = Field("", max_length=120)
    notes: str = Field("", max_length=2000)
    spec: dict
    link: list[LinkBody] = Field([], max_length=500)


# ── Formats ──────────────────────────────────────────────────────────────────

@router.get("")
def list_profiles(kind: str | None = None, db: Session = Depends(get_db)):
    q = db.query(ImportProfile)
    if kind:
        q = q.filter(ImportProfile.kind == kind)
    return [_json(db, r) for r in q.order_by(ImportProfile.kind, ImportProfile.name).all()]


@router.get("/{profile_id}")
def get_profile(profile_id: int, db: Session = Depends(get_db)):
    return _json(db, _profile(db, profile_id))


@router.post("", status_code=201)
def create_profile(body: ProfileBody, db: Session = Depends(get_db)):
    spec = _spec(body.spec)
    row = ImportProfile(name=body.name.strip(), kind=spec.kind, institution=body.institution.strip() or None,
                        notes=body.notes.strip() or None, spec_json=json.dumps(spec.to_json(), sort_keys=True),
                        fingerprint=spec.fingerprint, version=1)
    db.add(row)
    db.flush()
    for l in body.link:
        if l.kind != spec.kind:
            raise HTTPException(status_code=422, detail=f"A {spec.kind} format can't be used by a {l.kind} account.")
        _account(db, l.kind, l.account_id)
        resolve.link(db, row.id, l.kind, l.account_id)
    change_log.record(db, "format", "add", f"Added {spec.kind} import format {change_log.clip(row.name)}", {"id": row.id, "kind": spec.kind, "links": len(body.link)})
    db.commit()
    return _json(db, row)


@router.put("/{profile_id}")
def update_profile(profile_id: int, body: ProfileBody, db: Session = Depends(get_db)):
    row = _profile(db, profile_id)
    spec = _spec(body.spec)
    if spec.kind != row.kind:
        raise HTTPException(status_code=422, detail="A format's kind can't change; make a new format instead.")
    row.name, row.institution, row.notes = body.name.strip(), body.institution.strip() or None, body.notes.strip() or None
    new_json = json.dumps(spec.to_json(), sort_keys=True)
    if new_json != row.spec_json:
        row.spec_json, row.fingerprint, row.version = new_json, spec.fingerprint, row.version + 1
    for l in body.link:
        if l.kind != row.kind:
            raise HTTPException(status_code=422, detail=f"A {row.kind} format can't be used by a {l.kind} account.")
        _account(db, l.kind, l.account_id)
        resolve.link(db, row.id, l.kind, l.account_id)
    change_log.record(db, "format", "edit", f"Edited import format {change_log.clip(row.name)}" + (f" (version {row.version})" if new_json != row.spec_json or row.version > 1 else ""),
                      {"id": row.id, "version": row.version, "links": len(body.link)})
    db.commit()
    return _json(db, row)


@router.delete("/{profile_id}", status_code=204)
def delete_profile(profile_id: int, db: Session = Depends(get_db)):
    row = _profile(db, profile_id)
    db.query(ImportProfileLink).filter_by(profile_id=row.id).delete(synchronize_session=False)
    db.execute(text("UPDATE card_imports SET profile_id = NULL WHERE profile_id = :p"), {"p": row.id})
    change_log.record(db, "format", "delete", f"Deleted import format {change_log.clip(row.name)}", {"id": row.id, "kind": row.kind})
    db.delete(row)
    db.commit()


@router.post("/{profile_id}/links")
def add_link(profile_id: int, body: LinkBody, db: Session = Depends(get_db)):
    row = _profile(db, profile_id)
    if body.kind != row.kind:
        raise HTTPException(status_code=422, detail=f"A {row.kind} format can't be used by a {body.kind} account.")
    _account(db, body.kind, body.account_id)
    resolve.link(db, row.id, body.kind, body.account_id)
    change_log.record(db, "format", "link", f"Linked import format {change_log.clip(row.name)} to a {body.kind} account", {"id": row.id, "kind": body.kind, "account_id": body.account_id})
    db.commit()
    return _json(db, row)


@router.delete("/{profile_id}/links")
def remove_link(profile_id: int, kind: str = Query(...), account_id: int = Query(...), db: Session = Depends(get_db)):
    row = _profile(db, profile_id)
    db.query(ImportProfileLink).filter_by(profile_id=row.id, account_kind=kind, account_id=account_id).delete(
        synchronize_session=False)
    change_log.record(db, "format", "unlink", f"Unlinked import format {change_log.clip(row.name)} from a {kind} account", {"id": row.id, "kind": kind, "account_id": account_id})
    db.commit()
    return _json(db, row)


@router.get("/{profile_id}/export")
def export_profile(profile_id: int, db: Session = Depends(get_db)):
    """The format as a JSON file: settings and header text only, no account links or data."""
    row = _profile(db, profile_id)
    body = {"flow_import_format": ip.SCHEMA_VERSION, "name": row.name, "institution": row.institution or "",
            "notes": row.notes or "", "spec": json.loads(row.spec_json)}
    return Response(json.dumps(body, indent=2, sort_keys=True), media_type="application/json",
                    headers={"Content-Disposition": download_disposition(row.name, ".flow-format.json")})


download_disposition = exports.download_disposition      # shared with the CSV exports; kept importable from here


@router.post("/import", status_code=201)
async def import_profile_file(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """A format file exported from Flow. Saved unlinked, marked to be previewed before first use."""
    raw = await file.read(MAX_FORMAT_JSON + 1)
    if len(raw) > MAX_FORMAT_JSON:
        raise HTTPException(status_code=413, detail="A format file is at most 64 KB.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=422, detail="That isn't a Flow import format file.")
    if not isinstance(data, dict) or "spec" not in data or "flow_import_format" not in data:
        raise HTTPException(status_code=422, detail="That isn't a Flow import format file.")
    spec = _spec(data["spec"])
    name = str(data.get("name") or "Imported format").strip()[:120]
    row = ImportProfile(name=name, kind=spec.kind, institution=str(data.get("institution") or "")[:120] or None,
                        notes=str(data.get("notes") or "")[:2000] or None,
                        spec_json=json.dumps(spec.to_json(), sort_keys=True), fingerprint=spec.fingerprint,
                        version=1, needs_preview=True)
    db.add(row)
    change_log.record(db, "format", "import", f"Imported {spec.kind} format {change_log.clip(name)} from a file", {"kind": spec.kind, "name": name})
    db.commit()
    return _json(db, row)


# ── Working with a sample file ───────────────────────────────────────────────

@router.post("/draft")
async def draft_profile(file: UploadFile = File(...), kind: str | None = Form(None)):
    """A drafted format for a file Flow hasn't seen (import_infer), with the raw grid for the editor."""
    content, filename = await _read(file)
    try:
        draft = import_infer.infer(content, filename, kind or None)
    except (import_infer.InferError, ip.FormatMismatch, ip.SpecError) as exc:
        grid = []
        try:
            grid = ip.read_grid(content, filename, limit=50)
        except ip.FormatMismatch:
            pass
        raise HTTPException(status_code=422, detail={"code": "no_draft", "message": str(exc), "grid": grid})
    return draft.to_json()


@router.post("/grid")
async def grid(file: UploadFile = File(...), delimiter: str = Form("auto"), encoding: str = Form("auto"),
               quote_char: str = Form("auto"), sheet: str | None = Form(None)):
    """The file's first rows as text cells (blank rows dropped, as formats count rows), for the editor."""
    content, filename = await _read(file)
    xlsx = ip.is_xlsx(content, filename)
    if (delimiter not in ip.DELIMITERS or encoding not in ip.ENCODINGS or quote_char not in ip.QUOTE_CHARS):
        raise HTTPException(status_code=422, detail="Unknown delimiter, encoding or quote setting.")
    spec = ip.Spec(file_type="xlsx" if xlsx else "delimited", delimiter=delimiter, encoding=encoding,
                   quote_char=quote_char)
    try:
        sheets = ip.read_sheets(content, filename, spec)
    except ip.FormatMismatch as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    names = [n for n, _ in sheets]
    rows = next((r for n, r in sheets if n == sheet), sheets[0][1] if sheets else [])
    return {"file_type": spec.file_type, "sheets": names if xlsx else [],
            "delimiter": ip.detect_delimiter(ip.decode(content, encoding)) if not xlsx and delimiter == "auto" else delimiter,
            "grid": [[ip.cell_text(c).strip() for c in r] for r in rows[:50]], "rows": len(rows)}


def preview_json(parsed: ip.ParsedFile, spec: ip.Spec, check: ip.SignCheck, date_ambiguous: bool = False) -> dict:
    rows = parsed.rows[:PREVIEW_ROWS]
    return {
        "rows": [{"line": r.line, "date": r.date.isoformat() if r.date else None,
                  "description": r.description, "amount": r.amount, "value": r.value, "account_ref": r.account_ref,
                  "status": r.status, "check_number": r.check_number, "reconciled": r.reconciled,
                  "running_balance": r.running_balance} for r in rows],
        "row_count": len(parsed.rows),
        "money_in": round(sum(r.amount for r in parsed.rows if r.amount > 0), 2),
        "money_out": round(sum(r.amount for r in parsed.rows if r.amount < 0), 2),
        "errors": parsed.errors[:50], "error_count": len(parsed.errors), "skipped": parsed.skipped,
        "header_cells": parsed.header_cells, "header_row": parsed.header_row, "sheet": parsed.sheet,
        "fingerprint": parsed.fingerprint, "header_changed": parsed.fingerprint_ok is False, "diff": parsed.diff,
        "sign_check": {"status": check.status, "reason": check.reason},
        "date_order_ambiguous": date_ambiguous,
    }


@router.post("/preview")
async def preview(file: UploadFile = File(...), spec: str = Form(...), profile_id: int | None = Form(None),
                  account_kind: str | None = Form(None), account_id: int | None = Form(None),
                  db: Session = Depends(get_db)):
    """Read a file with a (possibly unsaved) format and show what it would import. Writes nothing
    except clearing ``needs_preview`` on the saved format when it reads the file cleanly."""
    content, filename = await _read(file)
    try:
        data = json.loads(spec)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="spec must be JSON.")
    s = _spec(data)
    try:
        parsed = ip.parse(content, filename, s)
    except ip.FormatMismatch as exc:
        raise HTTPException(status_code=422, detail={"code": "mismatch", "message": str(exc),
                                                     "header_cells": exc.header_cells, "diff": exc.diff})
    history = []
    if account_kind and account_id and parsed.rows:
        lo, hi = min(r.date for r in parsed.rows if r.date), max(r.date for r in parsed.rows if r.date)
        if account_kind == "cash":
            history = [(d, float(a), t or "") for d, a, t in db.execute(text(
                "SELECT date, amount, COALESCE(bank_description, description) FROM transactions "
                "WHERE register_account_id = :a AND source = :s AND date BETWEEN :lo AND :hi"),
                {"a": account_id, "s": "manual" if s.row_source == "manual" else "bank", "lo": lo, "hi": hi})]
        elif account_kind == "card":
            history = [(d, float(a), t) for d, a, t in db.execute(text(
                "SELECT date, amount, raw_description FROM card_transactions "
                "WHERE card_account_id = :a AND date BETWEEN :lo AND :hi"), {"a": account_id, "lo": lo, "hi": hi})]
    check = ip.verify_sign(parsed, s, history) if s.kind != "balance" else ip.SignCheck("unverified", "")
    if profile_id is not None and parsed.rows and not parsed.errors:
        row = db.get(ImportProfile, profile_id)
        if row is not None and row.needs_preview:
            row.needs_preview = False
            db.commit()
    return preview_json(parsed, s, check, ip.date_order_ambiguous(content, filename, s, parsed))
