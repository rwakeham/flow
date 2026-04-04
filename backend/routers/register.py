"""
Cash Flow / Register API endpoints.

All routes are prefixed /api/register and require authentication.
"""

from __future__ import annotations

from datetime import date as DateType

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from auth import require_auth
from database import get_db
from models import RegisterAccount, Transaction
from reconciler import TxnInfo, suggest_matches
from register_parser import FileFormat, parse_register_import

router = APIRouter(prefix="/api/register", dependencies=[Depends(require_auth)])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


# ── Helpers ────────────────────────────────────────────────────────────────────

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
    Returns:
      transactions    — list in ascending date order, each manual row has 'balance' (forecast)
      forecast_balance — total including all manual transactions
      verified_balance — total including only reconciled manual transactions
    """
    sorted_txns = sorted(txns, key=lambda t: (t.date, t.created_at or t.id))

    forecast_bal = opening
    verified_bal = opening
    result = []
    for t in sorted_txns:
        if t.source == "manual":
            forecast_bal += float(t.amount)
            if t.is_reconciled:
                verified_bal += float(t.amount)
            result.append(_txn_dict(t, forecast_bal))
        else:
            result.append(_txn_dict(t))

    return {
        "transactions": result,
        "forecast_balance": round(forecast_bal, 2),
        "verified_balance": round(verified_bal, 2),
    }


# ── Register Accounts ─────────────────────────────────────────────────────────

class RegisterAccountCreate(BaseModel):
    name: str
    opening_balance: float = 0.0


class RegisterAccountUpdate(BaseModel):
    name: str | None = None
    notes: str | None = None
    cutoff_date: DateType | None = None
    cutoff_balance: float | None = None
    clear_cutoff: bool = False  # explicitly clear the cutoff when True


def _acct_dict(acct: RegisterAccount, current_balance: float) -> dict:
    return {
        "id": acct.id,
        "name": acct.name,
        "opening_balance": float(acct.opening_balance),
        "notes": acct.notes,
        "cutoff_date": acct.cutoff_date.isoformat() if acct.cutoff_date else None,
        "cutoff_balance": float(acct.cutoff_balance) if acct.cutoff_balance is not None else None,
        "current_balance": round(current_balance, 2),
    }


def _effective_opening(acct: RegisterAccount) -> tuple[float, "DateType | None"]:
    """Return (starting_balance, cutoff_date_or_None) for running-balance calculations."""
    if acct.cutoff_date is not None and acct.cutoff_balance is not None:
        return float(acct.cutoff_balance), acct.cutoff_date
    return float(acct.opening_balance), None


@router.get("/accounts")
def list_register_accounts(db: Session = Depends(get_db)):
    accounts = db.query(RegisterAccount).order_by(RegisterAccount.id).all()
    result = []
    for acct in accounts:
        opening, cutoff_date = _effective_opening(acct)
        query = "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE register_account_id = :aid AND source = 'manual'"
        params: dict = {"aid": acct.id}
        if cutoff_date:
            query += " AND date >= :cutoff"
            params["cutoff"] = cutoff_date
        row = db.execute(text(query), params).scalar()
        current_balance = opening + float(row)
        result.append(_acct_dict(acct, current_balance))
    return result


@router.post("/accounts", status_code=201)
def create_register_account(body: RegisterAccountCreate, db: Session = Depends(get_db)):
    existing = db.query(RegisterAccount).filter(RegisterAccount.name == body.name).first()
    if existing:
        raise HTTPException(status_code=409, detail="Account name already exists")
    acct = RegisterAccount(name=body.name, opening_balance=body.opening_balance)
    db.add(acct)
    db.commit()
    db.refresh(acct)
    return _acct_dict(acct, float(acct.opening_balance))


@router.patch("/accounts/{account_id}")
def update_register_account(account_id: int, body: RegisterAccountUpdate, db: Session = Depends(get_db)):
    acct = db.get(RegisterAccount, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")
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
    if body.clear_cutoff:
        acct.cutoff_date = None
        acct.cutoff_balance = None
    elif body.cutoff_date is not None:
        acct.cutoff_date = body.cutoff_date
        acct.cutoff_balance = body.cutoff_balance  # may be None if user only set date
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


@router.get("/{account_id}/transactions")
def list_transactions(account_id: int, db: Session = Depends(get_db)):
    acct = db.get(RegisterAccount, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")

    opening, _ = _effective_opening(acct)
    txns = db.query(Transaction).filter(Transaction.register_account_id == account_id).all()
    return _compute_running_balance(opening, txns)


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

    if body.date is not None:
        txn.date = body.date
    if body.description is not None:
        txn.description = body.description
    if body.amount is not None:
        txn.amount = body.amount
    if body.notes is not None:
        txn.notes = body.notes

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

    db.delete(txn)
    db.commit()


# ── Import (bank export or manual ledger spreadsheet) ─────────────────────────

@router.post("/{account_id}/import/check")
async def check_import(
    account_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Dry-run: parse the file and return what would be imported without writing anything.
    """
    acct = db.get(RegisterAccount, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")

    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")

    try:
        result = parse_register_import(content)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not parse file: {exc}")

    if result.format == FileFormat.UNKNOWN:
        raise HTTPException(status_code=422, detail="No transactions found in file")

    _, cutoff_date = _effective_opening(acct)

    if result.format == FileFormat.LEDGER:
        would_import = 0
        would_skip = 0
        for row in result.ledger_rows:
            existing = db.execute(
                text("""
                    SELECT id FROM transactions
                    WHERE register_account_id = :aid
                      AND source = 'manual'
                      AND date = :dt
                      AND amount = :amt
                      AND description = :desc
                    LIMIT 1
                """),
                {"aid": account_id, "dt": row.date, "amt": float(row.amount), "desc": row.description},
            ).fetchone()
            if existing:
                would_skip += 1
            else:
                would_import += 1
        return {"format": "ledger", "would_import": would_import, "would_skip": would_skip}

    # Bank format
    new_rows = []
    would_skip = 0
    for row in result.bank_rows:
        if cutoff_date and row.date < cutoff_date:
            would_skip += 1
            continue
        # Would this be skipped as a bank duplicate?
        bank_dupe = db.execute(
            text("""
                SELECT id FROM transactions
                WHERE register_account_id = :aid
                  AND source = 'bank'
                  AND date = :dt
                  AND amount = :amt
                  AND bank_description = :desc
                LIMIT 1
            """),
            {"aid": account_id, "dt": row.date, "amt": row.amount, "desc": row.bank_description},
        ).fetchone()
        if bank_dupe:
            would_skip += 1
            continue

        # Would this match an already-reconciled manual entry?
        reconciled_manual = db.execute(
            text("""
                SELECT id FROM transactions
                WHERE register_account_id = :aid
                  AND source = 'manual'
                  AND date = :dt
                  AND amount = :amt
                  AND is_reconciled = TRUE
                LIMIT 1
            """),
            {"aid": account_id, "dt": row.date, "amt": float(row.amount)},
        ).fetchone()
        if reconciled_manual:
            would_skip += 1
            continue

        new_rows.append(row)

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
        suggestions = suggest_matches(bank_infos, manual_infos)
        would_auto_match = len(suggestions)

    would_import = len(new_rows)
    return {
        "format": "bank",
        "would_import": would_import,
        "would_skip": would_skip,
        "would_auto_match": would_auto_match,
        "would_unmatched": would_import - would_auto_match,
    }


@router.post("/{account_id}/import")
async def import_csv(
    account_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Auto-detects file format:
    - Bank export (col 1 = numeric amount)  → imported as source='bank', runs auto-matching
    - Ledger spreadsheet (col 1 = description) → imported as source='manual', reconciled
      flag preserved; used to seed the ledger from an existing tracking spreadsheet
    """
    acct = db.get(RegisterAccount, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")

    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")

    try:
        result = parse_register_import(content)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not parse file: {exc}")

    if result.format == FileFormat.UNKNOWN:
        raise HTTPException(status_code=422, detail="No transactions found in file")

    _, cutoff_date = _effective_opening(acct)
    if result.format == FileFormat.LEDGER:
        return _import_ledger_rows(result.ledger_rows, account_id, db, cutoff_date)
    else:
        return _import_bank_rows(result.bank_rows, account_id, db, cutoff_date)


def _import_ledger_rows(rows, account_id: int, db, cutoff_date=None) -> dict:
    """
    Import rows from the user's manual tracking spreadsheet as manual transactions.
    Already-reconciled entries (X flag) are marked is_reconciled=True.
    Duplicates (same date + amount + description) are skipped.
    No auto-matching is run — these ARE the ledger.
    """
    imported = 0
    skipped = 0
    for row in rows:
        existing = db.execute(
            text("""
                SELECT id FROM transactions
                WHERE register_account_id = :aid
                  AND source = 'manual'
                  AND date = :dt
                  AND amount = :amt
                  AND description = :desc
                LIMIT 1
            """),
            {"aid": account_id, "dt": row.date, "amt": row.amount, "desc": row.description},
        ).fetchone()
        if existing:
            skipped += 1
            continue

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

    Dedup logic (in order):
      1. Skip if an identical bank row already exists (same date/amount/description).
      2. Skip if a reconciled manual entry already covers the same date and amount —
         the manual entry is treated as authoritative, so deleting a bank row makes
         the skip permanent even on re-import.
      3. Otherwise import as an unmatched bank row and run fuzzy auto-matching.
    """
    imported = 0
    auto_matched = 0
    new_bank_txns: list[Transaction] = []

    for row in rows:
        if cutoff_date and row.date < cutoff_date:
            continue

        # 1. Skip exact bank duplicates
        existing_bank = db.execute(
            text("""
                SELECT id FROM transactions
                WHERE register_account_id = :aid
                  AND source = 'bank'
                  AND date = :dt
                  AND amount = :amt
                  AND bank_description = :desc
                LIMIT 1
            """),
            {"aid": account_id, "dt": row.date, "amt": row.amount, "desc": row.bank_description},
        ).fetchone()
        if existing_bank:
            continue

        # 2. Skip if a reconciled manual entry already covers this date + amount.
        #    This treats the manual entry as the authoritative record and makes
        #    deletion of bank rows permanent (they won't re-appear on re-import).
        reconciled_manual = db.execute(
            text("""
                SELECT id FROM transactions
                WHERE register_account_id = :aid
                  AND source = 'manual'
                  AND date = :dt
                  AND amount = :amt
                  AND is_reconciled = TRUE
                LIMIT 1
            """),
            {"aid": account_id, "dt": row.date, "amt": float(row.amount)},
        ).fetchone()
        if reconciled_manual:
            continue

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
        return {"format": "bank", "imported": imported, "auto_matched": auto_matched, "unmatched": 0}

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

    suggestions = suggest_matches(bank_infos, manual_infos)

    used_manual_ids: set[int] = set()
    for suggestion in sorted(suggestions, key=lambda s: -s.score):
        if suggestion.manual_id in used_manual_ids:
            continue
        bank_txn = db.get(Transaction, suggestion.bank_id)
        manual_txn = db.get(Transaction, suggestion.manual_id)
        if bank_txn and manual_txn and bank_txn.matched_to_id is None and manual_txn.matched_to_id is None:
            bank_txn.matched_to_id = manual_txn.id
            bank_txn.is_reconciled = True
            manual_txn.matched_to_id = bank_txn.id
            manual_txn.is_reconciled = True
            used_manual_ids.add(suggestion.manual_id)
            auto_matched += 1

    db.commit()
    unmatched = imported - auto_matched
    return {"format": "bank", "imported": imported, "auto_matched": auto_matched, "unmatched": unmatched}


# ── Reconciliation ─────────────────────────────────────────────────────────────

@router.get("/{account_id}/reconcile")
def get_reconcile_data(account_id: int, db: Session = Depends(get_db)):
    acct = db.get(RegisterAccount, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")

    unmatched_manual = (
        db.query(Transaction)
        .filter(
            Transaction.register_account_id == account_id,
            Transaction.source == "manual",
            Transaction.matched_to_id.is_(None),
            Transaction.is_reconciled == False,  # noqa: E712
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

    # Compute suggestions
    bank_infos = [
        TxnInfo(id=t.id, date=t.date, amount=float(t.amount), description=t.bank_description or "")
        for t in unmatched_bank
    ]
    manual_infos = [
        TxnInfo(id=t.id, date=t.date, amount=float(t.amount), description=t.description)
        for t in unmatched_manual
    ]
    suggestions = suggest_matches(bank_infos, manual_infos)
    suggestion_map = {s.bank_id: {"manual_id": s.manual_id, "score": s.score} for s in suggestions}

    bank_out = []
    for t in unmatched_bank:
        d = _txn_dict(t)
        d["suggestion"] = suggestion_map.get(t.id)
        bank_out.append(d)

    return {
        "unmatched_manual": [_txn_dict(t) for t in unmatched_manual],
        "unmatched_bank": bank_out,
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
    )
    db.add(manual_txn)
    db.flush()

    bank_txn.matched_to_id = manual_txn.id
    bank_txn.is_reconciled = True
    manual_txn.matched_to_id = bank_txn.id
    manual_txn.is_reconciled = True

    db.commit()
    db.refresh(manual_txn)
    return _txn_dict(manual_txn)
