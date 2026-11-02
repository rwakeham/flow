"""
Cash Flow / Register API endpoints.

All routes are prefixed /api/register and require authentication.
"""

from __future__ import annotations

from calendar import monthrange
from collections import Counter
from datetime import date as DateType

from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile, File, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from auth import require_auth
import balance_sheet
import change_log
from backup_service import ensure_daily_backup
from database import get_db
import exports
import import_profile_resolve
from import_dedup import (
    best_other_account,
    coverage_key,
    date_bounds,
    line_key,
    manual_key,
    other_account_message,
    plan_bank_import,
    plan_ledger_import,
)
from models import RegisterAccount, Transaction
from reconciler import TxnInfo, assign_matches, match_annotations, suggest_matches
import import_profile as ip

router = APIRouter(prefix="/api/register", dependencies=[Depends(require_auth)])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


# ── Helpers ────────────────────────────────────────────────────────────────────

def _reconcile_manual_to_bank(manual_txn: Transaction, bank_txn: Transaction) -> None:
    """
    Update the manual transaction's date and amount to match the bank's actual
    values (source of truth).  When there is a discrepancy, append an audit note
    so the user can see what the original entry was.
    """
    adjustments: list[str] = []

    if float(manual_txn.amount) != float(bank_txn.amount):
        adjustments.append(f"${manual_txn.amount}")
        manual_txn.amount = bank_txn.amount

    if manual_txn.date != bank_txn.date:
        adjustments.append(str(manual_txn.date))
        manual_txn.date = bank_txn.date

    if adjustments:
        note = f"[Adjusted from {', '.join(adjustments)}]"
        if manual_txn.notes:
            manual_txn.notes = f"{manual_txn.notes}\n{note}"
        else:
            manual_txn.notes = note


# ── Wrong-account check ───────────────────────────────────────────────────────

def _other_account_match(db, acct: RegisterAccount, rows) -> dict | None:
    """
    The other cash account a bank file most likely belongs to, or None (rule in
    import_dedup.best_other_account). Compared with bank-imported rows only, each
    account within the dates its bank rows cover; ledger files are not checked.
    """
    if not rows:
        return None
    lo, hi = date_bounds(rows)
    names = {a.id: a.name for a in db.query(RegisterAccount).all()}
    coverage = {aid: (a, b) for aid, a, b in db.execute(text("""
        SELECT register_account_id, MIN(date), MAX(date) FROM transactions
         WHERE source = 'bank' GROUP BY register_account_id
    """)).fetchall()}
    existing: dict[int, list[tuple]] = {}
    for aid, d, amt, desc in db.execute(text("""
        SELECT register_account_id, date, amount, bank_description FROM transactions
         WHERE source = 'bank' AND date BETWEEN :lo AND :hi
    """), {"lo": lo, "hi": hi}).fetchall():
        existing.setdefault(aid, []).append((d, amt, desc))
    m = best_other_account([(r.date, r.amount, r.bank_description) for r in rows], acct.id, names, existing, coverage)
    return m and {**m, "message": other_account_message(m, acct.name)}


# ── Import de-duplication ─────────────────────────────────────────────────────
#
# Imports de-dup by multiplicity so repeated identical statement lines survive —
# see import_dedup.py for the rules. These helpers supply the counts the planners
# consume, scoped to the date range of the file being imported.

def _line_coverage(db, account_id: int, lo, hi) -> Counter:
    """
    Bank rows already on file, per (date, amount, normalized description).

    Statement lines are comparable to each other, so an exact hit here means "this
    posting is already imported" — including when it is half of a confirmed
    reconciliation, where the bank row carries the count for the linked pair.
    """
    existing = db.execute(
        text("""
            SELECT date, amount, bank_description FROM transactions
            WHERE register_account_id = :aid
              AND source = 'bank'
              AND date BETWEEN :lo AND :hi
        """),
        {"aid": account_id, "lo": lo, "hi": hi},
    ).fetchall()
    return Counter(line_key(r.date, r.amount, r.bank_description) for r in existing)


def _generic_coverage(db, account_id: int, lo, hi) -> Counter:
    """
    Reconciled manual entries with no bank partner, per (date, amount).

    Three ways to end up here: the 'X' flag on a ledger-spreadsheet import, the
    "mark reconciled" action, or reconciling against a bank row later deleted. All
    assert a posting happened without an importable line to compare against, so they
    can only offer generic coverage.

    Linked manual entries are excluded — their bank partner is already counted by
    _line_coverage, and counting the pair twice would swallow a real second copy.
    Unreconciled, unmatched entries are excluded too: those are planned entries, and
    a statement row landing on one should match it, not be suppressed by it.
    """
    existing = db.execute(
        text("""
            SELECT date, amount FROM transactions
            WHERE register_account_id = :aid
              AND source = 'manual'
              AND is_reconciled = TRUE
              AND matched_to_id IS NULL
              AND date BETWEEN :lo AND :hi
        """),
        {"aid": account_id, "lo": lo, "hi": hi},
    ).fetchall()
    return Counter(coverage_key(r.date, r.amount) for r in existing)


def _existing_manual_counts(db, account_id: int, lo, hi) -> Counter:
    """How many manual rows the ledger already holds, per (date, amount, description)."""
    existing = db.execute(
        text("""
            SELECT date, amount, description FROM transactions
            WHERE register_account_id = :aid
              AND source = 'manual'
              AND date BETWEEN :lo AND :hi
        """),
        {"aid": account_id, "lo": lo, "hi": hi},
    ).fetchall()
    return Counter(manual_key(r.date, r.amount, r.description) for r in existing)


def _annotate_matches(payload: dict, txns: list[Transaction], today: DateType) -> None:
    """Fold match confidence (see reconciler.match_annotations) into the payload rows."""
    annotations = match_annotations(txns, today)
    for row in payload["transactions"]:
        extra = annotations.get(row["id"])
        if extra:
            row.update(extra)


def _txn_dict(t: Transaction, balance: float | None = None) -> dict:
    d = {
        "id": t.id,
        "register_account_id": t.register_account_id,
        "date": str(t.date),
        "description": t.description,
        "amount": float(t.amount),
        "source": t.source,
        "bank_description": t.bank_description,
        "matched_to_id": t.matched_to_id,
        "is_reconciled": t.is_reconciled,
        "notes": t.notes,
    }
    if balance is not None:
        d["balance"] = round(balance, 2)
    return d


def _compute_running_balance(opening: float, txns: list[Transaction]) -> dict:
    """
    Computes running balances for all transactions in chronological order.
    Running balance includes:
      - All manual transactions
      - Unmatched bank transactions (real money that hasn't been ledgered yet)
      - Matched bank rows are excluded (their manual counterpart is already counted)
    Returns:
      transactions     — ascending date order; every row has 'balance'
      forecast_balance — opening + all manual transactions
      verified_balance — opening + reconciled manual transactions only
    """
    sorted_txns = sorted(txns, key=lambda t: (t.date, t.id))

    running_bal = opening
    forecast_bal = opening
    verified_bal = opening
    result = []
    for t in sorted_txns:
        if t.source == "manual":
            running_bal += float(t.amount)
            forecast_bal += float(t.amount)
            if t.is_reconciled:
                verified_bal += float(t.amount)
            result.append(_txn_dict(t, running_bal))
        else:
            # Unmatched bank rows affect the running balance display;
            # matched bank rows don't (the manual counterpart already counted).
            if t.matched_to_id is None:
                running_bal += float(t.amount)
            result.append(_txn_dict(t, running_bal))

    return {
        "transactions": result,
        "forecast_balance": round(forecast_bal, 2),
        "verified_balance": round(verified_bal, 2),
    }


# ── Register Accounts ─────────────────────────────────────────────────────────

class RegisterAccountCreate(BaseModel):
    name: str
    opening_balance: float = 0.0
    institution: str | None = None
    notes: str | None = None
    is_default: bool = False


class RegisterAccountUpdate(BaseModel):
    name: str | None = None
    notes: str | None = None
    institution: str | None = None
    cutoff_date: DateType | None = None
    cutoff_balance: float | None = None
    clear_cutoff: bool = False  # explicitly clear the cutoff when True
    is_default: bool | None = None


def _acct_dict(acct: RegisterAccount, current_balance: float) -> dict:
    return {
        "id": acct.id,
        "name": acct.name,
        "opening_balance": float(acct.opening_balance),
        "notes": acct.notes,
        "institution": acct.institution or "",
        "cutoff_date": acct.cutoff_date.isoformat() if acct.cutoff_date else None,
        "cutoff_balance": float(acct.cutoff_balance) if acct.cutoff_balance is not None else None,
        "current_balance": round(current_balance, 2),
        "is_default": bool(acct.is_default),
    }


def _effective_opening(acct: RegisterAccount) -> tuple[float, "DateType | None"]:
    """Return (starting_balance, cutoff_date_or_None) for running-balance calculations."""
    if acct.cutoff_date is not None and acct.cutoff_balance is not None:
        return float(acct.cutoff_balance), acct.cutoff_date
    return float(acct.opening_balance), None


@router.get("/accounts/timeseries")
def register_accounts_timeseries(db: Session = Depends(get_db)):
    """
    Return month-end balances for every register account, derived from transactions.
    Used by the dashboard to include cash accounts in charts and asset allocation.
    """
    accounts = db.query(RegisterAccount).order_by(RegisterAccount.id).all()
    if not accounts:
        return {"periods": [], "accounts": []}

    all_period_set: set[str] = set()
    result_accounts = []

    for acct in accounts:
        opening, cutoff_date = _effective_opening(acct)

        txns = (
            db.query(Transaction)
            .filter(Transaction.register_account_id == acct.id)
            .order_by(Transaction.date)
            .all()
        )

        # Same counting logic as _compute_running_balance:
        # manual rows always count; bank rows only if unmatched
        qualifying = [
            t for t in txns
            if t.source == "manual" or (t.source == "bank" and t.matched_to_id is None)
        ]
        if cutoff_date:
            qualifying = [t for t in qualifying if t.date >= cutoff_date]

        if not qualifying:
            # Still emit the account with current balance for the latest period
            continue

        # Compute balance at end of each month that has transactions
        months = sorted({(t.date.year, t.date.month) for t in qualifying})
        running = opening
        txn_idx = 0
        period_balances: dict[str, float] = {}

        for year, month in months:
            last_day = monthrange(year, month)[1]
            month_end = DateType(year, month, last_day)
            while txn_idx < len(qualifying) and qualifying[txn_idx].date <= month_end:
                running += float(qualifying[txn_idx].amount)
                txn_idx += 1
            key = month_end.isoformat()
            period_balances[key] = running
            all_period_set.add(key)

        result_accounts.append({"id": acct.id, "name": acct.name, "period_balances": period_balances})

    periods = sorted(all_period_set)

    # Convert period_balances dict → values array, forward-filling missing months
    for acct in result_accounts:
        pb = acct.pop("period_balances")
        last_val: float | None = None
        values = []
        for p in periods:
            if p in pb:
                last_val = pb[p]
            values.append(last_val)
        acct["values"] = values

    return {"periods": periods, "accounts": result_accounts}


# Manage Accounts flags a cash account whose newest row is older than this: a monthly statement
# imported a few days after it closes stays unflagged until the next one is due.
REGISTER_BEHIND_DAYS = 40


@router.get("/accounts")
def list_register_accounts(x_flow_today: str | None = Header(None), db: Session = Depends(get_db)):
    accounts = db.query(RegisterAccount).order_by(RegisterAccount.id).all()
    if not accounts:
        return []

    # One query: sum each account's manual transactions, honoring per-account cutoff.
    sum_rows = db.execute(text("""
        SELECT t.register_account_id, COALESCE(SUM(t.amount), 0)
          FROM transactions t
          JOIN register_accounts a ON a.id = t.register_account_id
         WHERE t.source = 'manual'
           AND (
                a.cutoff_date IS NULL
             OR a.cutoff_balance IS NULL
             OR t.date >= a.cutoff_date
           )
         GROUP BY t.register_account_id
    """)).fetchall()
    sums: dict[int, float] = {row[0]: float(row[1]) for row in sum_rows}

    signals = account_signals(db)
    today = balance_sheet.client_today(x_flow_today, DateType.today())

    result = []
    for acct in accounts:
        opening, _ = _effective_opening(acct)
        row = _acct_dict(acct, opening + sums.get(acct.id, 0.0))
        sig = signals.get(acct.id, {})
        bank, any_ = sig.get("newest_bank"), sig.get("newest_any")
        newest = bank or any_
        row["latest_date"] = newest.isoformat() if newest else None
        row["latest_source"] = ("bank" if bank else "manual") if newest else None
        row["behind"] = newest is None or (today - newest).days > REGISTER_BEHIND_DAYS
        row["unmatched"] = sig.get("unmatched", 0)
        row["pending"] = sig.get("pending", 0)
        result.append(row)
    return result


def account_signals(db: Session) -> dict[int, dict]:
    """Per cash account, in one query: the newest bank-imported row and the newest row of any kind
    (Manage Accounts' "behind"), and the bank rows waiting in Reconcile: ``unmatched`` (no ledger
    entry matched, not marked reconciled: the reconcile view's unmatched_bank rule) and ``pending``
    (auto-matched, awaiting confirmation)."""
    rows = db.execute(text("""
        SELECT register_account_id,
               MAX(date) FILTER (WHERE source = 'bank'),
               MAX(date),
               COUNT(*) FILTER (WHERE source = 'bank' AND matched_to_id IS NULL AND NOT is_reconciled),
               COUNT(*) FILTER (WHERE source = 'bank' AND matched_to_id IS NOT NULL AND NOT is_reconciled)
          FROM transactions GROUP BY register_account_id
    """)).fetchall()
    return {aid: {"newest_bank": bank, "newest_any": any_, "unmatched": int(unmatched), "pending": int(pending)}
            for aid, bank, any_, unmatched, pending in rows}


@router.post("/accounts", status_code=201)
def create_register_account(body: RegisterAccountCreate, db: Session = Depends(get_db)):
    existing = db.query(RegisterAccount).filter(RegisterAccount.name == body.name).first()
    if existing:
        raise HTTPException(status_code=409, detail="Account name already exists")
    if body.is_default:
        db.query(RegisterAccount).update({"is_default": False})
    acct = RegisterAccount(name=body.name, opening_balance=body.opening_balance,
                           institution=(body.institution or "").strip() or None,
                           notes=(body.notes or "").strip() or None, is_default=body.is_default)
    db.add(acct)
    change_log.record(db, "register", "add_account", f"Added cash account {change_log.clip(acct.name)}",
                      {"name": acct.name, "opening_balance": float(acct.opening_balance), "is_default": bool(acct.is_default)})
    db.commit()
    db.refresh(acct)
    return _acct_dict(acct, float(acct.opening_balance))


@router.patch("/accounts/{account_id}")
def update_register_account(account_id: int, body: RegisterAccountUpdate, db: Session = Depends(get_db)):
    acct = db.get(RegisterAccount, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")
    before = {"name": acct.name, "notes": acct.notes, "institution": acct.institution, "cutoff_date": str(acct.cutoff_date or ""),
              "cutoff_balance": None if acct.cutoff_balance is None else float(acct.cutoff_balance), "is_default": bool(acct.is_default)}
    if body.name is not None:
        conflict = db.query(RegisterAccount).filter(
            RegisterAccount.name == body.name,
            RegisterAccount.id != account_id,
        ).first()
        if conflict:
            raise HTTPException(status_code=409, detail="Account name already exists")
        acct.name = body.name
    if body.notes is not None:
        acct.notes = body.notes or None
    if body.institution is not None:
        acct.institution = body.institution.strip() or None
    if body.clear_cutoff:
        acct.cutoff_date = None
        acct.cutoff_balance = None
    elif body.cutoff_date is not None:
        acct.cutoff_date = body.cutoff_date
        acct.cutoff_balance = body.cutoff_balance  # may be None if user only set date
    if body.is_default is not None:
        if body.is_default:
            db.query(RegisterAccount).filter(RegisterAccount.id != account_id).update({"is_default": False})
        acct.is_default = body.is_default
    after = {"name": acct.name, "notes": acct.notes, "institution": acct.institution, "cutoff_date": str(acct.cutoff_date or ""),
             "cutoff_balance": None if acct.cutoff_balance is None else float(acct.cutoff_balance), "is_default": bool(acct.is_default)}
    changed = {k: v for k, v in after.items() if before[k] != v}
    if changed:
        change_log.record(db, "register", "edit_account", f"Edited cash account {change_log.clip(before['name'])} ({', '.join(changed)})",
                          {"id": acct.id, "before": {k: before[k] for k in changed}, "after": changed})
    db.commit()
    db.refresh(acct)
    opening, cutoff_date = _effective_opening(acct)
    query = "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE register_account_id = :aid AND source = 'manual'"
    params: dict = {"aid": acct.id}
    if cutoff_date:
        query += " AND date >= :cutoff"
        params["cutoff"] = cutoff_date
    row = db.execute(text(query), params).scalar()
    return _acct_dict(acct, opening + float(row))


@router.delete("/accounts/{account_id}", status_code=204)
def delete_register_account(account_id: int, db: Session = Depends(get_db)):
    acct = db.get(RegisterAccount, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")
    import_profile_resolve.unlink_account(db, "cash", account_id)
    change_log.record(db, "register", "delete_account", f"Deleted cash account {change_log.clip(acct.name)} and its transactions", {"id": acct.id, "name": acct.name})
    db.delete(acct)
    db.commit()


# ── Transactions ──────────────────────────────────────────────────────────────

class TransactionCreate(BaseModel):
    date: DateType
    description: str
    amount: float  # positive=credit, negative=debit
    notes: str | None = None


class TransactionUpdate(BaseModel):
    date: DateType | None = None
    description: str | None = None
    amount: float | None = None
    notes: str | None = None


@router.get("/{account_id}/recurring-patterns")
def get_recurring_patterns(account_id: int, days_ahead: int = 60, db: Session = Depends(get_db)):
    acct = db.get(RegisterAccount, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")

    _, cutoff_date = _effective_opening(acct)
    query = db.query(Transaction).filter(Transaction.register_account_id == account_id)
    if cutoff_date:
        query = query.filter(Transaction.date >= cutoff_date)
    txns = query.all()

    from recurring import detect_recurring_patterns
    patterns = detect_recurring_patterns(txns, days_ahead=days_ahead)

    return [
        {
            "description": p.description,
            "amount": p.amount,
            "next_expected_date": p.next_expected_date.isoformat(),
            "frequency": p.frequency,
            "confidence": p.confidence,
            "occurrence_count": p.occurrence_count,
        }
        for p in patterns
    ]


@router.get("/{account_id}/transactions")
def list_transactions(account_id: int, db: Session = Depends(get_db)):
    acct = db.get(RegisterAccount, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")

    opening, cutoff_date = _effective_opening(acct)
    query = db.query(Transaction).filter(Transaction.register_account_id == account_id)
    if cutoff_date:
        query = query.filter(Transaction.date >= cutoff_date)
    txns = query.all()
    result = _compute_running_balance(opening, txns)
    _annotate_matches(result, txns, DateType.today())
    result["cutoff_date"] = acct.cutoff_date.isoformat() if acct.cutoff_date else None
    result["cutoff_balance"] = float(acct.cutoff_balance) if acct.cutoff_balance is not None else None
    return result


@router.get("/{account_id}/export.csv")
def export_register_csv(account_id: int, db: Session = Depends(get_db)):
    """The account's ledger as CSV: every row after the cutoff with the running balance as the
    register shows it (manual rows and unmatched bank rows count; a matched bank row is listed with
    the balance unchanged). The page's search and filters don't apply."""
    acct = db.get(RegisterAccount, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")
    opening, cutoff_date = _effective_opening(acct)
    query = db.query(Transaction).filter(Transaction.register_account_id == account_id)
    if cutoff_date:
        query = query.filter(Transaction.date >= cutoff_date)
    result = _compute_running_balance(opening, query.all())
    return exports.csv_response(exports.REGISTER_COLUMNS, exports.register_rows(result["transactions"]), acct.name)


@router.post("/{account_id}/transactions", status_code=201)
def create_transaction(account_id: int, body: TransactionCreate, db: Session = Depends(get_db)):
    acct = db.get(RegisterAccount, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")

    txn = Transaction(
        register_account_id=account_id,
        date=body.date,
        description=body.description,
        amount=body.amount,
        source="manual",
        notes=body.notes,
    )
    db.add(txn)
    change_log.record(db, "register", "add", f"Added {txn.date} {change_log.clip(txn.description)} {change_log.money(txn.amount)} in {change_log.clip(acct.name)}",
                      {"account_id": acct.id, "date": str(txn.date), "description": txn.description, "amount": float(txn.amount)})
    db.commit()
    db.refresh(txn)
    return _txn_dict(txn)


@router.patch("/transactions/{txn_id}")
def update_transaction(txn_id: int, body: TransactionUpdate, db: Session = Depends(get_db)):
    txn = db.get(Transaction, txn_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if txn.source != "manual":
        raise HTTPException(status_code=422, detail="Only manual transactions can be edited")
    before = {"date": str(txn.date), "description": txn.description, "amount": float(txn.amount), "notes": txn.notes}

    if body.date is not None:
        txn.date = body.date
    if body.description is not None:
        txn.description = body.description
    if body.amount is not None:
        txn.amount = body.amount
    if "notes" in body.model_fields_set:
        txn.notes = body.notes
    after = {"date": str(txn.date), "description": txn.description, "amount": float(txn.amount), "notes": txn.notes}
    changed = {k: v for k, v in after.items() if before[k] != v}
    if changed:
        acct = db.get(RegisterAccount, txn.register_account_id)
        change_log.record(db, "register", "edit", f"Edited {after['date']} {change_log.clip(after['description'])} {change_log.money(after['amount'])} in {change_log.clip(acct.name if acct else '')}",
                          {"id": txn.id, "before": {k: before[k] for k in changed}, "after": changed})

    db.commit()
    db.refresh(txn)
    return _txn_dict(txn)


@router.delete("/transactions/{txn_id}", status_code=204)
def delete_transaction(txn_id: int, db: Session = Depends(get_db)):
    txn = db.get(Transaction, txn_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    # If this transaction is part of a matched pair, unlink the partner
    if txn.matched_to_id is not None:
        partner = db.get(Transaction, txn.matched_to_id)
        if partner:
            partner.matched_to_id = None
            partner.is_reconciled = False

    acct = db.get(RegisterAccount, txn.register_account_id)
    change_log.record(db, "register", "delete", f"Deleted {txn.date} {change_log.clip(txn.description)} {change_log.money(txn.amount)} from {change_log.clip(acct.name if acct else '')}",
                      {"id": txn.id, "source": txn.source, "date": str(txn.date), "description": txn.description, "amount": float(txn.amount)})
    db.delete(txn)
    db.commit()


# ── Import (through the account's import format) ──────────────────────────────
#
# Every file is read with one of the account's import formats (import_profile):
# the format decides the columns, the sign and whether its rows are bank
# statement lines or manual ledger entries (Spec.row_source). No linked format
# reads the file: 422 no_profile with a drafted format; the file's header changed:
# 409 profile_drift; the sign is contradicted by the file: 422 sign_reversed.
# Rows the format can't read (a bad date or amount) are listed, and the import is
# refused until the user asks to import the rest (allow_errors).

async def _read_upload(file: UploadFile) -> tuple[bytes, str]:
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
    return content, file.filename or "upload"


def _history(db, account_id: int, source: str, lo, hi) -> list[tuple]:
    return [(d, float(a), t or "") for d, a, t in db.execute(text("""
        SELECT date, amount, COALESCE(bank_description, description) FROM transactions
         WHERE register_account_id = :aid AND source = :src AND date BETWEEN :lo AND :hi
    """), {"aid": account_id, "src": source, "lo": lo, "hi": hi}).fetchall()]


def _read_with_format(db, acct: RegisterAccount, content: bytes, filename: str, profile_id: int | None):
    res = import_profile_resolve.resolve_for_account(db, content, filename, "cash", acct.id, profile_id)
    if res.status != "match":
        raise import_profile_resolve.problem(db, res, content, filename, "cash", acct.id)
    c, parsed = res.candidate, res.parsed
    history = []
    if parsed.rows:
        lo, hi = min(r.date for r in parsed.rows), max(r.date for r in parsed.rows)
        history = _history(db, acct.id, "bank" if c.spec.row_source == "bank" else "manual", lo, hi)
    check = ip.verify_sign(parsed, c.spec, history)
    if check.status == "reversed":
        raise import_profile_resolve.sign_problem(check, c)
    import_profile_resolve.mark_previewed(db, c, parsed)
    return c, parsed, check


def _format_info(c, parsed, check) -> dict:
    return {
        "profile": import_profile_resolve.summary(c),
        "sign_check": {"status": check.status, "reason": check.reason},
        "errors": parsed.errors[:20],
        "error_count": len(parsed.errors),
    }


@router.post("/{account_id}/import/check")
async def check_import(
    account_id: int,
    file: UploadFile = File(...),
    profile_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """
    Dry-run: read the file with the account's format and return what would be imported. Writes nothing.
    """
    acct = db.get(RegisterAccount, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")
    content, filename = await _read_upload(file)
    c, parsed, check = _read_with_format(db, acct, content, filename, profile_id)
    info = _format_info(c, parsed, check)
    _, cutoff_date = _effective_opening(acct)

    if c.spec.row_source == "manual":
        rows = ip.to_ledger_rows(parsed)
        if not rows:
            return {"format": "ledger", "would_import": 0, "would_skip": 0, **info}
        lo, hi = date_bounds(rows)
        new_rows, would_skip = plan_ledger_import(rows, _existing_manual_counts(db, account_id, lo, hi))
        return {"format": "ledger", "would_import": len(new_rows), "would_skip": would_skip, **info}

    bank_rows = ip.to_bank_rows(parsed)
    candidates = []
    would_skip = 0
    for row in bank_rows:
        if cutoff_date and row.date < cutoff_date:
            would_skip += 1
            continue
        candidates.append(row)

    new_rows = []
    if candidates:
        lo, hi = date_bounds(candidates)
        new_rows, dupe_skipped = plan_bank_import(
            candidates,
            _line_coverage(db, account_id, lo, hi),
            _generic_coverage(db, account_id, lo, hi),
        )
        would_skip += dupe_skipped

    # Estimate fuzzy auto-matches for genuinely new rows
    would_auto_match = 0
    if new_rows:
        unmatched_manual = db.execute(
            text("""
                SELECT id, date, amount, description FROM transactions
                WHERE register_account_id = :aid
                  AND source = 'manual'
                  AND matched_to_id IS NULL
                  AND is_reconciled = FALSE
            """),
            {"aid": account_id},
        ).fetchall()
        bank_infos = [
            TxnInfo(id=i, date=r.date, amount=float(r.amount), description=r.bank_description or "")
            for i, r in enumerate(new_rows)
        ]
        manual_infos = [
            TxnInfo(id=r.id, date=r.date, amount=float(r.amount), description=r.description)
            for r in unmatched_manual
        ]
        would_auto_match = len(assign_matches(bank_infos, manual_infos))

    would_import = len(new_rows)
    return {
        "format": "bank",
        "other_account_match": _other_account_match(db, acct, bank_rows),
        "would_import": would_import,
        "would_skip": would_skip,
        "would_auto_match": would_auto_match,
        "would_unmatched": would_import - would_auto_match,
        **info,
    }


@router.post("/{account_id}/import")
async def import_csv(
    account_id: int,
    file: UploadFile = File(...),
    allow_other_account: bool = Query(False),
    allow_errors: bool = Query(False),
    profile_id: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """
    Import a file with the account's import format. Bank-statement formats import
    source='bank' rows and run auto-matching; ledger formats (row_source 'manual')
    import source='manual' rows with their reconciled flag (seeding the ledger from
    a tracking spreadsheet). A bank file whose rows are mostly already in another
    account is refused with 409 unless ``allow_other_account`` confirms it (see
    _other_account_match); a file with unreadable rows is refused with 422 unless
    ``allow_errors`` says to import the rest.
    """
    acct = db.get(RegisterAccount, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")
    content, filename = await _read_upload(file)
    c, parsed, check = _read_with_format(db, acct, content, filename, profile_id)
    if parsed.errors and not allow_errors:
        raise HTTPException(status_code=422, detail={
            "code": "row_errors", "errors": parsed.errors[:20], "error_count": len(parsed.errors),
            "message": f"{len(parsed.errors)} row(s) couldn't be read with the format \"{c.name}\". "
                       "Fix the format, or confirm to import the rest."})

    if c.spec.row_source == "bank" and not allow_other_account:
        other = _other_account_match(db, acct, ip.to_bank_rows(parsed))
        if other:
            raise HTTPException(status_code=409, detail=other["message"]
                                + " Pick that account, or confirm to import here anyway.")

    try:
        ensure_daily_backup(db)
    except Exception:
        pass  # backup failure must never block an import

    _, cutoff_date = _effective_opening(acct)
    import_profile_resolve.touch(db, c)
    if not acct.institution and c.institution:
        acct.institution = c.institution          # "filled in from the import format on first import"
    if c.spec.row_source == "manual":
        result = _import_ledger_rows(ip.to_ledger_rows(parsed), account_id, db, cutoff_date)
    else:
        result = _import_bank_rows(ip.to_bank_rows(parsed), account_id, db, cutoff_date)
    extra = (f", {result['auto_matched']} auto-matched" if result.get("auto_matched") else "")
    change_log.record(db, "register", "import", f"Imported {change_log.clip(filename, 40)} into {change_log.clip(acct.name)}: {result.get('imported', 0)} rows ({result.get('skipped', 0)} already on file{extra})",
                      {"account_id": acct.id, "format": c.name, "filename": filename, **{k: v for k, v in result.items() if isinstance(v, int)}})
    db.commit()
    return {**result, **_format_info(c, parsed, check)}


def _import_ledger_rows(rows, account_id: int, db, cutoff_date=None) -> dict:
    """
    Import rows from the user's manual tracking spreadsheet as manual transactions.
    Already-reconciled entries (X flag) are marked is_reconciled=True.
    Duplicates (same date + amount + description) are skipped, counting repeats:
    a sheet listing the same entry twice imports both unless the ledger already
    holds two copies.
    No auto-matching is run — these ARE the ledger.
    """
    if not rows:
        return {"format": "ledger", "imported": 0, "skipped": 0, "auto_matched": 0, "unmatched": 0}

    lo, hi = date_bounds(rows)
    new_rows, skipped = plan_ledger_import(rows, _existing_manual_counts(db, account_id, lo, hi))

    imported = 0
    for row in new_rows:
        txn = Transaction(
            register_account_id=account_id,
            date=row.date,
            description=row.description,
            amount=row.amount,
            source="manual",
            is_reconciled=row.is_reconciled,
        )
        db.add(txn)
        imported += 1

    db.commit()
    return {
        "format": "ledger",
        "imported": imported,
        "skipped": skipped,
        "auto_matched": 0,
        "unmatched": 0,
    }


def _import_bank_rows(rows, account_id: int, db, cutoff_date=None) -> dict:
    """
    Import rows from a bank export as bank transactions, then run auto-matching
    against any existing unmatched manual transactions.

    Dedup logic (in order), applied by count so repeated identical statement lines
    survive — see the "Import de-duplication" section above:
      1. Skip rows whose exact statement line is already on file (date, amount,
         normalized description) — that includes the bank half of a reconciled pair.
      2. Skip rows matching a stored line truncated at a different export width.
      3. Skip what remains against (date, amount) counts — stored bank rows the
         text tiers could not claim, plus reconciled manual entries with no bank
         row. The surplus imports and auto-matches against unreconciled entries.
    """
    candidates = [r for r in rows if not (cutoff_date and r.date < cutoff_date)]

    imported = 0
    auto_matched = 0
    new_bank_txns: list[Transaction] = []

    new_rows = []
    skipped = len(rows) - len(candidates)   # rows dropped by the account cutoff date
    if candidates:
        lo, hi = date_bounds(candidates)
        new_rows, dupe_skipped = plan_bank_import(
            candidates,
            _line_coverage(db, account_id, lo, hi),
            _generic_coverage(db, account_id, lo, hi),
        )
        skipped += dupe_skipped

    for row in new_rows:
        txn = Transaction(
            register_account_id=account_id,
            date=row.date,
            description=row.bank_description or "(bank import)",
            amount=row.amount,
            source="bank",
            bank_description=row.bank_description,
            notes=f"Check #{row.check_number}" if row.check_number else None,
        )
        db.add(txn)
        db.flush()
        imported += 1
        new_bank_txns.append(txn)

    if not new_bank_txns:
        db.commit()
        return {"format": "bank", "imported": imported, "skipped": skipped,
                "auto_matched": auto_matched, "unmatched": 0}

    # 3. Fuzzy auto-match remaining new bank rows against unreconciled manual entries
    unmatched_manual = (
        db.query(Transaction)
        .filter(
            Transaction.register_account_id == account_id,
            Transaction.source == "manual",
            Transaction.matched_to_id.is_(None),
            Transaction.is_reconciled == False,  # noqa: E712
        )
        .all()
    )

    bank_infos = [
        TxnInfo(id=t.id, date=t.date, amount=float(t.amount), description=t.bank_description or "")
        for t in new_bank_txns
    ]
    manual_infos = [
        TxnInfo(id=t.id, date=t.date, amount=float(t.amount), description=t.description)
        for t in unmatched_manual
    ]

    # One-to-one assignment: with repeated identical transactions, every new bank row
    # scores the same against every matching manual entry, so each needs its own
    # partner rather than all pointing at the first one.
    for suggestion in assign_matches(bank_infos, manual_infos):
        bank_txn = db.get(Transaction, suggestion.bank_id)
        manual_txn = db.get(Transaction, suggestion.manual_id)
        if bank_txn and manual_txn and bank_txn.matched_to_id is None and manual_txn.matched_to_id is None:
            bank_txn.matched_to_id = manual_txn.id
            manual_txn.matched_to_id = bank_txn.id
            _reconcile_manual_to_bank(manual_txn, bank_txn)
            auto_matched += 1

    db.commit()
    unmatched = imported - auto_matched
    return {"format": "bank", "imported": imported, "skipped": skipped,
            "auto_matched": auto_matched, "unmatched": unmatched}


# ── Reconciliation ─────────────────────────────────────────────────────────────

@router.get("/{account_id}/reconcile")
def get_reconcile_data(account_id: int, db: Session = Depends(get_db)):
    acct = db.get(RegisterAccount, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")

    today = DateType.today()

    # Only show past/present manual entries — future transactions haven't occurred yet
    unmatched_manual = (
        db.query(Transaction)
        .filter(
            Transaction.register_account_id == account_id,
            Transaction.source == "manual",
            Transaction.matched_to_id.is_(None),
            Transaction.is_reconciled == False,  # noqa: E712
            Transaction.date <= today,
        )
        .order_by(Transaction.date)
        .all()
    )

    unmatched_bank = (
        db.query(Transaction)
        .filter(
            Transaction.register_account_id == account_id,
            Transaction.source == "bank",
            Transaction.matched_to_id.is_(None),
            Transaction.is_reconciled == False,  # noqa: E712
        )
        .order_by(Transaction.date)
        .all()
    )

    # Pending matches: auto-matched pairs awaiting user confirmation
    pending_bank = (
        db.query(Transaction)
        .filter(
            Transaction.register_account_id == account_id,
            Transaction.source == "bank",
            Transaction.matched_to_id.isnot(None),
            Transaction.is_reconciled == False,  # noqa: E712
        )
        .order_by(Transaction.date)
        .all()
    )

    pending_matches = []
    for bank_txn in pending_bank:
        manual_txn = db.get(Transaction, bank_txn.matched_to_id)
        if manual_txn is None:
            continue
        bank_info = TxnInfo(id=bank_txn.id, date=bank_txn.date, amount=float(bank_txn.amount), description=bank_txn.bank_description or "")
        manual_info = TxnInfo(id=manual_txn.id, date=manual_txn.date, amount=float(manual_txn.amount), description=manual_txn.description)
        sug = suggest_matches([bank_info], [manual_info], min_score=0.0)
        score = sug[0].score if sug else 0.0
        description_score = sug[0].description_score if sug else 0.0
        pending_matches.append({
            "bank": _txn_dict(bank_txn),
            "manual": _txn_dict(manual_txn),
            "score": score,
            "description_score": description_score,
            "amount_diff": round(float(bank_txn.amount) - float(manual_txn.amount), 2),
            "date_diff": (bank_txn.date - manual_txn.date).days,
        })

    # Compute suggestions for fully unmatched bank rows
    bank_infos = [
        TxnInfo(id=t.id, date=t.date, amount=float(t.amount), description=t.bank_description or "")
        for t in unmatched_bank
    ]
    manual_infos = [
        TxnInfo(id=t.id, date=t.date, amount=float(t.amount), description=t.description)
        for t in unmatched_manual
    ]
    # assign_matches, not suggest_matches: two identical unmatched bank rows must be
    # offered two different manual entries, not the same one twice.
    suggestions = assign_matches(bank_infos, manual_infos)
    suggestion_map = {
        s.bank_id: {
            "manual_id": s.manual_id,
            "score": s.score,
            "description_score": s.description_score,
        }
        for s in suggestions
    }

    bank_out = []
    for t in unmatched_bank:
        d = _txn_dict(t)
        d["suggestion"] = suggestion_map.get(t.id)
        bank_out.append(d)

    return {
        "unmatched_manual": [_txn_dict(t) for t in unmatched_manual],
        "unmatched_bank": bank_out,
        "pending_matches": pending_matches,
    }


class MatchBody(BaseModel):
    bank_txn_id: int
    manual_txn_id: int


@router.post("/reconcile/match")
def match_transactions(body: MatchBody, db: Session = Depends(get_db)):
    bank_txn = db.get(Transaction, body.bank_txn_id)
    manual_txn = db.get(Transaction, body.manual_txn_id)

    if bank_txn is None or manual_txn is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if bank_txn.source != "bank":
        raise HTTPException(status_code=422, detail="bank_txn_id must reference a bank transaction")
    if manual_txn.source != "manual":
        raise HTTPException(status_code=422, detail="manual_txn_id must reference a manual transaction")

    # Unlink any existing partners first
    if bank_txn.matched_to_id:
        old = db.get(Transaction, bank_txn.matched_to_id)
        if old:
            old.matched_to_id = None
            old.is_reconciled = False
    if manual_txn.matched_to_id:
        old = db.get(Transaction, manual_txn.matched_to_id)
        if old:
            old.matched_to_id = None
            old.is_reconciled = False

    bank_txn.matched_to_id = manual_txn.id
    bank_txn.is_reconciled = True
    manual_txn.matched_to_id = bank_txn.id
    manual_txn.is_reconciled = True
    _reconcile_manual_to_bank(manual_txn, bank_txn)
    change_log.record(db, "reconcile", "match", f"Matched bank row {bank_txn.date} {change_log.clip(bank_txn.bank_description or bank_txn.description)} {change_log.money(bank_txn.amount)} to {change_log.clip(manual_txn.description)}",
                      {"bank_txn_id": bank_txn.id, "manual_txn_id": manual_txn.id})

    db.commit()
    return {"bank_txn_id": bank_txn.id, "manual_txn_id": manual_txn.id}


class UnmatchBody(BaseModel):
    txn_id: int


@router.post("/reconcile/unmatch")
def unmatch_transaction(body: UnmatchBody, db: Session = Depends(get_db)):
    txn = db.get(Transaction, body.txn_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="Transaction not found")

    if txn.matched_to_id:
        partner = db.get(Transaction, txn.matched_to_id)
        if partner:
            partner.matched_to_id = None
            partner.is_reconciled = False

    change_log.record(db, "reconcile", "unmatch", f"Unmatched {txn.date} {change_log.clip(txn.description or txn.bank_description)} {change_log.money(txn.amount)}",
                      {"txn_id": txn.id, "partner_id": txn.matched_to_id})
    txn.matched_to_id = None
    txn.is_reconciled = False

    db.commit()
    return {"ok": True}


@router.post("/transactions/{txn_id}/mark-reconciled")
def mark_reconciled(txn_id: int, db: Session = Depends(get_db)):
    txn = db.get(Transaction, txn_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    txn.is_reconciled = True
    change_log.record(db, "reconcile", "mark", f"Marked {txn.date} {change_log.clip(txn.description or txn.bank_description)} {change_log.money(txn.amount)} reconciled", {"txn_id": txn.id})
    db.commit()
    return _txn_dict(txn)


class AddToLedgerBody(BaseModel):
    bank_txn_id: int
    description: str | None = None
    notes: str | None = None


@router.post("/reconcile/add-to-ledger", status_code=201)
def add_to_ledger(body: AddToLedgerBody, db: Session = Depends(get_db)):
    """
    Create a manual transaction from a bank entry and immediately reconcile them.
    Used when a bank transaction was never pre-entered in the ledger.
    """
    bank_txn = db.get(Transaction, body.bank_txn_id)
    if bank_txn is None:
        raise HTTPException(status_code=404, detail="Bank transaction not found")
    if bank_txn.source != "bank":
        raise HTTPException(status_code=422, detail="Transaction is not a bank entry")

    description = body.description or bank_txn.bank_description or "(imported)"
    manual_txn = Transaction(
        register_account_id=bank_txn.register_account_id,
        date=bank_txn.date,
        description=description,
        amount=bank_txn.amount,
        source="manual",
        notes=body.notes,
        # Mark with bank_description so it is excluded from the description autocomplete
        bank_description=bank_txn.bank_description,
    )
    db.add(manual_txn)
    db.flush()

    bank_txn.matched_to_id = manual_txn.id
    bank_txn.is_reconciled = True
    manual_txn.matched_to_id = bank_txn.id
    manual_txn.is_reconciled = True
    change_log.record(db, "reconcile", "add_ledger", f"Added bank row {bank_txn.date} {change_log.clip(description)} {change_log.money(bank_txn.amount)} to the ledger",
                      {"bank_txn_id": bank_txn.id, "manual_txn_id": manual_txn.id})

    db.commit()
    db.refresh(manual_txn)
    return _txn_dict(manual_txn)


class BulkAddToLedgerBody(BaseModel):
    bank_txn_ids: list[int] = Field(max_length=10_000)


@router.post("/reconcile/bulk-add-to-ledger", status_code=200)
def bulk_add_to_ledger(body: BulkAddToLedgerBody, db: Session = Depends(get_db)):
    """Add multiple unmatched bank transactions to the ledger in one shot."""
    added = 0
    for bank_id in body.bank_txn_ids:
        bank_txn = db.get(Transaction, bank_id)
        if bank_txn is None or bank_txn.source != "bank" or bank_txn.matched_to_id is not None:
            continue
        description = bank_txn.bank_description or "(imported)"
        manual_txn = Transaction(
            register_account_id=bank_txn.register_account_id,
            date=bank_txn.date,
            description=description,
            amount=bank_txn.amount,
            source="manual",
            bank_description=bank_txn.bank_description,
        )
        db.add(manual_txn)
        db.flush()
        bank_txn.matched_to_id = manual_txn.id
        bank_txn.is_reconciled = True
        manual_txn.matched_to_id = bank_txn.id
        manual_txn.is_reconciled = True
        added += 1
    if added:
        change_log.record(db, "reconcile", "bulk_ledger", f"Added {added} bank rows to the ledger", {"count": added, "requested": len(body.bank_txn_ids)})
    db.commit()
    return {"added": added}
