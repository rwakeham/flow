"""
/api/import/detect and /api/import/batch/plan: which saved import formats read an
uploaded file, and (for several files) which account each one belongs to.

Read-only. A file is "detected" when a saved import format reads it exactly
(import_profile_resolve.choose); the formats' kinds say whether it can go to a
cash account (bank statement or ledger) or a card. A file no saved format reads
gets a drafted format (import_infer) for the user to check and save: Flow never
guesses a layout at import time.
"""

import json
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, File
from sqlalchemy import text
from sqlalchemy.orm import Session

from auth import require_auth
from database import get_db
from models import CardAccount, RegisterAccount
import card_import
import import_batch
import import_infer
import import_profile as ip
import import_profile_resolve as resolve

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_BATCH_FILES = 30
_KEY = {"cash": "r", "card": "c"}


@dataclass
class _Detected:
    result: dict                                            # what /import/detect returns
    matches: list = field(default_factory=list)             # [(Candidate, ParsedFile)] formats that read it exactly

    def first(self, kind: str, row_source: str | None = None):
        for c, parsed in self.matches:
            if c.kind == kind and (row_source is None or c.spec.row_source == row_source):
                return c, parsed
        return None


def _detect(db: Session, content: bytes, filename: str) -> _Detected:
    """Every saved format that reads the file; else a drafted format. 422 when it isn't a statement at all."""
    ev = resolve.evaluate(content, filename, [c for c in resolve.load(db) if c.kind != "balance"])
    matches = [(c, parsed) for _score, c, parsed in ev.matches()]
    if not matches and ev.drifted:
        _ov, c, _parsed, header, diff = sorted(ev.drifted, key=lambda t: t[0], reverse=True)[0]
        ev.header()
        return _Detected({"type": "changed", "kind": c.kind, "profile": resolve.summary(c), "diff": diff,
                          "header_cells": header, "draft": resolve.repoint(c.spec, header, ev.inferred),
                          "accounts": [f"{_KEY[k]}:{a}" for k, a in c.accounts if k in _KEY]})
    if not matches:
        try:
            draft = import_infer.infer(content, filename)
        except (import_infer.InferError, ip.FormatMismatch, ip.SpecError) as exc:
            raise HTTPException(status_code=422, detail=f"Could not read this file as a statement: {exc}")
        if draft.kind == "balance":
            raise HTTPException(status_code=422, detail="This looks like a balance file (accounts and values), "
                                "not transactions. Read it under Dashboard → Update balances → File.")
        try:
            rows = len(ip.parse(content, filename, draft.spec).rows)
        except ip.FormatMismatch:
            rows = 0
        return _Detected({"type": "new", "kind": draft.kind, "rows": rows, "draft": draft.to_json()})
    det = _Detected({}, matches)
    kinds = {(c.kind, c.spec.row_source if c.kind == "cash" else None) for c, _ in matches}
    profiles = [{**resolve.summary(c), "accounts": [f"{_KEY[k]}:{a}" for k, a in c.accounts if k in _KEY],
                 "rows": len(p.rows)} for c, p in matches]
    if ("cash", "bank") in kinds:
        c, parsed = det.first("cash", "bank")
        det.result = {"type": "register", "format": "bank", "rows": len(parsed.rows)}
    elif ("cash", "manual") in kinds and ("card", None) not in kinds:
        c, parsed = det.first("cash", "manual")
        det.result = {"type": "register", "format": "ledger", "rows": len(parsed.rows)}
    else:
        c, parsed = det.first("card")
        det.result = {"type": "card", "rows": len(parsed.rows)}
    card = det.first("card")
    det.result.update({"issuer_format": card[0].name if card else c.name,
                       "issuer_label": card[0].name if card else c.name,
                       "profiles": profiles,
                       "accounts": list(dict.fromkeys(k for p in profiles for k in p["accounts"]))})
    return det


@router.post("/import/detect")
async def detect_import(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """
    Which saved formats read this file (type card / register) and the accounts they
    are linked to; type "changed" when it is a saved format's export with a changed
    header (with the format re-pointed at the new header); or type "new" with a
    drafted format. Writes nothing.
    """
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
    return _detect(db, content, file.filename or "upload").result


@router.post("/import/batch/plan")
async def plan_batch(files: list[UploadFile] = File(...), pinned: str = Form(""), db: Session = Depends(get_db)):
    """
    Bulk import, step one (read-only): detect each file and suggest its account
    (import_batch.plan). ``pinned`` is a JSON object of file index to account key
    ("r:<id>" / "c:<id>") for files the user has already picked. The browser then
    sends each file to the normal import endpoint, which checks it again.
    """
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(status_code=422, detail=f"At most {MAX_BATCH_FILES} files at a time")
    try:
        pins = {int(k): str(v) for k, v in (json.loads(pinned) if pinned else {}).items()}
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="pinned must be a JSON object of file index to account")
    if len(pins) > len(files) or any(len(v) > 40 for v in pins.values()):
        raise HTTPException(status_code=422, detail="pinned must name at most one account per file")

    entries, batch_files = [], []
    for i, f in enumerate(files):
        entry = {"index": i, "filename": f.filename or f"file {i + 1}", "type": None, "error": None}
        entries.append(entry)
        content = await f.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            entry["error"] = "File too large (max 10 MB)"
            continue
        try:
            det = _detect(db, content, entry["filename"])
        except HTTPException as exc:
            entry["error"] = exc.detail
            continue
        except ip.TooManyRows as exc:
            entry["error"] = str(exc)
            continue
        if det.result["type"] == "new":
            entry["error"] = "No saved import format reads this file yet."
            entry["setup"] = {"mode": "new", "kind": det.result["kind"], "draft": det.result["draft"]}
            continue
        if det.result["type"] == "changed":
            entry["error"] = (f"Its columns have changed since the format \"{det.result['profile']['name']}\" "
                              "was saved.")
            entry["setup"] = {"mode": "changed", "kind": det.result["kind"], "profile": det.result["profile"],
                              "diff": det.result["diff"], "draft": det.result["draft"]}
            continue
        entry.update({k: v for k, v in det.result.items() if k != "draft"})
        bank, ledger, card = det.first("cash", "bank"), det.first("cash", "manual"), det.first("card")
        bf = import_batch.BatchFile(index=i, kind="card")
        if bank:
            bf.kind = "bank"
            bf.cash_rows = [(r.date, r.amount, r.bank_description) for r in ip.to_bank_rows(bank[1])]
        elif ledger and not card:
            bf.kind = "ledger"
        if card:
            c, parsed = card
            result = ip.to_card_result(parsed, c.spec, c.name)
            bf.card_rows = [(r.date, r.amount, r.raw_description) for r in result.rows]
            bf.numbers = result.card_numbers
            bf.sign_guessed = result.sign_needs_review
        elif bank:
            bf.card_rows = list(bf.cash_rows)      # a bank-layout file can go to a card too: same sign
        bf.format_accounts = entry["accounts"]
        dated = bf.cash_rows or bf.card_rows or ([(r.date,) for r in ledger[1].rows] if ledger else [])
        if dated:
            entry["date_min"] = min(r[0] for r in dated).isoformat()
            entry["date_max"] = max(r[0] for r in dated).isoformat()
        entry["sign_guessed"] = bf.sign_guessed
        batch_files.append(bf)

    reg_accts = db.query(RegisterAccount).order_by(RegisterAccount.name).all()
    card_accts = db.query(CardAccount).order_by(CardAccount.name).all()
    accounts = ([import_batch.BatchAccount(f"r:{a.id}", a.name) for a in reg_accts]
                + [import_batch.BatchAccount(f"c:{a.id}", a.name, card_import.card_numbers(a.last4), bool(a.archived)) for a in card_accts])
    existing: dict[str, list[tuple]] = {}
    coverage: dict[str, tuple] = {}
    all_dates = [r[0] for bf in batch_files for r in bf.cash_rows + bf.card_rows]
    if all_dates:
        window = {"lo": min(all_dates), "hi": max(all_dates)}
        for aid, d, amt, desc in db.execute(text("""
            SELECT register_account_id, date, amount, bank_description FROM transactions
             WHERE source = 'bank' AND date BETWEEN :lo AND :hi
        """), window).fetchall():
            existing.setdefault(f"r:{aid}", []).append((d, amt, desc))
        for aid, d, amt, desc in db.execute(text("""
            SELECT card_account_id, date, amount, raw_description FROM card_transactions
             WHERE date BETWEEN :lo AND :hi
        """), window).fetchall():
            existing.setdefault(f"c:{aid}", []).append((d, amt, desc))
    for aid, lo, hi in db.execute(text("""
        SELECT register_account_id, MIN(date), MAX(date) FROM transactions
         WHERE source = 'bank' GROUP BY register_account_id
    """)).fetchall():
        coverage[f"r:{aid}"] = (lo, hi)
    for aid, span in card_import.coverage_by_card(db, [a.id for a in card_accts]).items():
        coverage[f"c:{aid}"] = span

    suggestions = import_batch.plan(batch_files, accounts, existing, coverage, pins)
    for entry in entries:
        s = suggestions.get(entry["index"])
        if s is not None:
            entry.update({"account": s.account, "sure": s.sure, "hint": s.hint,
                          "reasons": s.reasons, "warnings": s.warnings})
    return {
        "files": entries,
        "accounts": [{"key": a.key, "name": a.name} for a in accounts if not a.archived],
    }
