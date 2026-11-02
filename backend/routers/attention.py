"""
GET /api/attention: the Dashboard's "Needs attention" panel. Every signal is gathered here from
what the other routers already compute (so each count matches the tab its item opens) and shaped by
attention.build (pure). Card signals come from one load of the Cards page's default period (this
year, by the user's date: X-Flow-Today).
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

import attention
import backup_service
import balance_sheet
import card_ai
import settings_store
import update_check
from auth import require_auth
from database import get_db
from models import CardAccount, ImportProfile
from routers import cards, cashflow, data, register

router = APIRouter(prefix="/api/attention", dependencies=[Depends(require_auth)])


def gather(db: Session, today: date) -> dict:
    """The inputs attention.build wants, read from the database and the in-memory state."""
    year_start = date(today.year, 1, 1)
    balance_accounts = [{"id": a["id"], "name": a["name"], "ignored": a["ignored"], "archived": a["archived"],
                         "behind": a["behind"]} for a in data.list_accounts(today.isoformat(), db)]
    cash_accounts = [{"id": a["id"], "name": a["name"], "behind": a["behind"], "unmatched": a["unmatched"],
                      "pending": a["pending"]} for a in register.list_register_accounts(today.isoformat(), db)]
    payees = cashflow.list_payees(year_start, today, db)["payees"]
    unsorted = sum(1 for p in payees if not p["role"] and not p["suggested"])

    review_open = subs_overdue = subs_price = subs_dup = 0
    if db.query(CardAccount.id).filter(CardAccount.archived == False).first() is not None:  # noqa: E712
        loaded = cards._load(db, None, year_start, today)
        review = cards.review_result(db, loaded)
        review_open = len(review["items"]) - review["reviewed_count"]
        for s in loaded.recurring_items:
            if s.get("verdict") == "dismissed":
                continue
            changed = s.get("change_detected") or ""
            if s.get("status") == "overdue":
                subs_overdue += 1
            if s.get("status") not in ("ended", "expected") and ("price change" in changed or "up " in changed):
                subs_price += 1
            if "duplicate" in (s.get("notes") or ""):
                subs_dup += 1

    backups = backup_service.list_backups()
    return {
        "today": today, "balance_accounts": balance_accounts, "cash_accounts": cash_accounts,
        "unsorted_payees": unsorted, "review_open": review_open, "subs_overdue": subs_overdue,
        "subs_price_change": subs_price, "subs_duplicate": subs_dup,
        "ai_problem": card_ai.key_problem(db) if card_ai.enabled() else None,
        "update": update_check.status().get("update"),
        "formats_unpreviewed": db.query(ImportProfile).filter(ImportProfile.needs_preview == True).count(),  # noqa: E712
        "last_backup_at": max((b.get("created_at") or "" for b in backups), default=None) or None,
        "backup_daily": settings_store.backup_daily_flag(),
    }


@router.get("")
def needs_attention(x_flow_today: str | None = Header(None), db: Session = Depends(get_db)):
    today = balance_sheet.client_today(x_flow_today, date.today())
    return {"items": attention.build(gather(db, today))}
