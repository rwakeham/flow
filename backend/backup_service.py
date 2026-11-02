"""
Backup service – creates, lists, restores, and manages ZIP backups stored on disk.

The daily backup (``loop``, started by main.py's lifespan) takes one ``scheduled`` backup a day at or
after ``settings_store.backup_hour()`` UTC and then keeps only the newest ``settings_store.backup_keep()``
automatic backups (``PRUNABLE``: scheduled and auto-pre-import); backups taken by hand, uploaded or
taken before a restore are never removed by Flow. ``encrypt_backup`` / ``decrypt_backup`` wrap a zip for
a copy kept off the server: a small container (``MAGIC``, scrypt salt and parameters, nonce, AES-GCM
ciphertext with the header as associated data) under a passphrase Flow never stores. The backups in
``BACKUP_DIR`` stay plain zips.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
import secrets
import threading
from datetime import date
from decimal import Decimal
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

import change_log
import settings_store
from models import REVOKED_SESSIONS_KEY, SESSION_EPOCH_KEY, Account, Balance, Base, RegisterAccount, Transaction

log = logging.getLogger("flow.backup")

# flow_settings rows that belong to the running install's sessions, never to a backup.
SESSION_KEYS = (SESSION_EPOCH_KEY, REVOKED_SESSIONS_KEY)
# flow_settings rows a restore keeps from the running install: the sessions above, and the install's
# own schedule (the daily backup and update check), which an older backup must not reset.
KEEP_ON_RESTORE = SESSION_KEYS + (settings_store.BACKUP_DAILY, settings_store.BACKUP_KEEP,
                                  settings_store.BACKUP_HOUR, settings_store.UPDATE_CHECK)

BACKUP_DIR = Path("/backups")

# One backup is written or removed at a time (the daily loop, an import's pre-import backup and
# the user's "+ New Backup" can coincide).
_lock = threading.Lock()

# A backup's id is its file name without .zip, always in the form create_backup and
# save_uploaded_backup write. Anything else (a path, "..") is refused before the disk is touched.
BACKUP_ID_RE = re.compile(r"^flow_backup_[0-9]{8}_[0-9]{6}_[0-9a-f]{8}$")

# Zip limits, checked from the directory before anything is decompressed (a small
# upload can claim gigabytes). A real backup has about 25 members.
MAX_ZIP_MEMBERS = 50
MAX_MEMBER_BYTES = 100 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024

_REQUIRED_FILES = {
    "backup_info.json",
    "accounts.json",
    "balances.json",
    "register_accounts.json",
    "transactions.json",
}


# Credit-card tables, in FK-safe insert order. Optional in a backup: one taken
# before cards existed restores without touching the card tables.
CARD_TABLES = [
    "import_profiles", "import_profile_links",      # card_imports.profile_id points at a format
    "card_accounts", "card_imports", "merchant_categories", "card_recurring_verdicts",
    "ai_request_log", "card_transactions", "card_insight_reports", "card_manual_recurring",
    "card_amount_rules", "custom_categories", "flow_settings", "category_renames", "payee_roles",
    "balance_markers",
]
# Card tables added after backups already existed: a backup without one still
# restores its cards, and the table starts empty.
_LATER_CARD_TABLES = {"import_profiles", "import_profile_links", "balance_markers", "card_manual_recurring", "card_amount_rules", "custom_categories", "flow_settings", "category_renames", "payee_roles"}
# Import formats are kept when a backup from before they existed is restored:
# wiping them would leave no way to import until each was set up again.
_KEEP_IF_ABSENT = {"import_profiles", "import_profile_links"}
_CARD_SERIAL_TABLES = ["import_profiles", "import_profile_links", "balance_markers", "card_accounts", "card_imports", "card_transactions", "ai_request_log",
                       "card_insight_reports", "card_manual_recurring", "card_amount_rules", "custom_categories",
                       "category_renames"]


def _jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return v


def _serialize_card_tables(db: Session) -> dict:
    out = {}
    for table in CARD_TABLES:
        rows = db.execute(text(f"SELECT * FROM {table}")).mappings().all()
        out[table] = [{k: _jsonable(v) for k, v in r.items()} for r in rows]
    return out


def _ensure_dir() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)


# ── Serialisation ─────────────────────────────────────────────────────────────

def _serialize(db: Session) -> dict:
    accounts = db.query(Account).order_by(Account.id).all()
    balances = db.query(Balance).order_by(Balance.id).all()
    register_accounts = db.query(RegisterAccount).order_by(RegisterAccount.id).all()
    transactions = db.query(Transaction).order_by(Transaction.id).all()

    return {
        **_serialize_card_tables(db),
        "accounts": [
            {
                "id": a.id,
                "name": a.name,
                "account_type": a.account_type,
                "override": a.override,
                "ignored": a.ignored,
                "institution": a.institution,
                "last4": a.last4,
                "archived": bool(a.archived),
                "alert_pct": a.alert_pct,
                "alert_abs": a.alert_abs,
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
                "source": b.source,
            }
            for b in balances
        ],
        "register_accounts": [
            {
                "id": r.id,
                "name": r.name,
                "opening_balance": str(r.opening_balance),
                "notes": r.notes,
                "institution": r.institution,
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
        for table in CARD_TABLES:
            zf.writestr(f"{table}.json", json.dumps(data.get(table, []), indent=2))
    return buf.getvalue()


# ── Public API ────────────────────────────────────────────────────────────────

def create_backup(db: Session, trigger: str = "manual") -> dict:
    """Serialise current DB state into a ZIP and save to BACKUP_DIR."""
    with _lock:
        return _create_backup(db, trigger)


def _create_backup(db: Session, trigger: str) -> dict:
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
            **{t: len(data.get(t, [])) for t in CARD_TABLES},
        },
    }
    zip_bytes = _build_zip_bytes(data, backup_info)
    short_id = uuid.uuid4().hex[:8]
    filename = f"flow_backup_{now.strftime('%Y%m%d_%H%M%S')}_{short_id}.zip"
    path = BACKUP_DIR / filename
    path.write_bytes(zip_bytes)
    return {
        **backup_info,
        "id": path.stem,
        "filename": filename,
        "size_bytes": len(zip_bytes),
    }


TRIGGERS = ("manual", "auto-pre-import", "pre-restore", "uploaded", "scheduled")
PRUNABLE = ("scheduled", "auto-pre-import")       # the automatic ones: retention applies to these only
TABLE_COUNTS = ("accounts", "balances", "register_accounts", "transactions", *CARD_TABLES)


def clean_info(raw) -> dict:
    """A backup's backup_info.json as Flow uses it: known fields only, each of the right type.
    An uploaded backup is someone else's file, and its info is shown in the page and read by
    has_backup_today, so nothing else in it may reach either (markup in a count, an "id" that
    names another backup, a created_at that isn't text)."""
    raw = raw if isinstance(raw, dict) else {}
    created = raw.get("created_at")
    try:
        created = created if isinstance(created, str) and datetime.fromisoformat(created) else ""
    except ValueError:
        created = ""
    tables = raw.get("tables") if isinstance(raw.get("tables"), dict) else {}
    return {
        "created_at": created,
        "trigger": raw.get("trigger") if raw.get("trigger") in TRIGGERS else "uploaded",
        "tables": {k: v for k, v in tables.items()
                   if k in TABLE_COUNTS and type(v) is int and 0 <= v < 10**9},
    }


def _read_zip_info(path: Path) -> dict | None:
    try:
        with zipfile.ZipFile(path, "r") as zf:
            check_zip_limits(zf)
            return clean_info(json.loads(zf.read("backup_info.json")))
    except Exception:
        return None


def list_backups() -> list[dict]:
    """Return all backup metadata sorted newest-first."""
    _ensure_dir()
    results = []
    for path in sorted(BACKUP_DIR.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
        if not BACKUP_ID_RE.fullmatch(path.stem):
            continue                            # not one of Flow's (its id would be refused)
        info = _read_zip_info(path)
        if info is None:
            continue
        results.append({
            **info,
            "id": path.stem,
            "filename": path.name,
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


# ── The daily backup and retention ───────────────────────────────────────────

FIRST_DELAY = 180          # seconds after startup: the deploy's health check comes first
INTERVAL = 15 * 60         # between checks: a restart still gets its backup the same day
BACKUP_OLD_DAYS = 7        # the Dashboard says so when the newest backup is older than this


def last_scheduled(entries: list[dict]) -> str | None:
    """When the newest scheduled backup was taken (its backup_info created_at, never the file's
    mtime: an uploaded backup keeps its original date). Pure."""
    times = [e.get("created_at") or "" for e in entries if e.get("trigger") == "scheduled"]
    return max(times) if times else None


def due(now_utc: datetime, last_scheduled_at: str | None, hour: int) -> bool:
    """Whether today's scheduled backup is still owed: it is ``hour`` o'clock UTC or later and no
    scheduled backup was taken today (UTC). A restart after the hour catches up at its first tick. Pure."""
    if now_utc.hour < hour:
        return False
    if not last_scheduled_at:
        return True
    try:
        last = datetime.fromisoformat(last_scheduled_at)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last.astimezone(timezone.utc).date() < now_utc.date()


def prune(entries: list[dict], keep: int) -> list[str]:
    """The ids of automatic backups (PRUNABLE triggers) beyond the newest ``keep`` by created_at;
    ``keep`` 0 keeps every one. Backups taken by hand, uploaded or taken before a restore are never
    listed. Pure."""
    if keep <= 0:
        return []
    automatic = sorted((e for e in entries if e.get("trigger") in PRUNABLE),
                       key=lambda e: e.get("created_at") or "", reverse=True)
    return [e["id"] for e in automatic[keep:]]


def prune_backups(keep: int) -> int:
    """Remove the automatic backups retention no longer keeps; returns how many."""
    with _lock:
        ids = prune(list_backups(), keep)
        for backup_id in ids:
            (BACKUP_DIR / f"{backup_id}.zip").unlink(missing_ok=True)
    return len(ids)


def run_scheduled(session_factory) -> dict:
    """One tick of the daily task: the day's backup if it is due and the switch is on, then retention."""
    out = {"created": None, "pruned": 0}
    if settings_store.backup_daily_flag() and due(datetime.now(timezone.utc), last_scheduled(list_backups()),
                                                  settings_store.backup_hour()):
        with session_factory() as db:
            out["created"] = create_backup(db, trigger="scheduled")
            change_log.record(db, "backup", "scheduled", f"Daily backup {out['created']['id']} taken", {"id": out["created"]["id"]}, via="schedule")
            db.commit()
        log.info("daily backup taken: %s", out["created"]["id"])
    out["pruned"] = prune_backups(settings_store.backup_keep())
    with session_factory() as db:
        if out["pruned"]:
            change_log.record(db, "backup", "pruned", f"Removed {out['pruned']} old automatic backup{'s' if out['pruned'] != 1 else ''}", {"count": out["pruned"]}, via="schedule")
            db.commit()
        change_log.prune(db)                 # the change log's own cap
    if out["pruned"]:
        log.info("retention removed %d automatic backup(s)", out["pruned"])
    return out


async def loop(session_factory, first_delay: float = FIRST_DELAY, interval: float = INTERVAL) -> None:
    """Background task started by main.py's lifespan: run_scheduled a few minutes after startup, then
    every ``interval`` seconds. It never raises, so it outlives any failure."""
    await asyncio.sleep(first_delay)
    while True:
        try:
            await asyncio.to_thread(run_scheduled, session_factory)
        except Exception:    # noqa: BLE001 - the task must keep going
            log.exception("daily backup")
        await asyncio.sleep(interval)


# ── Encrypted copies (download / upload only; the server's files stay plain) ─

MAGIC = b"FLOWBK1"
ENCRYPTED_SUFFIX = ".flowbackup"
SCRYPT_N_LOG2, SCRYPT_R, SCRYPT_P = 17, 8, 1      # ~128 MB and well under a second per file key
_SALT_LEN, _NONCE_LEN, _TAG_LEN = 16, 12, 16
HEADER_LEN = len(MAGIC) + _SALT_LEN + 3 + _NONCE_LEN
PASSPHRASE_MIN, PASSPHRASE_MAX = 8, 200


class WrongPassphrase(ValueError):
    """The passphrase doesn't open the file (or the file is damaged): AES-GCM's tag didn't verify."""


def is_encrypted(blob: bytes) -> bool:
    return blob[:len(MAGIC)] == MAGIC


def _file_key(passphrase: str, salt: bytes, n_log2: int, r: int, p: int) -> bytes:
    n = 2 ** n_log2
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32,
                          maxmem=2 * 128 * r * n)


def encrypt_backup(zip_bytes: bytes, passphrase: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = secrets.token_bytes(_SALT_LEN), secrets.token_bytes(_NONCE_LEN)
    header = MAGIC + salt + bytes([SCRYPT_N_LOG2, SCRYPT_R, SCRYPT_P]) + nonce
    key = _file_key(passphrase, salt, SCRYPT_N_LOG2, SCRYPT_R, SCRYPT_P)
    return header + AESGCM(key).encrypt(nonce, zip_bytes, header)


def decrypt_backup(blob: bytes, passphrase: str) -> bytes:
    """The zip inside an encrypted copy. ValueError when it isn't one (or asks for scrypt parameters
    Flow won't run: the header of an uploaded file is someone else's data); WrongPassphrase when the
    passphrase is wrong or the file was changed."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not is_encrypted(blob) or len(blob) < HEADER_LEN + _TAG_LEN:
        raise ValueError("Not an encrypted Flow backup")
    header = blob[:HEADER_LEN]
    pos = len(MAGIC)
    salt = header[pos:pos + _SALT_LEN]
    pos += _SALT_LEN
    n_log2, r, p = header[pos], header[pos + 1], header[pos + 2]
    nonce = header[pos + 3:pos + 3 + _NONCE_LEN]
    if not (10 <= n_log2 <= 18 and 1 <= r <= 16 and 1 <= p <= 4):
        raise ValueError("Not an encrypted Flow backup (unsupported parameters)")
    key = _file_key(passphrase, salt, n_log2, r, p)
    try:
        return AESGCM(key).decrypt(nonce, blob[HEADER_LEN:], header)
    except InvalidTag:
        raise WrongPassphrase("Wrong passphrase, or the file is damaged") from None


def get_backup_path(backup_id: str) -> Path:
    """Return path to the backup ZIP; raise ValueError for a malformed id or a missing file.
    The id is checked against BACKUP_ID_RE and then only COMPARED with the names in BACKUP_DIR: the
    path returned is the directory's own entry, never one built from the request's text."""
    if not isinstance(backup_id, str) or not BACKUP_ID_RE.fullmatch(backup_id):
        raise ValueError("Backup not found")
    _ensure_dir()
    for entry in BACKUP_DIR.iterdir():
        if entry.name == f"{backup_id}.zip" and entry.is_file():
            return entry.resolve()
    raise ValueError("Backup not found")


def check_zip_limits(zf: zipfile.ZipFile) -> None:
    """Refuse a zip with too many members or too much data once decompressed."""
    infos = zf.infolist()
    if len(infos) > MAX_ZIP_MEMBERS:
        raise ValueError(f"ZIP has {len(infos)} files; a backup has at most {MAX_ZIP_MEMBERS}")
    total = 0
    for info in infos:
        if info.file_size > MAX_MEMBER_BYTES:
            raise ValueError(f"ZIP member {info.filename!r} is too large")
        total += info.file_size
    if total > MAX_TOTAL_BYTES:
        raise ValueError("ZIP is too large once decompressed")


def _backup_members() -> set[str]:
    return _REQUIRED_FILES | {f"{t}.json" for t in CARD_TABLES}


def validate_zip_bytes(content: bytes) -> dict:
    """
    Validate that raw bytes form a well-structured backup ZIP.
    Returns the parsed backup_info dict, or raises ValueError.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(content), "r") as zf:
            check_zip_limits(zf)
            if not _REQUIRED_FILES.issubset(set(zf.namelist())):
                raise ValueError("ZIP is missing required backup files")
            try:
                return clean_info(json.loads(zf.read("backup_info.json")))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("backup_info.json isn't valid JSON") from exc
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

    updated_info = {**clean_info(original_info), "trigger": "uploaded"}

    # Rebuild ZIP with updated backup_info.json
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf_out:
        with zipfile.ZipFile(io.BytesIO(content), "r") as zf_in:
            check_zip_limits(zf_in)
            known = _backup_members()
            for name in zf_in.namelist():
                if name not in known:
                    continue                    # only the backup's own tables are kept
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
        **updated_info,
        "id": path.stem,
        "filename": filename,
        "size_bytes": len(zip_bytes),
    }


def _check_card_columns(card_data: dict) -> None:
    """
    Every card-table row in a backup must be an object whose keys are that table's own columns.
    The column names end up in INSERT statements, so a crafted backup could otherwise inject SQL.
    """
    for table, rows in card_data.items():
        if not isinstance(rows, list):
            raise ValueError(f"Backup table {table} is not a list of rows")
        allowed = set(Base.metadata.tables[table].columns.keys())
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"Backup table {table} has a row that is not an object")
            unknown = [c for c in row if c not in allowed]
            if unknown:
                raise ValueError(f"Backup table {table} has an unknown column {unknown[0][:60]!r}")


def restore_from_path(path: Path, db: Session) -> None:
    """
    Full-replace restore: wipe all tables and reload from the backup ZIP.
    Handles the self-referential matched_to_id FK by inserting transactions
    in two passes. Card tables are replaced only when the backup includes
    them; a backup taken before cards existed leaves card data as it is.
    """
    try:
        with zipfile.ZipFile(path, "r") as zf:
            check_zip_limits(zf)
            accounts_data = json.loads(zf.read("accounts.json"))
            balances_data = json.loads(zf.read("balances.json"))
            register_accounts_data = json.loads(zf.read("register_accounts.json"))
            transactions_data = json.loads(zf.read("transactions.json"))
            names = set(zf.namelist())
            card_data = {t: json.loads(zf.read(f"{t}.json")) for t in CARD_TABLES if f"{t}.json" in names}
    except Exception as exc:
        raise ValueError(f"Could not read backup: {exc}") from exc
    _check_card_columns(card_data)          # before anything is deleted

    # Card tables are replaced only when the backup carries them.
    restore_cards = all(t in card_data for t in CARD_TABLES if t not in _LATER_CARD_TABLES)
    sessions: dict[str, str] = {}
    if restore_cards:
        # The session generation and the revoked session ids are the running install's, not the
        # backup's: restoring an old backup must not bring back cookies a sign-out ended. The same
        # goes for the install's own schedule (daily backup, update check).
        for key in KEEP_ON_RESTORE:
            value = db.execute(text("SELECT value FROM flow_settings WHERE key = :k"), {"k": key}).scalar()
            if value is not None:
                sessions[key] = value
        for table in reversed(CARD_TABLES):
            if table in _KEEP_IF_ABSENT and table not in card_data:
                continue
            db.execute(text(f"DELETE FROM {table}"))

    # Delete in FK-safe order (transactions cascade from register_accounts)
    db.execute(text("DELETE FROM transactions"))
    db.execute(text("DELETE FROM register_accounts"))
    db.execute(text("DELETE FROM balances"))
    db.execute(text("DELETE FROM accounts"))
    db.flush()

    for a in accounts_data:
        a = {"institution": None, "last4": None, "archived": False, "alert_pct": None, "alert_abs": None, **a}
        db.execute(text(
            "INSERT INTO accounts (id, name, account_type, override, ignored, institution, last4, archived, "
            "alert_pct, alert_abs, created_at) "
            "VALUES (:id, :name, :account_type, :override, :ignored, :institution, :last4, :archived, "
            ":alert_pct, :alert_abs, :created_at)"
        ), a)

    for b in balances_data:
        b = {"source": "upload", **b}
        db.execute(text(
            "INSERT INTO balances (id, account_id, period, amount, source) "
            "VALUES (:id, :account_id, :period, :amount, :source)"
        ), b)

    for r in register_accounts_data:
        r = {"institution": None, **r}
        db.execute(text(
            "INSERT INTO register_accounts "
            "(id, name, opening_balance, notes, institution, cutoff_date, cutoff_balance, is_default, created_at) "
            "VALUES (:id, :name, :opening_balance, :notes, :institution, :cutoff_date, :cutoff_balance, "
            ":is_default, :created_at)"
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

    if restore_cards:
        for table in CARD_TABLES:
            for row in card_data.get(table, []):
                # Column names were checked against the table (_check_card_columns); quoted here
                # too, and the values go in as numbered parameters.
                cols = list(row)
                names = ", ".join(f'"{c}"' for c in cols)
                params = ", ".join(f":p{i}" for i in range(len(cols)))
                db.execute(text(f'INSERT INTO "{table}" ({names}) VALUES ({params})'),
                           {f"p{i}": row[c] for i, c in enumerate(cols)})
        for key in KEEP_ON_RESTORE:
            db.execute(text("DELETE FROM flow_settings WHERE key = :k"), {"k": key})
            if key in sessions:
                db.execute(text("INSERT INTO flow_settings (key, value) VALUES (:k, :v)"),
                           {"k": key, "v": sessions[key]})

    # Reset sequences so future inserts don't collide with restored IDs
    serial_tables = ["accounts", "balances", "register_accounts", "transactions"]
    if restore_cards:
        serial_tables += _CARD_SERIAL_TABLES
    for table in serial_tables:
        db.execute(text(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {table}), 0) + 1, false)"
        ))

    db.commit()
