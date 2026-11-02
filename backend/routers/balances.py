"""
/api/balances: typing and pasting balance-sheet values.

The paste path reads a bank's account-summary page locally (balance_sheet.extract).
Only when that reading looks incomplete, and only when the user asks, is a scrubbed
outline sent to the AI (amounts and account numbers as tokens, other words outside
a finance vocabulary as "~"); the model pairs tokens and Flow maps them back.
Nothing is written until the user applies the rows they ticked in the preview,
and /apply checks them again.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

import balance_sheet
import change_log
import card_ai
import import_profile as ip
import import_profile_resolve as resolve
from auth import require_auth
from backup_service import ensure_daily_backup
from database import get_db
from models import Account, AppSetting, Balance, BalanceMarker

router = APIRouter(prefix="/api/balances", dependencies=[Depends(require_auth)])

PROMPT_VERSION = "bal-v1"


# ── Settings ─────────────────────────────────────────────────────────────────

def thresholds(db: Session) -> balance_sheet.Thresholds:
    th = balance_sheet.Thresholds()
    for key, attr in (("balance_alert_pct", "pct"), ("balance_alert_abs", "abs")):
        row = db.get(AppSetting, key)
        if row and row.value not in (None, ""):
            try:
                setattr(th, attr, float(row.value))
            except ValueError:
                pass
    return th


class SettingsBody(BaseModel):
    alert_pct: float
    alert_abs: float


def user_today(x_flow_today: str | None = Header(None)) -> date:
    """Today in the user's time zone, from the browser (balance_sheet.client_today)."""
    return balance_sheet.client_today(x_flow_today, datetime.now(timezone.utc).date())


@router.get("/settings")
def get_settings(db: Session = Depends(get_db), today: date = Depends(user_today)):
    th = thresholds(db)
    return {"alert_pct": th.pct, "alert_abs": th.abs, "suggested_period": balance_sheet.current_period(today).isoformat(),
            "ai": {"enabled": card_ai.area_enabled("balances"), "reason": card_ai.area_off_reason("balances"),
                   "paused": (card_ai.key_problem(db) or {}).get("message")}}


@router.put("/settings")
def put_settings(body: SettingsBody, db: Session = Depends(get_db), today: date = Depends(user_today)):
    for key, v in (("balance_alert_pct", body.alert_pct), ("balance_alert_abs", body.alert_abs)):
        if not math.isfinite(v) or v < 0 or v > 1e12:
            raise HTTPException(status_code=422, detail="Thresholds must be zero or more")
        row = db.get(AppSetting, key) or AppSetting(key=key)
        row.value = str(v)
        db.add(row)
    change_log.record(db, "balance", "settings", f"Balance change alerts set to {body.alert_pct:g}% and {change_log.money(body.alert_abs)}",
                      {"alert_pct": body.alert_pct, "alert_abs": body.alert_abs})
    db.commit()
    return get_settings(db, today)


# ── Reading a paste ──────────────────────────────────────────────────────────

class PasteBody(BaseModel):
    text: str


def _item_json(it: balance_sheet.Item) -> dict:
    return {"last4": it.last4, "label": it.label, "amount": it.amount, "kind": it.kind,
            "detail": it.detail, "source": it.source, "account_id": it.account_id}


def _markers(db: Session) -> list[balance_sheet.Marker]:
    archived = {a.id for a in db.query(Account.id).filter(Account.archived.is_(True)).all()}
    return [balance_sheet.Marker(account_id=m.account_id, label_text=m.label_text, last4=m.last4,
                                 amount_rule=m.amount_rule, amount_label=m.amount_label,
                                 archived=m.account_id in archived)
            for m in db.query(BalanceMarker).order_by(BalanceMarker.id).all()]


def _read(db: Session, t: str) -> tuple[list[balance_sheet.Item], bool]:
    """Markers first, then the anchored reader for everything they didn't cover."""
    ex = balance_sheet.extract(t)
    items = balance_sheet.combine(balance_sheet.read_with_markers(t, _markers(db)), ex.items)
    still_unread = any(i.kind == "none" for i in items)
    return items, ex.needs_ai and (still_unread or not items)


def _check_text(body: PasteBody) -> str:
    t = body.text or ""
    if not t.strip():
        raise HTTPException(status_code=422, detail="Paste the account summary first")
    if len(t) > balance_sheet.MAX_PASTE_CHARS:
        raise HTTPException(status_code=413, detail=f"That's too long ({balance_sheet.MAX_PASTE_CHARS:,} characters at most)")
    return t


@router.post("/read")
def read_paste(body: PasteBody, db: Session = Depends(get_db)):
    """
    Read a paste locally (paste markers first, then the anchored reader). Nothing is sent anywhere;
    ``outline`` is what the AI would see if asked, ``lines`` what the teach view shows.
    """
    t = _check_text(body)
    items, needs_ai = _read(db, t)
    ol = balance_sheet.outline(t)
    return {"items": [_item_json(i) for i in items], "unread": [i.last4 for i in items if i.kind == "none"],
            "needs_ai": needs_ai, "outline": ol.text, "lines": balance_sheet.paste_lines(t)}


class Pair(BaseModel):
    account: str                                    # "<N3>"
    amount: str | None                              # "<A7>" or null
    kind: Literal["balance", "owed", "rewards", "closed", "none"]


class Pairs(BaseModel):
    pairs: list[Pair]


SYSTEM = """You read the outline of a bank's account-summary web page that a user copied. \
To protect their privacy the outline has been scrubbed: every money amount is a token like <A3>, \
every account number is a token like <N2>, and any word that is not common banking vocabulary is "~". \
Small numbers are "#".

For each account token <N…>, say which amount token is that account's current balance, and what kind \
of value it is:
- "balance": money in the account or its value (available balance, account value, current balance)
- "owed": an amount owed on a loan, mortgage, line of credit or card (outstanding balance, principal)
- "rewards": rewards, points or miles (not money)
- "closed": the account is shown as closed or paid in full
- "none": no balance is shown for the account

Rules: use each amount token at most once; never invent tokens; totals for groups of accounts \
(e.g. "total assets", "cash accounts", "total liabilities") belong to no account; when unsure, use \
"none" with a null amount. Answer for every account token in the outline."""


@router.post("/read-ai")
def read_paste_ai(body: PasteBody, db: Session = Depends(get_db)):
    """
    Ask the AI to pair amounts with accounts on the scrubbed outline (the user asks
    for this). Local readings stand; the AI only fills accounts left unread.
    """
    t = _check_text(body)
    reason = card_ai.area_off_reason("balances")
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    problem = card_ai.key_problem(db)
    if problem:
        raise HTTPException(status_code=409, detail=problem["message"] + " Update ANTHROPIC_API_KEY in .env and redeploy.")
    local, _needs = _read(db, t)
    ol = balance_sheet.outline(t)
    if not ol.numbers:
        raise HTTPException(status_code=422, detail="No account numbers found (Flow matches accounts by their last 4 digits).")
    payload = {"outline": ol.text}
    log_row = card_ai._log(db, "balances", PROMPT_VERSION, payload, len(ol.numbers))
    db.commit()
    try:
        parsed, usage, _model = card_ai._call(card_ai.client_factory(), SYSTEM, payload, Pairs,
                                              effort="low", max_tokens=4000, log_row=log_row)
    except Exception as exc:  # noqa: BLE001
        message, _fatal, kind = card_ai._describe_error(exc)
        card_ai._finish_log(log_row, status="refusal" if "declined" in message else "error", error=message, kind=kind)
        db.commit()
        raise HTTPException(status_code=502, detail=message) from exc
    card_ai._finish_log(log_row, usage=usage)
    db.commit()
    ai_items = balance_sheet.items_from_ai([p.model_dump() for p in parsed.pairs], ol)
    merged = balance_sheet.merge_items(local, ai_items)
    return {"items": [_item_json(i) for i in merged],
            "unread": [i.last4 for i in merged if i.kind == "none"], "needs_ai": False,
            "outline": ol.text, "lines": balance_sheet.paste_lines(t), "log_id": log_row.id}


# ── Balance files ────────────────────────────────────────────────────────────

MAX_FILE_BYTES = 10 * 1024 * 1024


@router.post("/import/read")
async def read_file(file: UploadFile = File(...), profile_id: int | None = Form(None),
                    allow_errors: bool = Form(False), db: Session = Depends(get_db),
                    today: date = Depends(user_today)):
    """
    Read a balance file (a holdings or accounts export) with a saved balance format and return
    rows shaped like a paste reading, for the same preview and apply. A file holds several
    accounts, so every balance format is a candidate, not only one account's. No format reads
    it: 422 no_profile with a draft; a changed header: 409 profile_drift; unreadable rows: 422
    row_errors unless allow_errors. Writes nothing but the format's last use.
    """
    content = await file.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
    filename = file.filename or "upload"
    candidates = resolve.load(db, kind="balance")
    if profile_id is not None:
        candidates = [c for c in candidates if c.id == profile_id]
    res = resolve.choose(content, filename, candidates, "balance")
    if res.status == "drift":
        c = res.candidate
        raise HTTPException(status_code=409, detail={
            "code": "profile_drift", "profile": resolve.summary(c), "diff": res.diff,
            "header_cells": res.header_cells, "draft": res.draft,
            "message": f"This file's columns have changed since the format \"{c.name}\" was saved. "
                       "Check the updated format before reading it."})
    if res.status == "none":
        raise HTTPException(status_code=422, detail={
            "code": "no_profile", "matches": [], "draft": res.draft,
            "message": "Flow hasn't seen this balance file's layout yet. Set up its format first."})
    c, parsed = res.candidate, res.parsed
    if parsed.errors and not allow_errors:
        raise HTTPException(status_code=422, detail={
            "code": "row_errors", "errors": parsed.errors[:20], "error_count": len(parsed.errors),
            "message": f"{len(parsed.errors)} row(s) couldn't be read with the format \"{c.name}\". "
                       "Fix the format, or read the rest."})
    infos, _latest = _account_infos(db, today)
    items = balance_sheet.assign_accounts(ip.to_balance_items(parsed, c.spec), infos, _markers(db))
    for it in items:
        it.source = "file"
    as_of = max((r.as_of for r in parsed.rows if r.as_of), default=None)
    resolve.mark_previewed(db, c, parsed)
    resolve.touch(db, c)
    db.commit()
    return {"items": [_item_json(i) for i in items], "profile": resolve.summary(c),
            "errors": parsed.errors[:20], "error_count": len(parsed.errors),
            "as_of": as_of.isoformat() if as_of else None,
            "suggested_period": balance_sheet.current_period(as_of).isoformat() if as_of else None}


# ── Paste markers ────────────────────────────────────────────────────────────

def _marker_json(m: BalanceMarker) -> dict:
    return {"id": m.id, "account_id": m.account_id, "label_text": m.label_text, "last4": m.last4,
            "amount_rule": m.amount_rule, "amount_label": m.amount_label, "source": m.source,
            "created_at": m.created_at.isoformat() if m.created_at else None}


def _save_marker(db: Session, m: balance_sheet.Marker, source: str) -> tuple[BalanceMarker, bool]:
    """Store a marker unless the account already has the same one. (row, created)."""
    same = db.query(BalanceMarker).filter_by(account_id=m.account_id, label_text=m.label_text,
                                             amount_rule=m.amount_rule, amount_label=m.amount_label).first()
    if same is not None:
        return same, False
    row = BalanceMarker(account_id=m.account_id, label_text=m.label_text[:200], last4=m.last4,
                        amount_rule=m.amount_rule, amount_label=(m.amount_label or None) and m.amount_label[:200],
                        source=source)
    db.add(row)
    db.flush()
    return row, True


@router.get("/markers")
def list_markers(account_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(BalanceMarker)
    if account_id is not None:
        q = q.filter(BalanceMarker.account_id == account_id)
    return [_marker_json(m) for m in q.order_by(BalanceMarker.id).all()]


class ExampleBody(BaseModel):
    text: str
    account_id: int
    label_line: int
    amount_line: int
    amount_start: int


@router.post("/markers/from-example", status_code=201)
def marker_from_example(body: ExampleBody, db: Session = Depends(get_db)):
    """"This line names the account, that is its balance": saved only if it finds that amount again."""
    t = _check_text(PasteBody(text=body.text))
    if db.get(Account, body.account_id) is None:
        raise HTTPException(status_code=404, detail="Account not found")
    try:
        m = balance_sheet.marker_from_example(t, body.account_id, body.label_line, body.amount_line, body.amount_start)
    except balance_sheet.MarkerError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    # A spot on the page belongs to one account: teaching it again moves it.
    for other in db.query(BalanceMarker).filter(BalanceMarker.account_id != body.account_id).all():
        om = balance_sheet.Marker(account_id=other.account_id, label_text=other.label_text, last4=other.last4,
                                  amount_rule=other.amount_rule, amount_label=other.amount_label)
        if balance_sheet.same_spot(t, m, om):
            db.delete(other)
    row, _created = _save_marker(db, m, "user")
    acct = db.get(Account, body.account_id)
    change_log.record(db, "balance", "marker", f"Taught where {change_log.clip(acct.name if acct else '')} sits on a pasted summary", {"account_id": body.account_id, "label": m.label_text})
    db.commit()
    return _marker_json(row)


@router.delete("/markers/{marker_id}", status_code=204)
def delete_marker(marker_id: int, db: Session = Depends(get_db)):
    row = db.get(BalanceMarker, marker_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Marker not found")
    acct = db.get(Account, row.account_id)
    change_log.record(db, "balance", "marker_delete", f"Removed a paste marker of {change_log.clip(acct.name if acct else '')}", {"account_id": row.account_id, "label": row.label_text})
    db.delete(row)
    db.commit()


# ── Preview and apply ────────────────────────────────────────────────────────

class EntryBody(BaseModel):
    amount: float | None = None
    last4: str = ""
    label: str = ""
    kind: str = "balance"
    detail: str = ""
    account_id: int | None = None
    source: str = "local"


class PreviewBody(BaseModel):
    period: date
    entries: list[EntryBody] = Field(max_length=1000)


def _account_infos(db: Session, period: date) -> tuple[list[balance_sheet.AccountInfo], date | None]:
    accounts = db.query(Account).order_by(Account.name).all()
    prev = {aid: (p, float(a)) for aid, p, a in db.execute(text("""
        SELECT DISTINCT ON (account_id) account_id, period, amount FROM balances
         WHERE period < :p ORDER BY account_id, period DESC
    """), {"p": period}).fetchall()}
    existing = {aid: float(a) for aid, a in db.execute(text(
        "SELECT account_id, amount FROM balances WHERE period = :p"), {"p": period}).fetchall()}
    latest = db.execute(text("SELECT MAX(period) FROM balances")).scalar()
    infos = [balance_sheet.AccountInfo(
        id=a.id, name=a.name, account_type=a.account_type, last4=balance_sheet.numbers(a.last4),
        archived=bool(a.archived), ignored=bool(a.ignored), alert_pct=a.alert_pct, alert_abs=a.alert_abs,
        previous=prev.get(a.id), existing=existing.get(a.id)) for a in accounts]
    return infos, latest


@router.post("/preview")
def preview(body: PreviewBody, db: Session = Depends(get_db), today: date = Depends(user_today)):
    """The checks shown before anything is written (balance_sheet.plan). Read-only."""
    if len(body.entries) > 200:
        raise HTTPException(status_code=422, detail="At most 200 entries at a time")
    infos, latest = _account_infos(db, body.period)
    entries = [balance_sheet.Entry(**e.model_dump()) for e in body.entries]
    out = balance_sheet.plan(entries, infos, body.period, today, latest, thresholds(db))
    out["accounts"] = [{"id": a.id, "name": a.name, "account_type": a.account_type, "archived": a.archived}
                       for a in infos]
    return out


class ApplyRow(BaseModel):
    account_id: int
    value: float                    # signed, as stored (liabilities negative)
    last4: str = ""                 # saved on the account when it isn't yet (a first-time link)


class RememberRow(BaseModel):
    account_id: int
    last4: str
    amount: float                   # as shown on the page


class ApplyBody(BaseModel):
    period: date
    rows: list[ApplyRow] = Field(max_length=1000)
    source: Literal["manual", "paste", "file"] = "paste"
    text: str | None = None                       # the paste, when rows read by the AI are to be remembered
    remember: list[RememberRow] = Field([], max_length=1000)


@router.post("/apply")
def apply(body: ApplyBody, db: Session = Depends(get_db), today: date = Depends(user_today)):
    """Write the rows the user ticked. Checked again here; all or nothing."""
    problem = balance_sheet.period_problem(body.period, today)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    if not body.rows:
        raise HTTPException(status_code=422, detail="Nothing to apply")
    seen: set[int] = set()
    accounts = {a.id: a for a in db.query(Account).all()}
    owner: dict[str, int] = {}
    for a in accounts.values():
        for n in balance_sheet.numbers(a.last4):
            owner.setdefault(n, a.id)
    for r in body.rows:
        if r.account_id not in accounts:
            raise HTTPException(status_code=422, detail=f"Account {r.account_id} doesn't exist")
        if r.account_id in seen:
            raise HTTPException(status_code=422, detail=f"{accounts[r.account_id].name} appears twice")
        seen.add(r.account_id)
        if not math.isfinite(r.value) or abs(r.value) > balance_sheet.MAX_AMOUNT:
            raise HTTPException(status_code=422, detail=f"{accounts[r.account_id].name}: that amount isn't usable")
        n = balance_sheet.numbers(r.last4)
        if r.last4 and not n:
            raise HTTPException(status_code=422, detail="Account numbers are the last 4 digits")
        if n and owner.get(n[0], r.account_id) != r.account_id:
            raise HTTPException(status_code=409, detail=f"…{n[0]} is saved on {accounts[owner[n[0]]].name}")
    try:
        ensure_daily_backup(db)
    except Exception:  # noqa: BLE001 - a backup failure must never block an update
        pass
    linked = 0
    for r in body.rows:
        acct = accounts[r.account_id]
        n = balance_sheet.numbers(r.last4)
        if n and n[0] not in balance_sheet.numbers(acct.last4):
            acct.last4 = balance_sheet.join_numbers(balance_sheet.numbers(acct.last4) + n)
            linked += 1
        value = round(r.value, 2)
        db.execute(pg_insert(Balance).values(account_id=acct.id, period=body.period, amount=value, source=body.source)
                   .on_conflict_do_update(index_elements=["account_id", "period"],
                                          set_={"amount": value, "source": body.source, "updated_at": text("now()")}))
    remembered = 0
    if body.remember and body.text and len(body.text) <= balance_sheet.MAX_PASTE_CHARS:
        # Rows the AI read: a marker that finds the same amount next time, so that paste needs no AI.
        applied = {r.account_id for r in body.rows}
        for r in body.remember[:50]:
            if r.account_id not in applied:
                continue
            m = balance_sheet.marker_for_item(body.text, r.account_id, r.last4, r.amount)
            if m is not None and _save_marker(db, m, "ai")[1]:
                remembered += 1
    change_log.record(db, "balance", "apply", f"Applied {len(body.rows)} balance{'s' if len(body.rows) != 1 else ''} for {body.period:%B %Y} ({body.source})",
                      {"period": body.period.isoformat(), "source": body.source, "accounts": [accounts[r.account_id].name for r in body.rows][:50],
                       "linked": linked, "remembered": remembered})
    db.commit()
    return {"period": body.period.isoformat(), "written": len(body.rows), "linked": linked, "remembered": remembered}


# ── Single balance edits (account history) ───────────────────────────────────

class OneBalance(BaseModel):
    account_id: int
    period: date
    amount: float                   # signed, as stored


@router.put("")
def put_balance(body: OneBalance, db: Session = Depends(get_db), today: date = Depends(user_today)):
    acct = db.get(Account, body.account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")
    problem = balance_sheet.period_problem(body.period, today)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    if not math.isfinite(body.amount) or abs(body.amount) > balance_sheet.MAX_AMOUNT:
        raise HTTPException(status_code=422, detail="That amount isn't usable")
    value = round(body.amount, 2)
    db.execute(pg_insert(Balance).values(account_id=body.account_id, period=body.period, amount=value, source="manual")
               .on_conflict_do_update(index_elements=["account_id", "period"],
                                      set_={"amount": value, "source": "manual", "updated_at": text("now()")}))
    change_log.record(db, "balance", "set", f"{change_log.clip(acct.name)} {body.period:%B %Y}: {change_log.money(value)}",
                      {"account_id": acct.id, "period": body.period.isoformat(), "amount": value})
    db.commit()
    return {"account_id": body.account_id, "period": body.period.isoformat(), "amount": value}


@router.delete("")
def delete_balance(account_id: int, period: date, db: Session = Depends(get_db)):
    n = db.query(Balance).filter(Balance.account_id == account_id, Balance.period == period).delete()
    if n:
        acct = db.get(Account, account_id)
        change_log.record(db, "balance", "delete", f"Deleted the {period:%B %Y} balance of {change_log.clip(acct.name if acct else '')}",
                          {"account_id": account_id, "period": period.isoformat()})
    db.commit()
    if not n:
        raise HTTPException(status_code=404, detail="No balance for that account and period")
    return {"deleted": n}
