from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from auth import require_auth
from database import get_db
from models import Account

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


# ── Accounts ─────────────────────────────────────────────────────────────────

class AccountUpdate(BaseModel):
    account_type: str


@router.get("/accounts")
def list_accounts(db: Session = Depends(get_db)):
    accounts = db.query(Account).order_by(Account.id).all()
    return [
        {
            "id": a.id,
            "name": a.name,
            "account_type": a.account_type,
            "override": a.override,
        }
        for a in accounts
    ]


@router.patch("/accounts/{account_id}")
def update_account(account_id: int, body: AccountUpdate, db: Session = Depends(get_db)):
    if body.account_type not in ("asset", "liability"):
        raise HTTPException(status_code=422, detail="account_type must be 'asset' or 'liability'")
    acct = db.get(Account, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")
    acct.account_type = body.account_type
    acct.override = True
    db.commit()
    return {"id": acct.id, "name": acct.name, "account_type": acct.account_type, "override": acct.override}


# ── Timeseries ────────────────────────────────────────────────────────────────

@router.get("/data/timeseries")
def get_timeseries(db: Session = Depends(get_db)):
    # Get all distinct periods ordered chronologically
    period_rows = db.execute(
        text("SELECT DISTINCT period FROM balances ORDER BY period")
    ).fetchall()
    periods = [str(r[0]) for r in period_rows]

    if not periods:
        return {"periods": [], "accounts": []}

    # Get all accounts
    accounts = db.query(Account).order_by(Account.id).all()

    # Get all balances in one query
    balance_rows = db.execute(
        text("SELECT account_id, period, amount FROM balances ORDER BY account_id, period")
    ).fetchall()

    # Build lookup: account_id -> {period_str -> amount}
    balance_map: dict[int, dict[str, float]] = {}
    for row in balance_rows:
        aid, period, amount = row
        period_str = str(period)
        if aid not in balance_map:
            balance_map[aid] = {}
        balance_map[aid][period_str] = float(amount)

    result_accounts = []
    for acct in accounts:
        values = [balance_map.get(acct.id, {}).get(p) for p in periods]
        result_accounts.append({
            "id": acct.id,
            "name": acct.name,
            "account_type": acct.account_type,
            "values": values,
        })

    return {"periods": periods, "accounts": result_accounts}


# ── Summary (metric cards) ────────────────────────────────────────────────────

@router.get("/data/summary")
def get_summary(db: Session = Depends(get_db)):
    # Get the two most recent distinct periods
    period_rows = db.execute(
        text("SELECT DISTINCT period FROM balances ORDER BY period DESC LIMIT 2")
    ).fetchall()

    if not period_rows:
        return {
            "as_of": None,
            "net_worth": 0,
            "total_assets": 0,
            "total_liabilities": 0,
            "net_worth_mom_delta": None,
            "largest_asset": None,
        }

    curr_period = str(period_rows[0][0])
    prev_period = str(period_rows[1][0]) if len(period_rows) > 1 else None

    def get_totals(period: str):
        rows = db.execute(
            text("""
                SELECT a.account_type, SUM(b.amount)
                FROM balances b
                JOIN accounts a ON a.id = b.account_id
                WHERE b.period = :period
                GROUP BY a.account_type
            """),
            {"period": period},
        ).fetchall()
        assets = 0.0
        liabilities = 0.0
        for row in rows:
            if row[0] == "asset":
                assets = float(row[1])
            else:
                liabilities = float(row[1])
        return assets, liabilities

    curr_assets, curr_liab = get_totals(curr_period)
    curr_net = curr_assets + curr_liab

    mom_delta = None
    if prev_period:
        prev_assets, prev_liab = get_totals(prev_period)
        prev_net = prev_assets + prev_liab
        mom_delta = round(curr_net - prev_net, 2)

    # Largest asset account at current period
    largest_row = db.execute(
        text("""
            SELECT a.name, b.amount
            FROM balances b
            JOIN accounts a ON a.id = b.account_id
            WHERE b.period = :period AND a.account_type = 'asset'
            ORDER BY b.amount DESC
            LIMIT 1
        """),
        {"period": curr_period},
    ).fetchone()

    largest_asset = None
    if largest_row:
        la_name, la_curr = largest_row[0], float(largest_row[1])
        la_mom = None
        if prev_period:
            prev_la = db.execute(
                text("""
                    SELECT b.amount FROM balances b
                    JOIN accounts a ON a.id = b.account_id
                    WHERE b.period = :period AND a.name = :name
                """),
                {"period": prev_period, "name": la_name},
            ).fetchone()
            if prev_la:
                la_mom = round(la_curr - float(prev_la[0]), 2)
        largest_asset = {"name": la_name, "value": round(la_curr, 2), "mom_delta": la_mom}

    return {
        "as_of": curr_period,
        "net_worth": round(curr_net, 2),
        "total_assets": round(curr_assets, 2),
        "total_liabilities": round(curr_liab, 2),
        "net_worth_mom_delta": mom_delta,
        "largest_asset": largest_asset,
    }
