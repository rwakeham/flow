"""
Backup service – creates, lists, restores, and manages ZIP backups stored on disk.
"""
from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from models import Account, Balance, RegisterAccount, Transaction

BACKUP_DIR = Path("/backups")

_REQUIRED_FILES = {
    "backup_info.json",
    "accounts.json",
    "balances.json",
    "register_accounts.json",
    "transactions.json",
}


def _ensure_dir() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)


# ── Serialisation ─────────────────────────────────────────────────────────────

def _serialize(db: Session) -> dict:
    accounts = db.query(Account).order_by(Account.id).all()
    balances = db.query(Balance).order_by(Balance.id).all()
    register_accounts = db.query(RegisterAccount).order_by(RegisterAccount.id).all()
    transactions = db.query(Transaction).order_by(Transaction.id).all()

    return {
        "accounts": [
            {
                "id": a.id,
                "name": a.name,
                "account_type": a.account_type,
                "override": a.override,
                "ignored": a.ignored,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in accounts
        ],
        "balances": [
            {
                "id": b.id,
                "account_id": b.account_id,
                "period": str(b.period),
                "amount": str(b.amount),
            }
            for b in balances
        ],
        "register_accounts": [
            {
                "id": r.id,
                "name": r.name,
                "opening_balance": str(r.opening_balance),
                "notes": r.notes,
                "cutoff_date": str(r.cutoff_date) if r.cutoff_date else None,
                "cutoff_balance": str(r.cutoff_balance) if r.cutoff_balance else None,
                "is_default": r.is_default,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in register_accounts
        ],
        "transactions": [
            {
                "id": t.id,
                "register_account_id": t.register_account_id,
                "date": str(t.date),
                "description": t.description,
                "amount": str(t.amount),
                "source": t.source,
                "bank_description": t.bank_description,
                "matched_to_id": t.matched_to_id,
                "is_reconciled": t.is_reconciled,
                "notes": t.notes,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            }
            for t in transactions
        ],
    }


def _build_zip_bytes(data: dict, backup_info: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("backup_info.json", json.dumps(backup_info, indent=2))
        zf.writestr("accounts.json", json.dumps(data["accounts"], indent=2))
        zf.writestr("balances.json", json.dumps(data["balances"], indent=2))
        zf.writestr("register_accounts.json", json.dumps(data["register_accounts"], indent=2))
        zf.writestr("transactions.json", json.dumps(data["transactions"], indent=2))
    return buf.getvalue()


# ── Public API ────────────────────────────────────────────────────────────────

def create_backup(db: Session, trigger: str = "manual") -> dict:
    """Serialise current DB state into a ZIP and save to BACKUP_DIR."""
    _ensure_dir()
    data = _serialize(db)
    now = datetime.now(timezone.utc)
    backup_info = {
        "created_at": now.isoformat(),
        "trigger": trigger,
        "tables": {
            "accounts": len(data["accounts"]),
            "balances": len(data["balances"]),
            "register_accounts": len(data["register_accounts"]),
            "transactions": len(data["transactions"]),
        },
    }
    zip_bytes = _build_zip_bytes(data, backup_info)
    short_id = uuid.uuid4().hex[:8]
    filename = f"flow_backup_{now.strftime('%Y%m%d_%H%M%S')}_{short_id}.zip"
    path = BACKUP_DIR / filename
    path.write_bytes(zip_bytes)
    return {
        "id": path.stem,
        "filename": filename,
        **backup_info,
        "size_bytes": len(zip_bytes),
    }


def _read_zip_info(path: Path) -> dict | None:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            return json.loads(zf.read("backup_info.json"))
    except Exception:
        return None


def list_backups() -> list[dict]:
    """Return all backup metadata sorted newest-first."""
    _ensure_dir()
    results = []
    for path in sorted(BACKUP_DIR.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        info = _read_zip_info(path)
        if info is None:
            continue
        results.append({
            "id": path.stem,
            "filename": path.name,
            **info,
            "size_bytes": path.stat().st_size,
        })
    return results


def has_backup_today() -> bool:
    """
    Return True if a backup already exists for today.

    "Today" is determined by the server's UTC date and compared against the
    UTC date encoded in each backup's `created_at` (also UTC). Around UTC
    midnight a user in a non-UTC timezone may briefly see the auto-pre-import
    backup re-trigger; the cost is one extra ZIP, so we accept the simpler logic.
    """
    _ensure_dir()
    today = datetime.now(timezone.utc).date().isoformat()  # "YYYY-MM-DD" in UTC
    for path in BACKUP_DIR.glob("*.zip"):
        info = _read_zip_info(path)
        if info and info.get("created_at", "").startswith(today):
            return True
    return False


def ensure_daily_backup(db: Session) -> None:
    """Create an auto-pre-import backup if none has been taken today."""
    if not has_backup_today():
        create_backup(db, trigger="auto-pre-import")


def get_backup_path(backup_id: str) -> Path:
    """Return path to the backup ZIP; raise ValueError if not found."""
    path = BACKUP_DIR / f"{backup_id}.zip"
    if not path.exists():
        raise ValueError(f"Backup not found: {backup_id}")
    return path


def validate_zip_bytes(content: bytes) -> dict:
    """
    Validate that raw bytes form a well-structured backup ZIP.
    Returns the parsed backup_info dict, or raises ValueError.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content), "r") as zf:
            if not _REQUIRED_FILES.issubset(set(zf.namelist())):
                raise ValueError("ZIP is missing required backup files")
            return json.loads(zf.read("backup_info.json"))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Not a valid ZIP file: {exc}") from exc


def save_uploaded_backup(content: bytes, original_info: dict) -> dict:
    """Persist an uploaded ZIP to BACKUP_DIR with trigger='uploaded'."""
    _ensure_dir()
    created_at = original_info.get("created_at", "")
    try:
        dt = datetime.fromisoformat(created_at)
        ts = dt.strftime("%Y%m%d_%H%M%S")
    except Exception:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    updated_info = {**original_info, "trigger": "uploaded"}

    # Rebuild ZIP with updated backup_info.json
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf_out:
        with zipfile.ZipFile(io.BytesIO(content), "r") as zf_in:
            for name in zf_in.namelist():
                if name == "backup_info.json":
                    zf_out.writestr("backup_info.json", json.dumps(updated_info, indent=2))
                else:
                    zf_out.writestr(name, zf_in.read(name))

    zip_bytes = buf.getvalue()
    short_id = uuid.uuid4().hex[:8]
    filename = f"flow_backup_{ts}_{short_id}.zip"
    path = BACKUP_DIR / filename
    path.write_bytes(zip_bytes)
    return {
        "id": path.stem,
        "filename": filename,
        **updated_info,
        "size_bytes": len(zip_bytes),
    }


def restore_from_path(path: Path, db: Session) -> None:
    """
    Full-replace restore: wipe all tables and reload from the backup ZIP.
    Handles the self-referential matched_to_id FK by inserting transactions
    in two passes.
    """
    try:
        with zipfile.ZipFile(path, "r") as zf:
            accounts_data = json.loads(zf.read("accounts.json"))
            balances_data = json.loads(zf.read("balances.json"))
            register_accounts_data = json.loads(zf.read("register_accounts.json"))
            transactions_data = json.loads(zf.read("transactions.json"))
    except Exception as exc:
        raise ValueError(f"Could not read backup: {exc}") from exc

    # Delete in FK-safe order (transactions cascade from register_accounts)
    db.execute(text("DELETE FROM transactions"))
    db.execute(text("DELETE FROM register_accounts"))
    db.execute(text("DELETE FROM balances"))
    db.execute(text("DELETE FROM accounts"))
    db.flush()

    for a in accounts_data:
        db.execute(text(
            "INSERT INTO accounts (id, name, account_type, override, ignored, created_at) "
            "VALUES (:id, :name, :account_type, :override, :ignored, :created_at)"
        ), a)

    for b in balances_data:
        db.execute(text(
            "INSERT INTO balances (id, account_id, period, amount) "
            "VALUES (:id, :account_id, :period, :amount)"
        ), b)

    for r in register_accounts_data:
        db.execute(text(
            "INSERT INTO register_accounts "
            "(id, name, opening_balance, notes, cutoff_date, cutoff_balance, is_default, created_at) "
            "VALUES (:id, :name, :opening_balance, :notes, :cutoff_date, :cutoff_balance, :is_default, :created_at)"
        ), r)

    # First pass: insert all transactions without matched_to_id to avoid self-ref FK violations
    for t in transactions_data:
        db.execute(text(
            "INSERT INTO transactions "
            "(id, register_account_id, date, description, amount, source, "
            " bank_description, matched_to_id, is_reconciled, notes, created_at) "
            "VALUES (:id, :register_account_id, :date, :description, :amount, :source, "
            "        :bank_description, NULL, :is_reconciled, :notes, :created_at)"
        ), t)

    # Second pass: restore matched_to_id links
    for t in transactions_data:
        if t.get("matched_to_id") is not None:
            db.execute(text(
                "UPDATE transactions SET matched_to_id = :mid WHERE id = :id"
            ), {"mid": t["matched_to_id"], "id": t["id"]})

    # Reset sequences so future inserts don't collide with restored IDs
    for table in ("accounts", "balances", "register_accounts", "transactions"):
        db.execute(text(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"
        ))

    db.commit()
