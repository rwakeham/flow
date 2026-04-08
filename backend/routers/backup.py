import io
import json
import zipfile
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from auth import require_auth
from database import get_db
from models import Account, Balance, RegisterAccount, Transaction

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


@router.get("/backup/download")
def download_backup(db: Session = Depends(get_db)):
    accounts = db.query(Account).order_by(Account.id).all()
    balances = db.query(Balance).order_by(Balance.id).all()
    register_accounts = db.query(RegisterAccount).order_by(RegisterAccount.id).all()
    transactions = db.query(Transaction).order_by(Transaction.id).all()

    accounts_data = [
        {
            "id": a.id,
            "name": a.name,
            "account_type": a.account_type,
            "override": a.override,
            "ignored": a.ignored,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in accounts
    ]

    balances_data = [
        {
            "id": b.id,
            "account_id": b.account_id,
            "period": str(b.period),
            "amount": str(b.amount),
        }
        for b in balances
    ]

    register_accounts_data = [
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
    ]

    transactions_data = [
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
    ]

    now = datetime.now(timezone.utc)
    backup_info = {
        "created_at": now.isoformat(),
        "tables": {
            "accounts": len(accounts_data),
            "balances": len(balances_data),
            "register_accounts": len(register_accounts_data),
            "transactions": len(transactions_data),
        },
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("backup_info.json", json.dumps(backup_info, indent=2))
        zf.writestr("accounts.json", json.dumps(accounts_data, indent=2))
        zf.writestr("balances.json", json.dumps(balances_data, indent=2))
        zf.writestr("register_accounts.json", json.dumps(register_accounts_data, indent=2))
        zf.writestr("transactions.json", json.dumps(transactions_data, indent=2))

    buf.seek(0)
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    filename = f"flow_backup_{timestamp}.zip"

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
