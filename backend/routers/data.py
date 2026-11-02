from datetime import date

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

import balance_sheet
from auth import require_auth
from database import get_db
import import_profile_resolve
from models import Account, Balance

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


# ── Accounts ─────────────────────────────────────────────────────────────────

def _account_json(a: Account) -> dict:
    return {
        "id": a.id,
        "name": a.name,
        "account_type": a.account_type,
        "override": a.override,
        "ignored": a.ignored,
        "institution": a.institution,
        "last4": balance_sheet.numbers(a.last4),
        "archived": bool(a.archived),
        "alert_pct": a.alert_pct,
        "alert_abs": a.alert_abs,
    }


class AccountCreate(BaseModel):
    name: str
    account_type: str
    institution: str | None = None
    last4: list[str] | None = Field(None, max_length=50)
    alert_pct: float | None = None
    alert_abs: float | None = None


class AccountUpdate(BaseModel):
    name: str | None = None
    account_type: str | None = None
    ignored: bool | None = None
    archived: bool | None = None
    institution: str | None = None
    last4: list[str] | None = Field(None, max_length=50)
    alert_pct: float | None = None
    alert_abs: float | None = None
    clear_alerts: bool = False       # back to the global thresholds


def _check_name(db: Session, name: str, own_id: int | None = None) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name is required")
    if len(name) > 80:
        raise HTTPException(status_code=422, detail="Name is too long (80 characters at most)")
    clash = db.query(Account).filter(Account.name == name, Account.id != (own_id or 0)).first()
    if clash:
        raise HTTPException(status_code=409, detail=f"An account named {name!r} already exists")
    return name


def _check_numbers(db: Session, nums: list[str], own_id: int | None = None) -> str | None:
    clean = balance_sheet.numbers(",".join(nums))
    if len(clean) != len([n for n in nums if n.strip()]):
        raise HTTPException(status_code=422, detail="Account numbers are the last 4 digits")
    for other in db.query(Account).filter(Account.id != (own_id or 0)).all():
        shared = set(clean) & set(balance_sheet.numbers(other.last4))
        if shared:
            raise HTTPException(status_code=409, detail=f"…{sorted(shared)[0]} is already on {other.name}")
    return balance_sheet.join_numbers(clean)


def _check_alert(v: float | None) -> float | None:
    if v is not None and (v < 0 or v > 1e12):
        raise HTTPException(status_code=422, detail="Alert thresholds must be zero or more")
    return v


@router.get("/accounts")
def list_accounts(x_flow_today: str | None = Header(None), db: Session = Depends(get_db)):
    """Every balance-sheet account, with its newest balance (``latest``) and whether this month's is
    still missing (``behind``, by the user's date: X-Flow-Today)."""
    today = balance_sheet.client_today(x_flow_today, date.today())
    newest = {aid: (period, amount, updated) for aid, period, amount, updated in db.execute(text(
        "SELECT DISTINCT ON (account_id) account_id, period, amount, updated_at"
        "  FROM balances ORDER BY account_id, period DESC")).fetchall()}
    out = []
    for a in db.query(Account).order_by(Account.id).all():
        row = _account_json(a)
        n = newest.get(a.id)
        row["latest"] = {"period": n[0].isoformat(), "amount": float(n[1]),
                         "updated_at": n[2].isoformat() if n[2] else None} if n else None
        row["behind"] = balance_sheet.behind(n[0] if n else None, today)
        out.append(row)
    return out


@router.post("/accounts", status_code=201)
def create_account(body: AccountCreate, db: Session = Depends(get_db)):
    if body.account_type not in ("asset", "liability"):
        raise HTTPException(status_code=422, detail="account_type must be 'asset' or 'liability'")
    acct = Account(
        name=_check_name(db, body.name),
        account_type=body.account_type,
        override=True,
        ignored=False,
        archived=False,
        institution=(body.institution or "").strip() or None,
        last4=_check_numbers(db, body.last4 or []),
        alert_pct=_check_alert(body.alert_pct),
        alert_abs=_check_alert(body.alert_abs),
    )
    db.add(acct)
    db.commit()
    return _account_json(acct)


@router.patch("/accounts/{account_id}")
def update_account(account_id: int, body: AccountUpdate, db: Session = Depends(get_db)):
    acct = db.get(Account, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if body.name is not None:
        acct.name = _check_name(db, body.name, acct.id)
    if body.account_type is not None:
        if body.account_type not in ("asset", "liability"):
            raise HTTPException(status_code=422, detail="account_type must be 'asset' or 'liability'")
        acct.account_type = body.account_type
        acct.override = True
    if body.ignored is not None:
        acct.ignored = body.ignored
    if body.archived is not None:
        acct.archived = body.archived
    if body.institution is not None:
        acct.institution = body.institution.strip() or None
    if body.last4 is not None:
        acct.last4 = _check_numbers(db, body.last4, acct.id)
    if body.clear_alerts:
        acct.alert_pct = acct.alert_abs = None
    else:
        if "alert_pct" in body.model_fields_set:
            acct.alert_pct = _check_alert(body.alert_pct)
        if "alert_abs" in body.model_fields_set:
            acct.alert_abs = _check_alert(body.alert_abs)
    db.commit()
    return _account_json(acct)


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)):
    """Delete a balance-sheet account and its whole history (archive it to keep the history)."""
    acct = db.get(Account, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")
    n = db.query(Balance).filter(Balance.account_id == acct.id).delete()
    import_profile_resolve.unlink_account(db, "balance", account_id)
    db.delete(acct)
    db.commit()
    return {"deleted": account_id, "balances_deleted": n}


@router.get("/accounts/{account_id}/balances")
def account_balances(account_id: int, db: Session = Depends(get_db)):
    acct = db.get(Account, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Account not found")
    rows = (db.query(Balance).filter(Balance.account_id == account_id)
            .order_by(Balance.period.desc()).all())
    return {
        "account": _account_json(acct),
        "balances": [{"period": b.period.isoformat(), "amount": float(b.amount), "source": b.source,
                      "updated_at": b.updated_at.isoformat() if b.updated_at else None} for b in rows],
    }


# ── Timeseries ────────────────────────────────────────────────────────────────

def balance_matrix(db: Session) -> tuple[list[date], list[Account], dict[int, tuple[list, list]]]:
    """Every period, every account, and each account's values with gaps carried forward (and which were)."""
    periods = [r[0] for r in db.execute(text("SELECT DISTINCT period FROM balances ORDER BY period")).fetchall()]
    accounts = db.query(Account).order_by(Account.id).all()
    raw: dict[int, dict[date, float]] = {}
    for aid, period, amount in db.execute(text("SELECT account_id, period, amount FROM balances")).fetchall():
        raw.setdefault(aid, {})[period] = float(amount)
    out = {}
    for a in accounts:
        vals = [raw.get(a.id, {}).get(p) for p in periods]
        out[a.id] = balance_sheet.carry_forward(vals, archived=bool(a.archived))
    return periods, accounts, out


@router.get("/data/timeseries")
def get_timeseries(db: Session = Depends(get_db)):
    """
    Monthly values per account. A month an account wasn't updated shows its last
    value (``carried`` marks those), so a half-entered month doesn't dip.
    """
    periods, accounts, matrix = balance_matrix(db)
    if not periods:
        return {"periods": [], "accounts": []}
    return {
        "periods": [str(p) for p in periods],
        "accounts": [{
            "id": a.id,
            "name": a.name,
            "account_type": a.account_type,
            "ignored": a.ignored,
            "archived": bool(a.archived),
            "values": matrix[a.id][0],
            "carried": matrix[a.id][1],
        } for a in accounts],
    }


# ── Summary (metric cards) ────────────────────────────────────────────────────

def summarize(periods: list[date], accounts: list, matrix: dict) -> dict:
    """Totals for the latest period (carried values included) and the change since the one before. Pure."""
    if not periods:
        return {"as_of": None, "net_worth": 0, "total_assets": 0, "total_liabilities": 0,
                "net_worth_mom_delta": None, "largest_asset": None, "carried_accounts": []}
    live = [a for a in accounts if not a.ignored]

    def totals(i: int) -> tuple[float, float]:
        assets = sum(matrix[a.id][0][i] or 0 for a in live if a.account_type == "asset")
        liabs = sum(matrix[a.id][0][i] or 0 for a in live if a.account_type != "asset")
        return assets, liabs

    cur = len(periods) - 1
    assets, liabs = totals(cur)
    mom = None
    if cur > 0:
        pa, pl = totals(cur - 1)
        mom = round((assets + liabs) - (pa + pl), 2)
    largest = None
    cands = [(matrix[a.id][0][cur], a) for a in live if a.account_type == "asset" and matrix[a.id][0][cur] is not None]
    if cands:
        val, a = max(cands, key=lambda t: t[0])
        prev = matrix[a.id][0][cur - 1] if cur > 0 else None
        largest = {"name": a.name, "value": round(val, 2),
                   "mom_delta": round(val - prev, 2) if prev is not None else None}
    return {
        "as_of": str(periods[cur]),
        "net_worth": round(assets + liabs, 2),
        "total_assets": round(assets, 2),
        "total_liabilities": round(liabs, 2),
        "net_worth_mom_delta": mom,
        "largest_asset": largest,
        "carried_accounts": [a.name for a in live if matrix[a.id][1][cur]],
    }


@router.get("/data/summary")
def get_summary(db: Session = Depends(get_db)):
    return summarize(*balance_matrix(db))
