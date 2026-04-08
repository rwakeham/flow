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


@router.post("/import/detect")
async def detect_import(file: UploadFile = File(...)):
    """
    Auto-detect whether a file is a balance sheet (CSV/XLSX wide format) or a
    register import (bank/ledger tab-delimited). Read-only — does not write any data.
    """
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")

    filename = file.filename or "upload"
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    # XLSX is only ever used for balance sheets
    if ext == "xlsx":
        try:
            result = parse_upload(content, filename)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if not result.accounts or not result.rows:
            raise HTTPException(status_code=422, detail="No data found in XLSX file")
        return {"type": "balance", "accounts": len(result.accounts), "periods": len(result.rows)}

    # Tab-delimited files are exclusively bank/ledger exports
    preview = content[:2000].decode("utf-8", errors="replace")
    if "\t" in preview:
        try:
            reg = parse_register_import(content)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Could not parse file: {exc}")
        if reg.format == FileFormat.UNKNOWN:
            raise HTTPException(status_code=422, detail="No transactions found in file")
        rows = len(reg.bank_rows) if reg.format == FileFormat.BANK else len(reg.ledger_rows)
        return {"type": "register", "format": reg.format.value, "rows": rows}

    # Plain CSV — try balance sheet first (wide format), then register
    try:
        bal = parse_upload(content, filename)
        if bal.accounts and bal.rows:
            return {"type": "balance", "accounts": len(bal.accounts), "periods": len(bal.rows)}
    except ValueError:
        pass

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

    ensure_daily_backup(db)

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
