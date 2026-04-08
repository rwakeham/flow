import csv
import io
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

_DATE_VALUE_RE = re.compile(
    r"^\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}$"  # mm/dd/yy(yy)
    r"|^\d{4}[/\-]\d{1,2}[/\-]\d{1,2}$"   # yyyy-mm-dd
)


def _parse_preview_rows(content: bytes, max_rows: int = 20) -> list[list[str]]:
    """Return up to max_rows parsed rows from CSV or TSV content."""
    text = content[:8000].decode("utf-8", errors="replace").lstrip("\ufeff")
    has_tabs = "\t" in text
    rows: list[list[str]] = []
    if has_tabs:
        for line in text.splitlines():
            if not line.strip():
                continue
            rows.append([f.strip() for f in line.split("\t")])
            if len(rows) >= max_rows:
                break
    else:
        reader = csv.reader(io.StringIO(text))
        for fields in reader:
            stripped = [f.strip() for f in fields]
            if any(stripped):
                rows.append(stripped)
                if len(rows) >= max_rows:
                    break
    return rows


def _is_numeric(value: str) -> bool:
    """True for values like -170, 4,821.46, (3,099.54), $45.67."""
    v = value.replace(",", "").replace("$", "").replace("(", "-").replace(")", "")
    try:
        float(v)
        return True
    except ValueError:
        return False


def _has_text_data_column(rows: list[list[str]]) -> bool:
    """
    Return True if any column (other than a leading date column) contains
    predominantly non-numeric values.

    Balance sheets have only numeric data columns (one balance per account).
    Bank/ledger files always have at least one text column (description, flags
    like '*', reconciliation marks, etc.).  This distinction is reliable
    regardless of delimiter, quoting, or whether a header row is present.
    """
    if not rows:
        return False

    # Decide whether row 0 is a header: its first cell is not a date value.
    first_cell = rows[0][0] if rows[0] else ""
    data_rows = rows[1:] if not _DATE_VALUE_RE.match(first_cell) else rows

    if not data_rows:
        return False

    col_count = max(len(r) for r in data_rows)

    # Skip column 0 (date). Examine every other column.
    for col_idx in range(1, col_count):
        values = [r[col_idx] for r in data_rows if col_idx < len(r) and r[col_idx]]
        if not values:
            continue
        text_count = sum(1 for v in values if not _is_numeric(v))
        if text_count / len(values) > 0.3:
            return True  # more than 30 % of non-empty values are non-numeric

    return False


@router.post("/import/detect")
async def detect_import(file: UploadFile = File(...)):
    """
    Identify whether a file is a balance sheet or a bank/ledger register import.
    Read-only — never writes data.

    Detection is structural:
    - Balance sheet: wide format, all data columns are numeric (account balances).
    - Bank/ledger:   narrow format with at least one text column (description,
                     flags like '*', reconciliation marks, etc.).
    XLSX files are always treated as balance sheets.
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

    # Structural detection: does any data column contain text?
    preview_rows = _parse_preview_rows(content)
    if _has_text_data_column(preview_rows):
        # Text column present → bank or ledger register export
        try:
            reg = parse_register_import(content)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Could not parse file: {exc}")
        if reg.format == FileFormat.UNKNOWN:
            raise HTTPException(status_code=422, detail="No transactions found in file")
        rows = len(reg.bank_rows) if reg.format == FileFormat.BANK else len(reg.ledger_rows)
        return {"type": "register", "format": reg.format.value, "rows": rows}

    # All data columns are numeric → balance sheet.
    # Require at least one actual numeric value (guards against empty files).
    try:
        bal = parse_upload(content, filename)
        if bal.accounts and bal.rows and any(r["values"] for r in bal.rows):
            return {"type": "balance", "accounts": len(bal.accounts), "periods": len(bal.rows)}
    except ValueError:
        pass

    # Fallback: try register parser (e.g. a headerless ledger with only numbers)
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
