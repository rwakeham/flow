"""
/api/cashflow: the Dashboard's cash flow Sankey and the payee roles behind it (cash_flow.py).
"""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import card_taxonomy
import cash_flow
from auth import require_auth
from database import get_db
from models import Account, CardAccount, CardTransaction, PayeeRole, RegisterAccount, Transaction

router = APIRouter(prefix="/api/cashflow", dependencies=[Depends(require_auth)])

MATCH_MARGIN = timedelta(days=8)        # rows just outside the period, for matching card payments and transfers


def _cash_rows(db: Session, lo: date | None, hi: date | None) -> list[cash_flow.CashRow]:
    """The register's ledger (manual rows, plus bank rows not matched to one), after each account's cutoff."""
    out = []
    for acct in db.query(RegisterAccount).all():
        q = db.query(Transaction).filter(Transaction.register_account_id == acct.id)
        cutoff = acct.cutoff_date if acct.cutoff_balance is not None else None
        if cutoff:
            q = q.filter(Transaction.date >= cutoff)
        if lo:
            q = q.filter(Transaction.date >= lo)
        if hi:
            q = q.filter(Transaction.date <= hi)
        for t in q.all():
            if t.source == "manual" or t.matched_to_id is None:
                out.append(cash_flow.CashRow(t.id, acct.id, t.date, float(t.amount), t.description or t.bank_description or ""))
    return out


def _roles(db: Session) -> dict[str, cash_flow.Role]:
    return {r.key: cash_flow.Role(r.role, r.category or "", r.target or "") for r in db.query(PayeeRole).all()}


def _period(start: date | None, end: date | None) -> tuple[date, date]:
    today = date.today()
    end = end or today
    start = start or (end.replace(day=1) - timedelta(days=1)).replace(day=1)
    if start > end:
        raise HTTPException(status_code=422, detail="The start date is after the end date")
    return start, end


def _inputs(db: Session, start: date, end: date):
    """Register rows, card spending and card payments around [start, end] (a margin for matching)."""
    lo, hi = start - MATCH_MARGIN, end + MATCH_MARGIN
    rows = _cash_rows(db, lo, hi)
    card_rows = db.query(CardTransaction).filter(CardTransaction.date >= lo, CardTransaction.date <= hi).all()
    spend = [cash_flow.CardSpend(t.date, -float(t.amount), t.cat_l1, t.id, t.cat_l2, t.canonical_merchant,
                                 t.card_account_id)
             for t in card_rows if t.is_spend and t.cat_l1 != card_taxonomy.EXCLUDED_L1]
    payments = [cash_flow.CardPayment(t.date, float(t.amount)) for t in card_rows
                if t.txn_type == "payment" and float(t.amount) > 0]
    return rows, spend, payments, card_rows


@router.get("")
def get_cash_flow(start: date | None = None, end: date | None = None, per_month: bool = True,
                  db: Session = Depends(get_db)):
    """The Sankey for a period: nodes, links (per month on average unless per_month is false), totals."""
    start, end = _period(start, end)
    rows, spend, payments, _ = _inputs(db, start, end)
    months = cash_flow.months_between(start, end) if per_month else 1.0
    f = cash_flow.build(rows, spend, payments, _roles(db), start, end, months)

    warnings = []
    for c in db.query(CardAccount).filter(CardAccount.archived.is_(False)).all():
        last = db.query(CardTransaction.date).filter(CardTransaction.card_account_id == c.id) \
            .order_by(CardTransaction.date.desc()).first()
        if last and last[0] < end - timedelta(days=20) and last[0] >= start - timedelta(days=60):
            warnings.append(f"{c.name}'s charges end on {last[0].isoformat()}: import its newer statement.")
    return {"start": start.isoformat(), "end": end.isoformat(), "per_month": per_month, "months": f.totals["months"],
            "nodes": f.nodes, "links": f.links, "totals": f.totals, "unsorted": f.unsorted, "warnings": warnings}


DRILL_GROUPS = ("income", "spend", "debt", "save", "unsorted")
MAX_DRILL_ROWS = 5000


@router.get("/detail")
def get_detail(group: str, start: date | None = None, end: date | None = None, db: Session = Depends(get_db)):
    """
    Every transaction behind one group of the diagram (cash_flow.tag, the same sorting the diagram
    uses), for the drill-in: its bar (detail), one level further (sub), and for card charges the row
    the card editor opens with.
    """
    if group not in DRILL_GROUPS:
        raise HTTPException(status_code=422, detail="Unknown group")
    start, end = _period(start, end)
    rows, spend, payments, card_rows = _inputs(db, start, end)
    entries, _left = cash_flow.tag(rows, spend, payments, _roles(db), start, end)
    entries = sorted((e for e in entries if e.group == group), key=lambda e: (e.date, e.id), reverse=True)
    if len(entries) > MAX_DRILL_ROWS:
        raise HTTPException(status_code=413, detail="Too many transactions in this period: pick a shorter one")
    from routers.cards import _txn_dict      # the card editor's row shape
    card_by_id = {t.id: t for t in card_rows}
    card_names = {c.id: c.name for c in db.query(CardAccount).all()}
    acct_names = {a.id: a.name for a in db.query(RegisterAccount).all()}
    out = []
    for e in entries:
        item = {"source": e.source, "id": e.id, "date": e.date.isoformat(), "amount": round(e.amount, 2),
                "detail": e.detail, "sub": e.sub, "label": e.label, "key": e.key, "account_id": e.account_id}
        if e.source == "card":
            t = card_by_id.get(e.id)
            item["where"] = card_names.get(e.account_id, "")
            item["txn"] = _txn_dict(t, item["where"]) if t is not None else None
        else:
            item["where"] = acct_names.get(e.account_id, "")
        out.append(item)
    return {"group": group, "start": start.isoformat(), "end": end.isoformat(),
            "months": cash_flow.months_between(start, end), "entries": out}


@router.get("/payees")
def list_payees(start: date | None = None, end: date | None = None, db: Session = Depends(get_db)):
    """Every register payee in the period (all time without dates) with its role and where the role came from."""
    rows = _cash_rows(db, start - MATCH_MARGIN if start else None, end + MATCH_MARGIN if end else None)
    lo, hi = start or date.min, end or date.max
    payments = [cash_flow.CardPayment(t.date, float(t.amount)) for t in db.query(CardTransaction)
                .filter(CardTransaction.txn_type == "payment").all() if float(t.amount) > 0]
    pairs = cash_flow.detect_pairs(rows, payments)
    roles = _roles(db)
    stored = {r.key: r for r in db.query(PayeeRole).all()}
    out: dict[str, dict] = {}
    for r in rows:
        if not (lo <= r.date <= hi):
            continue
        p = out.setdefault(r.key, {"key": r.key, "label": r.label, "count": 0, "money_in": 0.0, "money_out": 0.0,
                                   "last_date": None, "example": r.description[:80], "roles_seen": {}})
        p["count"] += 1
        p["money_in" if r.amount > 0 else "money_out"] += abs(r.amount)
        p["last_date"] = max(p["last_date"] or r.date, r.date)
        role, why = cash_flow.role_for(r, roles, pairs)
        name = role.role if role else "none"
        p["roles_seen"][name] = p["roles_seen"].get(name, 0) + 1
        p.setdefault("why", why)
        if why == "unmatched_transfer":
            p["hint"] = cash_flow.UNMATCHED_TRANSFER_HINT
    result = []
    for p in out.values():
        s = stored.get(p["key"])
        seen = p.pop("roles_seen")
        top = max(seen, key=seen.get)
        p.update({"money_in": round(p["money_in"], 2), "money_out": round(p["money_out"], 2),
                  "last_date": p["last_date"].isoformat() if p["last_date"] else None,
                  "role": s.role if s else None, "category": s.category if s else None, "target": s.target if s else None,
                  "suggested": None if top == "none" else top, "suggested_by": None if s else p.get("why"),
                  "mixed": len(seen) > 1 and not s})
        p.pop("why", None)
        result.append(p)
    result.sort(key=lambda p: (p["role"] is not None, p["suggested"] is not None, -(p["money_out"] + p["money_in"])))
    accounts = [a.name for a in db.query(Account).order_by(Account.name).all() if not a.archived]
    return {"payees": result, "roles": cash_flow.ROLES, "categories": card_taxonomy.CHOOSABLE_L1, "accounts": accounts}


class RoleBody(BaseModel):
    role: str
    label: str = ""
    category: str = ""
    target: str = ""


@router.put("/payees/{key}")
def set_role(key: str, body: RoleBody, db: Session = Depends(get_db)):
    if body.role not in cash_flow.ROLES:
        raise HTTPException(status_code=422, detail="Unknown role")
    category = body.category.strip()
    if category and (body.role != "spending" or category not in card_taxonomy.CHOOSABLE_L1):
        raise HTTPException(status_code=422, detail="A category applies to spending, and must be a top-level category")
    target = body.target.strip()[:80]
    if target and body.role not in ("debt", "saving"):
        raise HTTPException(status_code=422, detail="An account applies to debt payments and saving")
    key = key.strip().upper()
    if not key or len(key) > 120:
        raise HTTPException(status_code=422, detail="Unknown payee")
    r = db.get(PayeeRole, key) or PayeeRole(key=key, label=(body.label.strip() or key)[:120], role=body.role)
    r.role, r.category, r.target = body.role, category or None, target or None
    if body.label.strip():
        r.label = body.label.strip()[:120]
    db.add(r)
    db.commit()
    return {"key": r.key, "label": r.label, "role": r.role, "category": r.category, "target": r.target}


@router.delete("/payees/{key}")
def clear_role(key: str, db: Session = Depends(get_db)):
    r = db.get(PayeeRole, key.strip().upper())
    if r is not None:
        db.delete(r)
        db.commit()
    return {"key": key, "cleared": r is not None}
