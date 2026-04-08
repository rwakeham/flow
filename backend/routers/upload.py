import re

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert as pg_insert

from auth import require_auth
from backup_service import ensure_daily_backup
from database import get_db
from models import Account, Balance
from parser import parse_upload
from register_parser import FileFormat, parse_register_import

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

# Matches date values like 4/7/2026, 04-07-2026, 2026-04-07, 2026/04/07
_DATE_VALUE_RE = re.compile(
    r"^\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}$"   # mm/dd/yy(yy) or dd-mm-yy(yy)
    r"|^\d{4}[/\-]\d{1,2}[/\-]\d{1,2}$"     # yyyy-mm-dd or yyyy/mm/dd
)


def _first_col_is_date(content: bytes) -> bool:
    """
    Return True when the first non-empty column of the first non-empty row
    contains a date *value* (e.g. '4/7/2026') rather than a label (e.g. 'date').

    Bank/ledger exports have no header row — data starts immediately.
    Balance sheets always begin with a header row whose first column is a label.
    """
    preview = content[:500].decode("utf-8", errors="replace")
    has_tabs = "\t" in preview
    for line in preview.splitlines():
        line = line.strip()
        if not line:
            continue
        first_col = (line.split("\t")[0] if has_tabs else line.split(",")[0]).strip()
        return bool(_DATE_VALUE_RE.match(first_col))
    return False


@router.post("/import/detect")
async def detect_import(file: UploadFile = File(...)):
    """
    Identify whether a file is a balance sheet or a bank/ledger register import.
    Read-only — never writes data.
    """
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")

    filename = file.filename or "upload"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    # XLSX is always a balance sheet
    if ext == "xlsx":
        try:
            result = parse_upload(content, filename)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if not result.accounts or not result.rows:
            raise HTTPException(status_code=422, detail="No data found in XLSX file")
        return {"type": "balance", "accounts": len(result.accounts), "periods": len(result.rows)}

    # Key heuristic: bank/ledger exports have no header row — the first row IS
    # data, so its first column is a date value.  Balance sheets always have a
    # named header row (e.g. "date", "Period"), so the first column is a label.
    if _first_col_is_date(content):
        # No header row → bank or ledger register export
        try:
            reg = parse_register_import(content)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Could not parse file: {exc}")
        if reg.format == FileFormat.UNKNOWN:
            raise HTTPException(status_code=422, detail="No transactions found in file")
        rows = len(reg.bank_rows) if reg.format == FileFormat.BANK else len(reg.ledger_rows)
        return {"type": "register", "format": reg.format.value, "rows": rows}

    # Has a header row → try balance sheet.  Require at least one row with an
    # actual numeric value (guards against ledger files that happen to have headers).
    try:
        bal = parse_upload(content, filename)
        if bal.accounts and bal.rows and any(r["values"] for r in bal.rows):
            return {"type": "balance", "accounts": len(bal.accounts), "periods": len(bal.rows)}
    except ValueError:
        pass

    # Fall back to register (e.g. ledger with a header row)
    try:
        reg = parse_register_import(content)
        if reg.format != FileFormat.UNKNOWN:
            rows = len(reg.bank_rows) if reg.format == FileFormat.BANK else len(reg.ledger_rows)
            return {"type": "register", "format": reg.format.value, "rows": rows}
    except Exception:
        pass

    raise HTTPException(
        status_code=422,
        detail="Could not detect file format. Expected a balance sheet "
               "(CSV/XLSX with date + account columns) or a bank/ledger export.",
    )


@router.post("/upload")
async def upload_file(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")

    try:
        ensure_daily_backup(db)
    except Exception:
        pass  # backup failure must never block an import

    filename = file.filename or "upload.csv"
    try:
        result = parse_upload(content, filename)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    accounts_created = 0
    rows_written = 0
    account_id_map: dict[str, int] = {}

    for parsed_acct in result.accounts:
        existing = db.query(Account).filter(Account.name == parsed_acct.name).first()
        if existing is None:
            acct = Account(
                name=parsed_acct.name,
                account_type=parsed_acct.detected_type,
                override=False,
            )
            db.add(acct)
            db.flush()
            account_id_map[parsed_acct.name] = acct.id
            accounts_created += 1
        else:
            if not existing.override:
                existing.account_type = parsed_acct.detected_type
            account_id_map[parsed_acct.name] = existing.id

    db.flush()

    periods_seen: set = set()
    for row in result.rows:
        period = row["period"]
        periods_seen.add(period)
        for acct_name, amount in row["values"].items():
            acct_id = account_id_map.get(acct_name)
            if acct_id is None:
                continue
            stmt = (
                pg_insert(Balance)
                .values(account_id=acct_id, period=period, amount=amount)
                .on_conflict_do_update(
                    index_elements=["account_id", "period"],
                    set_={"amount": amount},
                )
            )
            db.execute(stmt)
            rows_written += 1

    db.commit()

    return {
        "periods_upserted": len(periods_seen),
        "accounts_seen": len(result.accounts),
        "accounts_created": accounts_created,
        "rows_written": rows_written,
    }
