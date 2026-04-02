from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert as pg_insert

from auth import require_auth
from database import get_db
from models import Account, Balance
from parser import parse_upload

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


@router.post("/upload")
async def upload_file(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")

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
