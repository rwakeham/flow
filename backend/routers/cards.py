"""
Credit card API: accounts, statement imports, categorized transactions,
recurring charges, spend analytics and AI insights.

All routes are prefixed /api/cards and require authentication. Card data never
feeds the dashboard: nothing outside this router reads the card tables.
"""

from __future__ import annotations

import csv
import io
import json
from collections import Counter
from datetime import date as DateType

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, text
from sqlalchemy.orm import Session

import card_ai
import card_analytics
import card_classifier
import card_import
import import_profile as ip
import card_amount_rules
import card_recurring
import card_rules
import card_taxonomy
import cash_flow
import change_log
import exports
from auth import require_auth
from backup_service import ensure_daily_backup
from database import SessionLocal, get_db
import import_profile_resolve
from models import (
    CategoryRename,
    AiRequestLog, CardAccount, CardAmountRule, CardImport, CardInsightReport, CardManualRecurring,
    CardRecurringVerdict, CustomCategory,
    CardTransaction, MerchantCategory,
)

router = APIRouter(prefix="/api/cards", dependencies=[Depends(require_auth)])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_ACCOUNT_IDS = 500         # card ids in one ?accounts= filter
TXN_TYPES = {"purchase", "refund", "benefit_credit", "card_fee", "interest", "cash_advance", "payment",
             "payment_reversal", "carry_in", "balance_transfer", "adjustment"}


# ── Accounts ─────────────────────────────────────────────────────────────────

class CardAccountBody(BaseModel):
    name: str | None = None
    issuer: str | None = None
    last4: str | None = None
    cardholder_names: str | None = None
    notes: str | None = None
    archived: bool | None = None


def _acct_dict(a: CardAccount, stats: dict | None = None) -> dict:
    stats = stats or {}
    return {
        "id": a.id, "name": a.name, "issuer": a.issuer, "last4": a.last4,
        "cardholder_names": a.cardholder_names or "", "notes": a.notes, "archived": bool(a.archived),
        "txn_count": stats.get("count", 0), "first_date": stats.get("first"), "last_date": stats.get("last"),
        "last_import": stats.get("last_import"),
    }


@router.get("/accounts")
def list_accounts(db: Session = Depends(get_db)):
    stats: dict[int, dict] = {}
    for aid, n, lo, hi in db.execute(text("""
        SELECT card_account_id, COUNT(*), MIN(date), MAX(date) FROM card_transactions GROUP BY card_account_id
    """)).fetchall():
        stats[aid] = {"count": n, "first": lo.isoformat(), "last": hi.isoformat()}
    for aid, ts in db.execute(text(
        "SELECT card_account_id, MAX(created_at) FROM card_imports GROUP BY card_account_id"
    )).fetchall():
        stats.setdefault(aid, {})["last_import"] = ts.isoformat() if ts else None
    return [_acct_dict(a, stats.get(a.id)) for a in db.query(CardAccount).order_by(CardAccount.name).all()]


@router.post("/accounts", status_code=201)
def create_account(body: CardAccountBody, db: Session = Depends(get_db)):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name is required")
    if db.query(CardAccount).filter(CardAccount.name == name).first():
        raise HTTPException(status_code=409, detail="A card with that name already exists")
    a = CardAccount(name=name, issuer=(body.issuer or "").strip() or None, last4=_numbers_field(body.last4),
                    cardholder_names=(body.cardholder_names or "").strip() or None, notes=body.notes)
    db.add(a)
    change_log.record(db, "card", "add_account", f"Added card {change_log.clip(name)}", {"name": name, "issuer": a.issuer})
    db.commit()
    db.refresh(a)
    return _acct_dict(a)


def _numbers_field(value: str | None) -> str | None:
    """Card numbers as typed ("1234 5678", "1234,5678") stored one way: "1234, 5678"."""
    return ", ".join(card_import.card_numbers(value)) or None


@router.patch("/accounts/{account_id}")
def update_account(account_id: int, body: CardAccountBody, db: Session = Depends(get_db)):
    a = db.get(CardAccount, account_id)
    if a is None:
        raise HTTPException(status_code=404, detail="Card not found")
    before = {"name": a.name, "issuer": a.issuer, "last4": a.last4, "archived": bool(a.archived)}
    fields = body.model_fields_set
    if "name" in fields:
        name = (body.name or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="Name is required")
        if db.query(CardAccount).filter(CardAccount.name == name, CardAccount.id != account_id).first():
            raise HTTPException(status_code=409, detail="A card with that name already exists")
        a.name = name
    for f in ("issuer", "cardholder_names", "notes"):
        if f in fields:
            setattr(a, f, (getattr(body, f) or "").strip() or None)
    if "last4" in fields:
        a.last4 = _numbers_field(body.last4)
    if "archived" in fields and body.archived is not None:
        a.archived = body.archived
    after = {"name": a.name, "issuer": a.issuer, "last4": a.last4, "archived": bool(a.archived)}
    changed = {k: v for k, v in after.items() if before[k] != v}
    change_log.record(db, "card", "edit_account", f"Edited card {change_log.clip(before['name'])}" + (f" ({', '.join(changed)})" if changed else ""),
                      {"id": a.id, "before": {k: before[k] for k in changed}, "after": changed})
    db.commit()
    return _acct_dict(a)


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(account_id: int, db: Session = Depends(get_db)):
    a = db.get(CardAccount, account_id)
    if a is None:
        raise HTTPException(status_code=404, detail="Card not found")
    import_profile_resolve.unlink_account(db, "card", account_id)
    change_log.record(db, "card", "delete_account", f"Deleted card {change_log.clip(a.name)} and its transactions", {"id": a.id, "name": a.name})
    db.delete(a)
    db.commit()


# ── Import ───────────────────────────────────────────────────────────────────

async def _read_and_parse(db: Session, acct: CardAccount, file: UploadFile,
                          profile_id: int | None) -> tuple[ip.CardParseResult, str, "import_profile_resolve.Candidate", list]:
    """Read a statement with the card's import format (see routers/register.py for the rules)."""
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
    filename = file.filename or "upload"
    res = import_profile_resolve.resolve_for_account(db, content, filename, "card", acct.id, profile_id)
    if res.status != "match":
        raise import_profile_resolve.problem(db, res, content, filename, "card", acct.id)
    c, parsed = res.candidate, res.parsed
    history = []
    if parsed.rows:
        lo, hi = min(r.date for r in parsed.rows), max(r.date for r in parsed.rows)
        history = [(d, float(a), t) for d, a, t in db.query(
            CardTransaction.date, CardTransaction.amount, CardTransaction.raw_description).filter(
            CardTransaction.card_account_id == acct.id, CardTransaction.date.between(lo, hi)).all()]
    check = ip.verify_sign(parsed, c.spec, history)
    if check.status == "reversed":
        raise import_profile_resolve.sign_problem(check, c)
    import_profile_resolve.mark_previewed(db, c, parsed)
    result = ip.to_card_result(parsed, c.spec, c.name, check, institution=c.institution)
    if not result.rows:
        raise HTTPException(status_code=422, detail="No transactions found in file.")
    return result, filename, c, parsed.errors


def _parse_summary(parsed: ip.CardParseResult, c) -> dict:
    return {
        "issuer_format": parsed.issuer_format, "issuer_label": parsed.issuer_label,
        "profile": import_profile_resolve.summary(c),
        "sign_convention": parsed.sign_convention, "sign_reason": parsed.sign_reason,
        "sign_check": {"status": parsed.sign_status, "reason": parsed.sign_reason},
        "sign_needs_review": parsed.sign_needs_review, "rows_parsed": len(parsed.rows),
        "pending_dropped": parsed.pending_dropped, "rows_unreadable": parsed.rows_skipped,
        "statement_start": parsed.statement_start.isoformat() if parsed.statement_start else None,
        "statement_end": parsed.statement_end.isoformat() if parsed.statement_end else None,
        "warnings": parsed.warnings,
    }


@router.post("/{account_id}/import/check")
async def check_import(account_id: int, file: UploadFile = File(...), profile_id: int | None = Query(None),
                       db: Session = Depends(get_db)):
    """Dry run: read with the card's format, de-duplicate and classify without writing anything."""
    acct = db.get(CardAccount, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Card not found")
    parsed, _, c, errors = await _read_and_parse(db, acct, file, profile_id)
    plan = card_import.plan_import(db, acct, parsed)
    problem = card_ai.key_problem(db) if card_ai.area_enabled("cards") else None
    other = card_import.other_card_match(db, acct, parsed)
    known = {n.strip().upper() for n in (acct.cardholder_names or "").splitlines() if n.strip()}
    return {
        **_parse_summary(parsed, c), "errors": errors[:20], "error_count": len(errors),
        "would_import": len(plan.to_import), "would_skip": plan.skipped_duplicates,
        "would_send_to_ai": plan.needs_ai if card_ai.area_enabled("cards") and not problem else 0,
        "ai_enabled": card_ai.area_enabled("cards"),
        "ai_problem": problem,
        "preview": plan.preview,
        "other_card_match": {**other, "message": card_import.other_card_message(other, acct.name)} if other else None,
        "suggested_cardholders": [n for n in parsed.suggested_cardholders if n.strip().upper() not in known],
        "new_card_numbers": _new_numbers(acct, parsed),
    }


def _new_numbers(acct: CardAccount, parsed: ip.CardParseResult) -> list[str]:
    """Card numbers in the file the account doesn't list yet; an import adds them."""
    known = {n[-4:] for n in card_import.card_numbers(acct.last4)}
    return [n for n in parsed.card_numbers if n[-4:] not in known]


@router.post("/{account_id}/import")
async def import_statement(account_id: int, background: BackgroundTasks, file: UploadFile = File(...),
                           profile_id: int | None = Query(None), allow_other_card: bool = Query(False),
                           allow_errors: bool = Query(False), db: Session = Depends(get_db)):
    """
    Import a statement. A file whose transactions are mostly already on another
    card is refused with 409 unless ``allow_other_card`` confirms it (see
    card_import.other_card_match): exports without a card number can only be
    attributed by the account picked, so a wrong pick would double-count.
    """
    acct = db.get(CardAccount, account_id)
    if acct is None:
        raise HTTPException(status_code=404, detail="Card not found")
    parsed, filename, c, errors = await _read_and_parse(db, acct, file, profile_id)
    if errors and not allow_errors:
        raise HTTPException(status_code=422, detail={
            "code": "row_errors", "errors": errors[:20], "error_count": len(errors),
            "message": f"{len(errors)} row(s) couldn't be read with the format \"{c.name}\". "
                       "Fix the format, or confirm to import the rest."})
    if not allow_other_card:
        other = card_import.other_card_match(db, acct, parsed)
        if other:
            raise HTTPException(status_code=409, detail=card_import.other_card_message(other, acct.name)
                                + " Pick that card, or confirm to import here anyway.")
    try:
        ensure_daily_backup(db)
    except Exception:
        pass  # a backup failure must never block an import
    plan = card_import.plan_import(db, acct, parsed)
    imp = card_import.apply_import(db, acct, parsed, plan, filename)
    imp.profile_id = c.id
    import_profile_resolve.touch(db, c)
    # A card added to the account (authorized user) or a reissued number: the
    # next file with it is recognised by number (see import_batch).
    new_numbers = card_import.remember_numbers(acct, parsed.card_numbers)
    card_amount_rules.apply(db)
    db.flush()
    change_log.record(db, "card", "import", f"Imported {change_log.clip(filename, 40)} into {change_log.clip(acct.name)}: {len(plan.to_import)} rows ({plan.skipped_duplicates} already on file)",
                      {"card_account_id": acct.id, "import_id": imp.id, "filename": filename, "format": c.name,
                       "imported": len(plan.to_import), "skipped": plan.skipped_duplicates, "new_card_numbers": new_numbers})
    db.commit()

    # A key the API already rejected is not retried automatically (see card_ai.key_problem).
    problem = card_ai.key_problem(db) if card_ai.area_enabled("cards") else None
    queued = plan.needs_ai if card_ai.area_enabled("cards") and not problem else 0
    if queued:
        background.add_task(card_ai.run_categorization, SessionLocal)
    return {
        **_parse_summary(parsed, c), "import_id": imp.id,
        "imported": len(plan.to_import), "skipped": plan.skipped_duplicates, "new_card_numbers": new_numbers,
        "ai": {"enabled": card_ai.area_enabled("cards"), "queued_merchants": queued, "reason": card_ai.area_off_reason("cards"),
               "problem": problem},
    }


@router.get("/imports")
def list_imports(account_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(CardImport, CardAccount.name).join(CardAccount, CardAccount.id == CardImport.card_account_id)
    if account_id:
        q = q.filter(CardImport.card_account_id == account_id)
    return [{
        "id": i.id, "card_account_id": i.card_account_id, "card": name, "filename": i.filename,
        "issuer_format": i.issuer_format, "sign_convention": i.sign_convention, "sign_reason": i.sign_reason,
        "rows_parsed": i.rows_parsed, "rows_imported": i.rows_imported, "rows_skipped": i.rows_skipped,
        "pending_dropped": i.pending_dropped,
        "date_min": i.date_min.isoformat() if i.date_min else None,
        "date_max": i.date_max.isoformat() if i.date_max else None,
        "created_at": i.created_at.isoformat() if i.created_at else None,
    } for i, name in q.order_by(CardImport.created_at.desc()).limit(200).all()]


@router.delete("/imports/{import_id}", status_code=204)
def undo_import(import_id: int, db: Session = Depends(get_db)):
    imp = db.get(CardImport, import_id)
    if imp is None:
        raise HTTPException(status_code=404, detail="Import not found")
    card = db.get(CardAccount, imp.card_account_id)
    change_log.record(db, "card", "undo_import", f"Undid the import of {change_log.clip(imp.filename or '', 40)} into {change_log.clip(card.name if card else '')}: {imp.rows_imported} rows removed",
                      {"import_id": imp.id, "card_account_id": imp.card_account_id, "rows": imp.rows_imported})
    db.query(CardTransaction).filter(CardTransaction.import_id == import_id).delete(synchronize_session=False)
    db.delete(imp)
    db.commit()


# ── Shared loading for transactions and analytics ────────────────────────────

def _parse_ids(accounts: str | None, db: Session) -> list[int]:
    if accounts:
        try:
            ids = [int(x) for x in accounts.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=422, detail="accounts must be comma-separated ids")
        if len(ids) > MAX_ACCOUNT_IDS:
            raise HTTPException(status_code=422, detail=f"At most {MAX_ACCOUNT_IDS} accounts at a time")
        return ids
    return [a.id for a in db.query(CardAccount).filter(CardAccount.archived == False).all()]  # noqa: E712


class Loaded:
    def __init__(self, rows, coverage, card_names, card_meta, common, recurring_items, recurring_totals,
                 txns_by_id):
        self.rows = rows
        self.coverage = coverage
        self.card_names = card_names
        self.card_meta = card_meta
        self.common = common
        self.recurring_items = recurring_items
        self.recurring_totals = recurring_totals
        self.txns_by_id = txns_by_id

    @property
    def common_rows(self) -> list[card_analytics.Row]:
        if not self.common["exists"]:
            return []
        lo, hi = DateType.fromisoformat(self.common["start"]), DateType.fromisoformat(self.common["end"])
        return [r for r in self.rows if lo <= r.date <= hi]


def _load(db: Session, accounts: str | None, date_from: DateType | None, date_to: DateType | None,
          common_period: bool = False) -> Loaded:
    ids = _parse_ids(accounts, db)
    accts = {a.id: a for a in db.query(CardAccount).filter(CardAccount.id.in_(ids)).all()} if ids else {}
    ids = list(accts)
    card_names = {aid: a.name for aid, a in accts.items()}
    card_meta = {aid: {"name": a.name, "issuer": a.issuer} for aid, a in accts.items()}
    txns = (db.query(CardTransaction).filter(CardTransaction.card_account_id.in_(ids))
            .order_by(CardTransaction.date, CardTransaction.id).all()) if ids else []
    cache = {c.merchant_key: c for c in db.query(MerchantCategory).all()}
    full_cov = card_import.coverage_by_card(db, ids)

    # Recurring detection always looks at the full history: cadence needs it.
    vrows = db.query(CardRecurringVerdict).all()
    verdicts = {(v.canonical_merchant, v.card_account_id): v.status for v in vrows if v.status}
    ended = {(v.canonical_merchant, v.card_account_id): v.ended_through for v in vrows if v.ended_through}
    charges = []
    for t in txns:
        if t.txn_type != "purchase":
            continue
        c = cache.get(t.merchant_key)
        charges.append(card_recurring.Charge(
            id=t.id, card_account_id=t.card_account_id, card=card_names[t.card_account_id], date=t.date,
            spend=-float(t.amount), canonical_merchant=t.canonical_merchant, cat_l1=t.cat_l1, cat_l2=t.cat_l2,
            cat_l3=t.cat_l3, installment_flag=t.installment_flag,
            subscription_hint=bool(t.subscription_hint or (c is not None and c.subscription_hint)),
            merchant_family=t.merchant_family or "",
        ))
    manual = [card_recurring.ManualSeries(
        id=m.id, name=m.name, merchant=m.merchant, cadence=m.cadence, kind=m.kind, card_account_id=m.card_account_id,
        amount_min=m.amount_min, amount_max=m.amount_max, ended_through=m.ended_through)
        for m in db.query(CardManualRecurring).order_by(CardManualRecurring.id).all()
        if m.card_account_id is None or m.card_account_id in card_names]
    items, kinds = card_recurring.detect(charges, full_cov, verdicts, ended, manual, card_names)

    coverage = card_import.clip_coverage(full_cov, date_from, date_to)
    common = card_analytics.common_period(coverage)
    rows = []
    for t in txns:
        if (date_from and t.date < date_from) or (date_to and t.date > date_to):
            continue
        c = cache.get(t.merchant_key)
        rows.append(card_analytics.Row(
            id=t.id, card_account_id=t.card_account_id, card=card_names[t.card_account_id], date=t.date,
            amount=float(t.amount), txn_type=t.txn_type, txn_subtype=t.txn_subtype or "", is_spend=t.is_spend,
            canonical_merchant=t.canonical_merchant, merchant_family=t.merchant_family, cat_l1=t.cat_l1,
            cat_l2=t.cat_l2, cat_l3=t.cat_l3, category_source=t.category_source, foreign_flag=t.foreign_flag,
            raw_description=t.raw_description, recurring_kind=kinds.get(t.id, ""),
            ai_confidence=c.confidence if (c is not None and c.source == "ai") else None,
        ))
    loaded = Loaded(rows, coverage, card_names, card_meta, common, items, card_recurring.totals(items),
                    {t.id: t for t in txns})
    if common_period:
        loaded.rows = loaded.common_rows
    return loaded


# ── Taxonomy and transactions ────────────────────────────────────────────────

@router.get("/taxonomy")
def taxonomy():
    return {"tree": card_taxonomy.as_list(), "txn_types": sorted(TXN_TYPES)}


# ── Your own subcategories and details ──────────────────────────────────────

class CustomCategoryBody(BaseModel):
    l1: str
    l2: str                            # the subcategory (new, or the parent of a new detail)
    l3: str = ""                       # set to add a detail under l2
    fixedness: str = "discretionary"
    role: str = "none"
    rewards: str = "general"
    ai_visible: bool = True


class CustomCategoryEdit(BaseModel):
    name: str | None = None            # rename (the subcategory, or the detail)
    fixedness: str | None = None
    role: str | None = None
    rewards: str | None = None
    ai_visible: bool | None = None


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > 60 or any(ch in name for ch in "|›>"):
        raise HTTPException(status_code=422, detail="Names need 1 to 60 characters and no | › or >")
    return name


def _check_attrs(fixedness, role, rewards) -> None:
    if fixedness is not None and fixedness not in card_taxonomy.FIXEDNESS_CHOICES:
        raise HTTPException(status_code=422, detail="Unknown fixedness")
    if role is not None and role not in card_taxonomy.ROLE_CHOICES:
        raise HTTPException(status_code=422, detail="Unknown role")
    if rewards is not None and rewards not in card_taxonomy.REWARD_CHOICES:
        raise HTTPException(status_code=422, detail="Unknown rewards bucket")


def _custom_dict(c: CustomCategory, db: Session) -> dict:
    q = db.query(func.count(CardTransaction.id)).filter(CardTransaction.cat_l1 == c.l1, CardTransaction.cat_l2 == c.l2)
    if c.l3:
        q = q.filter(CardTransaction.cat_l3 == c.l3)
    return {"id": c.id, "l1": c.l1, "l2": c.l2, "l3": c.l3, "fixedness": c.fixedness, "role": c.role,
            "rewards": c.rewards, "ai_visible": c.ai_visible, "transactions": q.scalar() or 0}


@router.get("/categories")
def list_custom_categories(db: Session = Depends(get_db)):
    return {"custom": [_custom_dict(c, db) for c in db.query(CustomCategory).order_by(CustomCategory.l1, CustomCategory.l2,
                                                                                         CustomCategory.l3).all()],
            "l1": card_taxonomy.CHOOSABLE_L1, "fixedness": card_taxonomy.FIXEDNESS_CHOICES,
            "roles": list(card_taxonomy.ROLE_CHOICES), "rewards": list(card_taxonomy.REWARD_CHOICES)}


@router.post("/categories", status_code=201)
def add_custom_category(body: CustomCategoryBody, db: Session = Depends(get_db)):
    """A new subcategory under an existing category, or a new detail under a subcategory."""
    l1, l2, l3 = body.l1.strip(), _clean_name(body.l2), (body.l3 or "").strip()
    if l1 not in card_taxonomy.CHOOSABLE_L1:
        raise HTTPException(status_code=422, detail="Unknown category")
    _check_attrs(body.fixedness, body.role, body.rewards)
    if l3:
        l3 = _clean_name(l3)
        if not card_taxonomy.is_valid(l1, l2):
            raise HTTPException(status_code=422, detail=f"{l1} has no subcategory {l2}")
        if l3 in card_taxonomy.tree()[l1][l2]:
            raise HTTPException(status_code=409, detail=f"{l2} already has {l3}")
    elif card_taxonomy.is_valid(l1, l2):
        raise HTTPException(status_code=409, detail=f"{l1} already has {l2}")
    c = CustomCategory(l1=l1, l2=l2, l3=l3, fixedness=body.fixedness, role=body.role, rewards=body.rewards,
                       ai_visible=body.ai_visible)
    db.add(c)
    change_log.record(db, "category", "add", f"Added {'detail' if l3 else 'subcategory'} {l1} › {l2}" + (f" › {l3}" if l3 else ""),
                      {"l1": l1, "l2": l2, "l3": l3, "fixedness": body.fixedness, "role": body.role, "rewards": body.rewards, "ai_visible": body.ai_visible})
    db.commit()
    card_taxonomy.refresh(db)
    return _custom_dict(c, db)


def _retag(db: Session, old: tuple[str, str, str], new: tuple[str, str, str], keep_l3: bool) -> int:
    """
    Move everything filed under ``old`` (l1, l2, and l3 when set) to ``new``: transactions, the merchant
    cache, amount-rule targets and the categories charges had before a rule. With ``keep_l3`` a
    subcategory keeps its details (a detail named like the subcategory follows the new name).
    """
    l1, l2, l3 = old
    n1, n2, n3 = new

    def target(cur_l3: str) -> tuple[str, str, str]:
        if l3 or not keep_l3:
            return n1, n2, n3
        return n1, n2, (n2 if cur_l3 == l2 else cur_l3)

    moved = 0
    for model in (CardTransaction, MerchantCategory):
        q = db.query(model).filter(model.cat_l1 == l1, model.cat_l2 == l2)
        if l3:
            q = q.filter(model.cat_l3 == l3)
        for row in q.all():
            row.cat_l1, row.cat_l2, row.cat_l3 = target(row.cat_l3)
            moved += model is CardTransaction
    for r in db.query(CardAmountRule).filter(CardAmountRule.cat_l1 == l1, CardAmountRule.cat_l2 == l2).all():
        if not l3 or r.cat_l3 == l3:
            r.cat_l1, r.cat_l2, r.cat_l3 = target(r.cat_l3)
    for t in db.query(CardTransaction).filter(CardTransaction.pre_rule_category.like(f"{l1}|{l2}|%")).all():
        p = t.pre_rule_category.split("|")
        if len(p) == 4 and (not l3 or p[2] == l3):
            t.pre_rule_category = "|".join((*target(p[2]), p[3]))
    return moved


@router.patch("/categories/{cat_id}")
def edit_custom_category(cat_id: int, body: CustomCategoryEdit, db: Session = Depends(get_db)):
    c = db.get(CustomCategory, cat_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Not found")
    _check_attrs(body.fixedness, body.role, body.rewards)
    old = (c.l1, c.l2, c.l3)
    for k in ("fixedness", "role", "rewards", "ai_visible"):
        if getattr(body, k) is not None:
            setattr(c, k, getattr(body, k))
    moved = 0
    if body.name is not None:
        moved = _rename(db, c.l1, c.l2, c.l3, body.name)
    change_log.record(db, "category", "edit", f"Edited {old[0]} › {old[1]}" + (f" › {old[2]}" if old[2] else "") + (f": renamed to {change_log.clip(body.name)} ({moved} charges)" if body.name is not None else ""),
                      {"id": c.id, "was": old, "now": (c.l1, c.l2, c.l3), "moved": moved})
    db.commit()
    card_taxonomy.refresh(db)
    return {**_custom_dict(c, db), "moved": moved}


def _rename(db: Session, l1: str, l2: str, l3: str, new_name: str) -> int:
    """
    Rename a subcategory (l3 "") or detail, built-in or yours, by its current name. Everything filed
    under it follows (_retag); a built-in rename is remembered in category_renames so the rules'
    answers follow too. Returns the number of transactions moved; the caller commits and refreshes.
    """
    name = _clean_name(new_name)
    if l1 not in card_taxonomy.CHOOSABLE_L1:
        raise HTTPException(status_code=422, detail=f"{l1}'s subcategories can't be renamed")
    t = card_taxonomy.tree()
    if l2 not in t.get(l1, {}):
        raise HTTPException(status_code=404, detail=f"{l1} has no subcategory {l2}")
    if (l3 or l2) == name:
        return 0
    if l3:
        if name in t[l1][l2]:
            raise HTTPException(status_code=409, detail=f"{l2} already has {name}")
    elif name in t[l1]:
        raise HTTPException(status_code=409, detail=f"{l1} already has {name}")

    if l3:
        custom = db.query(CustomCategory).filter(CustomCategory.l1 == l1, CustomCategory.l2 == l2,
                                                 CustomCategory.l3 == l3).first()
        builtin = custom is None and card_taxonomy.is_builtin(l1, l2, l3)
        moved = _retag(db, (l1, l2, l3), (l1, l2, name), keep_l3=False)
        if custom is not None:
            custom.l3 = name
    else:
        custom = db.query(CustomCategory).filter(CustomCategory.l1 == l1, CustomCategory.l2 == l2,
                                                 CustomCategory.l3 == "").first()
        builtin = custom is None and card_taxonomy.is_builtin(l1, l2)
        moved = _retag(db, (l1, l2, ""), (l1, name, ""), keep_l3=True)
        for r in db.query(CardAmountRule).filter(CardAmountRule.scope_l1 == l1, CardAmountRule.scope_l2 == l2).all():
            r.scope_l2 = name
        for d in db.query(CustomCategory).filter(CustomCategory.l1 == l1, CustomCategory.l2 == l2).all():
            d.l2 = name                      # the subcategory itself (yours) and your details under it
    if builtin:
        _, o2, o3 = card_taxonomy.base(l1, l2, l3)
        key = (l1, o2, o3 if l3 else "")
        original = o3 if l3 else o2
        row = db.query(CategoryRename).filter(CategoryRename.l1 == key[0], CategoryRename.l2 == key[1],
                                              CategoryRename.l3 == key[2]).first()
        if name == original:
            if row is not None:
                db.delete(row)
        else:
            if row is None:
                row = CategoryRename(l1=key[0], l2=key[1], l3=key[2], new_name=name)
                db.add(row)
            row.new_name = name
    db.flush()
    return moved


class RenameBody(BaseModel):
    l1: str
    l2: str
    l3: str = ""                       # empty: rename the subcategory
    new_name: str


@router.post("/categories/rename")
def rename_category(body: RenameBody, db: Session = Depends(get_db)):
    """Rename any subcategory or detail (built-in or yours); charges, merchants and rules follow."""
    moved = _rename(db, body.l1.strip(), body.l2.strip(), body.l3.strip(), body.new_name)
    change_log.record(db, "category", "rename", f"Renamed {body.l1.strip()} › {body.l2.strip()}" + (f" › {body.l3.strip()}" if body.l3.strip() else "") + f" to {change_log.clip(body.new_name)} ({moved} charges)",
                      {"l1": body.l1, "l2": body.l2, "l3": body.l3, "new_name": body.new_name, "moved": moved})
    db.commit()
    card_taxonomy.refresh(db)
    return {"moved": moved, "l1": body.l1, "l2": body.l2, "l3": body.l3, "name": _clean_name(body.new_name)}


@router.get("/categories/renames")
def list_renames():
    """Built-in names you renamed: original → current."""
    return {"renames": [{"l1": l1, "l2": l2, "l3": l3, "name": new, "current_l2": card_taxonomy.current(l1, l2)[1]}
                        for (l1, l2, l3), new in sorted(card_taxonomy.renames().items())]}


class MoveBody(BaseModel):
    from_l1: str
    from_l2: str
    from_l3: str = ""                  # empty: the whole subcategory
    to_l1: str
    to_l2: str
    to_l3: str = ""                    # empty: the destination subcategory itself


def _under(cat: tuple[str, str, str], l1: str, l2: str, l3: str) -> bool:
    return cat[0] == l1 and cat[1] == l2 and (not l3 or cat[2] == l3)


@router.post("/categories/move")
def move_category(body: MoveBody, db: Session = Depends(get_db)):
    """
    Move every charge, cached merchant and amount-rule target filed under one subcategory (or
    detail) to another, and remember each merchant there so future imports follow.
    """
    f1, f2, f3 = body.from_l1.strip(), body.from_l2.strip(), body.from_l3.strip()
    t1, t2, t3 = body.to_l1.strip(), body.to_l2.strip(), body.to_l3.strip()
    if not card_taxonomy.is_valid(f1, f2):
        raise HTTPException(status_code=404, detail=f"{f1} has no subcategory {f2}")
    if t1 not in card_taxonomy.CHOOSABLE_L1 or not card_taxonomy.is_valid(t1, t2):
        raise HTTPException(status_code=422, detail=f"{t1} has no subcategory {t2}")
    if t3 and t3 != t2 and t3 not in card_taxonomy.tree()[t1][t2]:
        raise HTTPException(status_code=422, detail=f"{t2} has no detail {t3}: add it first")
    dest = (t1, t2, t3 or t2)
    if (f1, f2, f3) == (t1, t2, t3) or (f3 and dest == (f1, f2, f3)):
        raise HTTPException(status_code=422, detail="That's the same category")

    def base_cat(t: CardTransaction) -> tuple[str, str, str]:
        p = (t.pre_rule_category or "").split("|")
        return tuple(p[:3]) if len(p) == 4 else (t.cat_l1, t.cat_l2, t.cat_l3)

    affected = [t for t in db.query(CardTransaction).filter(
        (CardTransaction.cat_l1 == f1) | CardTransaction.pre_rule_category.like(f"{f1}|%")).all()
        if _under((t.cat_l1, t.cat_l2, t.cat_l3), f1, f2, f3) or _under(base_cat(t), f1, f2, f3)]
    moved = _retag(db, (f1, f2, f3), dest, keep_l3=False)
    remembered = set()
    for t in affected:
        if t.category_source not in ("user", "amount_rule"):
            t.category_source = "merchant"
        if t.merchant_key and t.txn_type in card_classifier.MERCHANT_TYPES and t.merchant_key not in remembered \
                and _under(base_cat(t), *dest):
            _remember(db, t.merchant_key, t.canonical_merchant, t.merchant_family or t.canonical_merchant, *dest)
            remembered.add(t.merchant_key)
    db.flush()
    card_amount_rules.apply(db)
    scoped = 0 if f3 else db.query(CardAmountRule).filter(CardAmountRule.scope_l1 == f1,
                                                           CardAmountRule.scope_l2 == f2).count()
    change_log.record(db, "category", "move", f"Moved {f1} › {f2}" + (f" › {f3}" if f3 else "") + f" to {t1} › {t2}" + (f" › {t3}" if t3 else "") + f" ({moved} charges, {len(remembered)} merchants)",
                      {"from": (f1, f2, f3), "to": (t1, t2, t3), "moved": moved, "merchants": len(remembered)})
    db.commit()
    return {"moved": moved, "merchants": len(remembered), "rules_left_on_old_subcategory": scoped}


@router.delete("/categories/{cat_id}")
def delete_custom_category(cat_id: int, move_l1: str = "", move_l2: str = "", move_l3: str = "",
                           db: Session = Depends(get_db)):
    """Delete one of yours; its charges, merchants and rules move to the category you pick."""
    c = db.get(CustomCategory, cat_id)
    if c is None:
        return {"moved": 0}
    names = (c.l1, c.l2, c.l3)
    in_use = _custom_dict(c, db)["transactions"] or db.query(MerchantCategory).filter(
        MerchantCategory.cat_l1 == c.l1, MerchantCategory.cat_l2 == c.l2).count()
    moved = rules_dropped = 0
    if in_use or db.query(CardAmountRule).filter(CardAmountRule.cat_l1 == c.l1, CardAmountRule.cat_l2 == c.l2).count():
        if not card_taxonomy.is_valid(move_l1, move_l2) or (move_l1, move_l2) == (c.l1, c.l2) and not c.l3:
            raise HTTPException(status_code=422, detail="Choose where its charges should go")
        dest = card_classifier.coerce_category(move_l1, move_l2, move_l3)
        moved = _retag(db, (c.l1, c.l2, c.l3), dest, keep_l3=False)
    if not c.l3:
        for r in db.query(CardAmountRule).filter(CardAmountRule.scope_l1 == c.l1, CardAmountRule.scope_l2 == c.l2).all():
            db.delete(r)
            rules_dropped += 1
        db.query(CustomCategory).filter(CustomCategory.l1 == c.l1, CustomCategory.l2 == c.l2).delete()
    else:
        db.delete(c)
    db.flush()
    card_amount_rules.apply(db)
    change_log.record(db, "category", "delete", f"Deleted {names[0]} › {names[1]}" + (f" › {names[2]}" if names[2] else "") + (f": {moved} charges moved to {move_l1} › {move_l2}" if moved else ""),
                      {"deleted": names, "moved_to": (move_l1, move_l2, move_l3) if moved else None, "moved": moved, "rules_removed": rules_dropped})
    db.commit()
    card_taxonomy.refresh(db)
    return {"moved": moved, "rules_removed": rules_dropped}


def _txn_dict(t: CardTransaction, card: str, recurring_kind: str = "", ai_confidence: float | None = None) -> dict:
    return {
        "id": t.id, "card_account_id": t.card_account_id, "card": card, "date": t.date.isoformat(),
        "raw_description": t.raw_description, "merchant_key": t.merchant_key, "merchant_label": t.merchant_label,
        "canonical_merchant": t.canonical_merchant, "merchant_family": t.merchant_family,
        "amount": float(t.amount), "txn_type": t.txn_type, "is_spend": t.is_spend,
        "cat_l1": t.cat_l1, "cat_l2": t.cat_l2, "cat_l3": t.cat_l3, "category_source": t.category_source,
        "issuer_category": t.issuer_category, "processor": t.processor, "foreign_flag": t.foreign_flag,
        "installment_flag": t.installment_flag, "notes": t.notes, "recurring_kind": recurring_kind,
        "ai_confidence": ai_confidence,
    }


@router.get("/transactions")
def list_transactions(
    accounts: str | None = None, date_from: DateType | None = Query(None, alias="from"),
    date_to: DateType | None = Query(None, alias="to"), q: str | None = None, l1: str | None = None,
    l2: str | None = None, l3: str | None = None, merchant: str | None = None, type: str = "spend",
    source: str | None = None, recurring: bool = False, page: int = 1, per_page: int = 50,
    db: Session = Depends(get_db),
):
    ld = _load(db, accounts, date_from, date_to)
    rows = ld.rows
    if type == "spend":
        rows = [r for r in rows if r.is_spend]
    elif type == "excluded":
        rows = [r for r in rows if not r.is_spend]
    elif type in TXN_TYPES:
        rows = [r for r in rows if r.txn_type == type]
    if l1:
        rows = [r for r in rows if r.cat_l1 == l1]
    if l2:
        rows = [r for r in rows if r.cat_l2 == l2]
    if l3:
        rows = [r for r in rows if r.cat_l3 == l3]
    if merchant:
        rows = [r for r in rows if r.canonical_merchant == merchant]
    if source == "needs_review":
        rows = [r for r in rows if r.cat_l1 == "Uncategorized"
                or (r.ai_confidence is not None and r.ai_confidence < 0.5)]
    elif source:
        rows = [r for r in rows if r.category_source == source]
    if recurring:
        rows = [r for r in rows if r.recurring_kind]
    if q:
        needle = q.strip().lower()
        rows = [r for r in rows if needle in r.raw_description.lower() or needle in r.canonical_merchant.lower()
                or needle in f"{abs(r.amount):.2f}" or needle in r.cat_l2.lower() or needle in r.cat_l3.lower()]
    rows = sorted(rows, key=lambda r: (r.date, r.id), reverse=True)
    per_page = max(1, min(per_page, 500))
    total = len(rows)
    page_rows = rows[(max(page, 1) - 1) * per_page: max(page, 1) * per_page]
    return {
        "total": total, "page": page, "per_page": per_page,
        "sum_spend": round(sum(r.spend for r in rows if r.is_spend), 2),
        "transactions": [_txn_dict(ld.txns_by_id[r.id], r.card, r.recurring_kind, r.ai_confidence)
                         for r in page_rows],
    }


# ── Editing a merchant's name and category ──────────────────────────────────
#
# A "merchant" is what the user sees: every purchase or refund shown under one
# name, plus the other rows that share those rows' statement spellings
# (merchant keys). Rows the user changed on their own (category_source 'user'
# or name_set_by_user) are kept by merchant-wide edits unless overwrite_user.

def _merchant_rows(db: Session, name: str, key: str = "") -> list[CardTransaction]:
    base = db.query(CardTransaction).filter(CardTransaction.txn_type.in_(["purchase", "refund"]))
    rows = {t.id: t for t in base.filter(CardTransaction.canonical_merchant == name).all()}
    keys = {t.merchant_key for t in rows.values() if t.merchant_key} | ({key} if key else set())
    if keys:
        rows.update({t.id: t for t in base.filter(CardTransaction.merchant_key.in_(keys)).all()})
    return list(rows.values())


def _individual(t: CardTransaction) -> bool:
    return t.category_source == "user" or bool(t.name_set_by_user)


def _scope_info(db: Session, rows: list[CardTransaction], focus_id: int | None = None) -> dict:
    keys = sorted({t.merchant_key for t in rows if t.merchant_key})
    cache = {c.merchant_key: c for c in db.query(MerchantCategory).filter(MerchantCategory.merchant_key.in_(keys)).all()} if keys else {}
    return {"count": len(rows), "keys": keys,
            "user_set": sum(1 for t in rows if t.id != focus_id and _individual(t)),
            "remembered": bool(keys) and all(k in cache and cache[k].source == "user" for k in keys)}


@router.get("/transactions/{txn_id}/merchant-scope")
def merchant_scope(txn_id: int, db: Session = Depends(get_db)):
    """What "all transactions from this merchant" covers, for the edit dialog."""
    t = db.get(CardTransaction, txn_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    rows = _merchant_rows(db, t.canonical_merchant, t.merchant_key) if t.txn_type in ("purchase", "refund") else [t]
    if t.id not in {r.id for r in rows}:
        rows.append(t)
    return {"name": t.canonical_merchant, "family": t.merchant_family, "merchant_key": t.merchant_key,
            **_scope_info(db, rows, t.id)}


class EditBody(BaseModel):
    name: str
    family: str = ""
    cat_l1: str
    cat_l2: str
    cat_l3: str = ""
    scope: str = "this"                # 'this' | 'merchant'
    remember: bool = False             # scope 'this': also the merchant's name/category for future imports
    overwrite_user: bool = False       # scope 'merchant': also change rows set individually


def _category(body) -> tuple[str, str, str]:
    if not card_taxonomy.is_valid(body.cat_l1, body.cat_l2):
        raise HTTPException(status_code=422, detail="Unknown category")
    if body.cat_l1 == card_taxonomy.EXCLUDED_L1:
        raise HTTPException(status_code=422, detail="Use the transaction type to exclude a row from spend")
    return card_classifier.coerce_category(body.cat_l1, body.cat_l2, body.cat_l3)


def _names(name: str, family: str) -> tuple[str, str]:
    name = (name or "").strip()[:120]
    if not name:
        raise HTTPException(status_code=422, detail="The merchant needs a name")
    return name, ((family or "").strip()[:120] or name)


def _remember(db: Session, key: str, name: str, family: str, l1: str, l2: str, l3: str) -> None:
    c = db.get(MerchantCategory, key)
    if c is None:
        c = MerchantCategory(merchant_key=key)
        db.add(c)
    c.canonical_merchant, c.merchant_family = name, family
    c.cat_l1, c.cat_l2, c.cat_l3 = l1, l2, l3
    c.source = "user"


def apply_merchant_edit(db: Session, rows: list[CardTransaction], name: str, family: str,
                        cat: tuple[str, str, str], overwrite_user: bool, focus_id: int | None = None) -> dict:
    """Rename and recategorize a merchant's rows and remember it for every statement spelling."""
    updated = kept = 0
    for t in rows:
        if t.id != focus_id and _individual(t) and not overwrite_user:
            kept += 1
            continue
        t.canonical_merchant, t.merchant_family = name, family
        t.cat_l1, t.cat_l2, t.cat_l3 = cat
        t.category_source, t.name_set_by_user, t.pre_rule_category = "merchant", False, None
        updated += 1
    for key in {t.merchant_key for t in rows if t.merchant_key}:
        _remember(db, key, name, family, *cat)
    return {"updated": updated, "kept_user_overrides": kept}


def _rules_follow(db: Session, old_names: set[str], name: str) -> None:
    """After a merchant edit: its amount rules follow the new name and still win over its new category."""
    lowered = {n.lower() for n in old_names}
    for r in db.query(CardAmountRule).all():
        if r.merchant and r.merchant.lower() in lowered:   # category rules have no merchant
            r.merchant = name
    db.flush()
    card_amount_rules.apply(db, old_names | {name})


@router.post("/transactions/{txn_id}/edit")
def edit_transaction_merchant(txn_id: int, body: EditBody, db: Session = Depends(get_db)):
    """
    One dialog for name and category. scope 'this': this row only (it is then
    "set individually"); 'remember' also saves it as the merchant's answer for
    future imports. scope 'merchant': every row of the merchant (see
    _merchant_rows), keeping rows set individually unless overwrite_user.
    """
    t = db.get(CardTransaction, txn_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    cat = _category(body)
    name, family = _names(body.name, body.family)
    old = {"merchant": t.canonical_merchant, "category": [t.cat_l1, t.cat_l2, t.cat_l3]}
    if body.scope == "merchant":
        rows = _merchant_rows(db, t.canonical_merchant, t.merchant_key)
        if t.id not in {r.id for r in rows}:
            rows.append(t)
        old_names = {r.canonical_merchant for r in rows}
        out = apply_merchant_edit(db, rows, name, family, cat, body.overwrite_user, focus_id=t.id)
        _rules_follow(db, old_names, name)
        change_log.record(db, "card", "merchant_edit", f"{change_log.clip(old['merchant'])} → {change_log.clip(name)}, {cat[0]} › {cat[1]} ({out.get('updated', 0)} charges)",
                          {"txn_id": t.id, "before": old, "name": name, "family": family, "category": list(cat), **{k: v for k, v in out.items() if isinstance(v, int)}})
        db.commit()
        return {**out, "remembered": bool(t.merchant_key)}
    if body.scope != "this":
        raise HTTPException(status_code=422, detail="scope must be 'this' or 'merchant'")
    if (name, family) != (t.canonical_merchant, t.merchant_family):
        t.name_set_by_user = True
    t.canonical_merchant, t.merchant_family = name, family
    t.cat_l1, t.cat_l2, t.cat_l3 = cat
    t.category_source, t.pre_rule_category = "user", None
    remembered = bool(t.merchant_key and body.remember)
    if remembered:
        _remember(db, t.merchant_key, name, family, *cat)
    change_log.record(db, "card", "charge_edit", f"{t.date} {change_log.clip(old['merchant'])} {change_log.money(-float(t.amount))} → {change_log.clip(name)}, {cat[0]} › {cat[1]}" + (" (remembered)" if remembered else ""),
                      {"txn_id": t.id, "before": old, "name": name, "category": list(cat), "remembered": remembered})
    db.commit()
    return {"updated": 1, "kept_user_overrides": 0, "remembered": remembered}


class TxnBody(BaseModel):
    notes: str | None = None
    txn_type: str | None = None


@router.patch("/transactions/{txn_id}")
def update_transaction(txn_id: int, body: TxnBody, db: Session = Depends(get_db)):
    t = db.get(CardTransaction, txn_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Transaction not found")
    old_type, old_notes = t.txn_type, t.notes
    if "notes" in body.model_fields_set:
        t.notes = (body.notes or "").strip() or None
    if body.txn_type is not None and body.txn_type != t.txn_type:
        if body.txn_type not in TXN_TYPES:
            raise HTTPException(status_code=422, detail="Unknown transaction type")
        t.txn_type = body.txn_type
        t.txn_subtype = None
        t.is_spend = body.txn_type in card_rules.SPEND_TYPES
        if body.txn_type in card_classifier.MERCHANT_TYPES:
            if t.cat_l1 == card_taxonomy.EXCLUDED_L1:
                r = card_rules.classify_merchant(card_classifier.upper_desc(t.raw_description),
                                                 t.issuer_category or "", fallback_name=t.merchant_label or None)
                t.canonical_merchant, t.merchant_family = r["canonical"], r["family"]
                t.cat_l1, t.cat_l2, t.cat_l3 = card_taxonomy.current(r["l1"], r["l2"], r["l3"])
            if body.txn_type == "benefit_credit":
                t.cat_l1, t.cat_l2, t.cat_l3 = "Card Credits & Adjustments", "Card benefit credit", t.canonical_merchant
        elif body.txn_type == "card_fee":
            t.cat_l1, t.cat_l2, t.cat_l3 = card_taxonomy.current(*card_rules.FEE_CLASS["other fee"])
        else:
            title = body.txn_type.replace("_", " ").title()
            t.cat_l1, t.cat_l2, t.cat_l3 = card_taxonomy.EXCLUDED_L1, title, title
        t.category_source = "user"
    what = ([f"type {old_type} → {t.txn_type}"] if t.txn_type != old_type else []) + (["notes"] if t.notes != old_notes else [])
    if what:
        change_log.record(db, "card", "charge_edit", f"{t.date} {change_log.clip(t.canonical_merchant)} {change_log.money(-float(t.amount))}: {', '.join(what)}",
                          {"txn_id": t.id, "type": [old_type, t.txn_type], "notes_changed": t.notes != old_notes})
    db.commit()
    acct = db.get(CardAccount, t.card_account_id)
    return _txn_dict(t, acct.name if acct else "")


# ── Merchants ────────────────────────────────────────────────────────────────

@router.get("/merchants")
def list_merchants(q: str | None = None, db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT merchant_key, MAX(merchant_label), MAX(canonical_merchant), MAX(merchant_family),
               COUNT(*), COALESCE(SUM(-amount), 0), MAX(date)
          FROM card_transactions
         WHERE merchant_key <> '' AND txn_type IN ('purchase', 'refund')
         GROUP BY merchant_key
    """)).fetchall()
    cats = Counter()
    for key, l1, l2, l3, src in db.execute(text("""
        SELECT merchant_key, cat_l1, cat_l2, cat_l3, category_source FROM card_transactions
         WHERE merchant_key <> '' AND txn_type IN ('purchase', 'refund')
    """)).fetchall():
        cats[(key, l1, l2, l3, src)] += 1
    top: dict[str, tuple] = {}
    for (key, l1, l2, l3, src), n in cats.most_common():
        top.setdefault(key, (l1, l2, l3, src))
    cache = {c.merchant_key: c for c in db.query(MerchantCategory).all()}
    out = []
    needle = (q or "").strip().lower()
    for key, label, canonical, family, n, spend, last in rows:
        if needle and needle not in (canonical or "").lower() and needle not in (label or "").lower():
            continue
        l1, l2, l3, src = top.get(key, card_taxonomy.UNCATEGORIZED + ("unclassified",))
        c = cache.get(key)
        out.append({
            "merchant_key": key, "label": label, "canonical_merchant": canonical, "merchant_family": family,
            "count": n, "spend": round(float(spend), 2), "last_date": last.isoformat() if last else None,
            "cat_l1": l1, "cat_l2": l2, "cat_l3": l3, "category_source": src,
            "cache_source": c.source if c else None, "confidence": c.confidence if c else None,
            "subscription_hint": c.subscription_hint if c else None,
        })
    out.sort(key=lambda d: -d["spend"])
    return out


class MerchantEditBody(BaseModel):
    name: str                           # the merchant as shown now
    new_name: str
    family: str = ""
    cat_l1: str
    cat_l2: str
    cat_l3: str = ""
    overwrite_user: bool = False


@router.post("/merchant/edit")
def edit_merchant(body: MerchantEditBody, db: Session = Depends(get_db)):
    """The merchant page's edit: every row of the merchant, remembered for future imports."""
    rows = _merchant_rows(db, body.name)
    if not rows:
        raise HTTPException(status_code=404, detail="Merchant not found")
    cat = _category(body)
    name, family = _names(body.new_name, body.family)
    old_names = {r.canonical_merchant for r in rows}
    out = apply_merchant_edit(db, rows, name, family, cat, body.overwrite_user)
    _rules_follow(db, old_names, name)
    change_log.record(db, "card", "merchant_edit", f"{change_log.clip(body.name)} → {change_log.clip(name)}, {cat[0]} › {cat[1]} ({out.get('updated', 0)} charges)",
                      {"before": body.name, "name": name, "family": family, "category": list(cat), **{k: v for k, v in out.items() if isinstance(v, int)}})
    db.commit()
    return {**out, "name": name}


class AmountRuleBody(BaseModel):
    merchant: str = ""                 # one merchant ...
    scope_l1: str = ""                 # ... or every merchant in a category (L1, optionally L2)
    scope_l2: str = ""
    amount_min: float | None = None
    amount_max: float | None = None
    cat_l1: str = ""
    cat_l2: str = ""
    cat_l3: str = ""


def _rule_dict(r: CardAmountRule) -> dict:
    return {"id": r.id, "merchant": r.merchant or "", "scope_l1": r.scope_l1 or "", "scope_l2": r.scope_l2 or "",
            "amount_min": r.amount_min, "amount_max": r.amount_max,
            "cat_l1": r.cat_l1, "cat_l2": r.cat_l2, "cat_l3": r.cat_l3}


def _rule_spec(db: Session, body: AmountRuleBody, rule_id: int | None, need_category: bool) -> card_amount_rules.AmountRule:
    """Validate a rule (scope, range, no overlap in its scope) and return it unsaved."""
    merchant, s1, s2 = body.merchant.strip(), body.scope_l1.strip(), body.scope_l2.strip()
    lo, hi = body.amount_min, body.amount_max
    if s1:
        merchant = ""
        if s1 not in card_taxonomy.TREE or (s2 and not card_taxonomy.is_valid(s1, s2)):
            raise HTTPException(status_code=422, detail="Unknown category for the rule to cover")
    elif not merchant:
        raise HTTPException(status_code=422, detail="Which merchant or category is this rule for?")
    if lo is None and hi is None:
        raise HTTPException(status_code=422, detail="Give the rule a lower or upper amount (or both)")
    if (lo is not None and lo < 0) or (hi is not None and hi <= 0) or (lo is not None and hi is not None and lo >= hi):
        raise HTTPException(status_code=422, detail="The amount range is not valid")
    cat = _category(body) if need_category else ("", "", "")
    spec = card_amount_rules.AmountRule(rule_id or 0, merchant, lo, hi, *cat, s1, s2 if s1 else "")
    for r in card_amount_rules.load(db):
        if r.id != rule_id and r.same_scope(spec) and card_amount_rules.overlaps(lo, hi, r.amount_min, r.amount_max):
            raise HTTPException(status_code=422, detail="This range overlaps another rule for the same "
                                + ("category" if s1 else "merchant"))
    return spec


@router.get("/amount-rules")
def list_amount_rules(db: Session = Depends(get_db)):
    rules = card_amount_rules.load(db)
    return [{**_rule_dict(db.get(CardAmountRule, r.id)), **card_amount_rules.coverage(db, r, rules)} for r in rules]


@router.post("/amount-rules/preview")
def preview_amount_rule(body: AmountRuleBody, rule_id: int | None = None, db: Session = Depends(get_db)):
    """What a rule would cover, before it is saved (other rules in place)."""
    try:
        spec = _rule_spec(db, body, rule_id, need_category=False)
    except HTTPException as exc:
        return {"error": exc.detail}
    spec.id = rule_id or -1
    return card_amount_rules.coverage(db, spec, card_amount_rules.load(db))


@router.post("/amount-rules", status_code=201)
def add_amount_rule(body: AmountRuleBody, db: Session = Depends(get_db)):
    spec = _rule_spec(db, body, None, need_category=True)
    r = CardAmountRule(merchant=spec.merchant or None, scope_l1=spec.scope_l1 or None, scope_l2=spec.scope_l2 or None,
                       amount_min=spec.amount_min, amount_max=spec.amount_max,
                       cat_l1=spec.cat_l1, cat_l2=spec.cat_l2, cat_l3=spec.cat_l3)
    db.add(r)
    db.flush()
    changed = card_amount_rules.apply(db, {spec.merchant} if spec.merchant else None)
    change_log.record(db, "rule", "add", f"Added amount rule for {change_log.clip(spec.merchant or (spec.scope_l1 + (' › ' + spec.scope_l2 if spec.scope_l2 else '')))}: {spec.cat_l1} › {spec.cat_l2} ({changed} charges)",
                      {**_rule_dict(r), "changed": changed})
    db.commit()
    return {**_rule_dict(r), "changed": changed}


@router.put("/amount-rules/{rule_id}")
def update_amount_rule(rule_id: int, body: AmountRuleBody, db: Session = Depends(get_db)):
    r = db.get(CardAmountRule, rule_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Rule not found")
    spec = _rule_spec(db, body, rule_id, need_category=True)
    old_merchant = r.merchant or ""
    r.merchant, r.scope_l1, r.scope_l2 = spec.merchant or None, spec.scope_l1 or None, spec.scope_l2 or None
    r.amount_min, r.amount_max = spec.amount_min, spec.amount_max
    r.cat_l1, r.cat_l2, r.cat_l3 = spec.category
    db.flush()
    changed = card_amount_rules.apply(db, {spec.merchant, old_merchant})
    change_log.record(db, "rule", "edit", f"Edited amount rule for {change_log.clip(spec.merchant or (spec.scope_l1 + (' › ' + spec.scope_l2 if spec.scope_l2 else '')))}: {spec.cat_l1} › {spec.cat_l2} ({changed} charges)",
                      {**_rule_dict(r), "changed": changed})
    db.commit()
    return {**_rule_dict(r), "changed": changed}


@router.delete("/amount-rules/{rule_id}")
def delete_amount_rule(rule_id: int, db: Session = Depends(get_db)):
    r = db.get(CardAmountRule, rule_id)
    if r is None:
        return {"changed": 0}
    merchant = r.merchant or ""
    was = _rule_dict(r)
    db.delete(r)
    db.flush()
    changed = card_amount_rules.apply(db, {merchant})       # its charges go back to their own category
    change_log.record(db, "rule", "delete", f"Deleted amount rule for {change_log.clip(merchant or (was['scope_l1'] + (' › ' + was['scope_l2'] if was['scope_l2'] else '')))} ({changed} charges back)",
                      {**was, "changed": changed})
    db.commit()
    return {"changed": changed}


def _sorted_rules(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda d: -1e18 if d["amount_min"] is None else d["amount_min"])


def _rules_for_merchant(db: Session, name: str, rows: list[CardTransaction]) -> dict:
    """The merchant's own amount rules, and the category rules that reach it (read-only there)."""
    rules = card_amount_rules.load(db)
    own = [r for r in rules if not r.is_category_rule and r.merchant.lower() == name.lower()]
    base = card_amount_rules.base_lookup(db)
    cats = Counter(base(t)[:2] if t.category_source == card_amount_rules.SOURCE else (t.cat_l1, t.cat_l2)
                   for t in rows if t.canonical_merchant == name and t.category_source != "user")
    reach = [r for r in rules if any(r.covers_category(*c) for c in cats)]
    return {"amount_rules": _sorted_rules([{**_rule_dict(db.get(CardAmountRule, r.id)), **card_amount_rules.coverage(db, r, rules)}
                                          for r in own]),
            "category_rules": _sorted_rules([_rule_dict(db.get(CardAmountRule, r.id)) for r in reach])}


@router.get("/merchant")
def merchant_page(name: str, accounts: str | None = None, date_from: DateType | None = Query(None, alias="from"),
                  date_to: DateType | None = Query(None, alias="to"), db: Session = Depends(get_db)):
    """Everything about one merchant: totals for the selection, its spellings and how each was categorized,
    recurring series, and its transactions (newest first)."""
    all_rows = _merchant_rows(db, name)
    if not all_rows:
        raise HTTPException(status_code=404, detail="Merchant not found")
    ld = _load(db, accounts, date_from, date_to)
    detail = card_analytics.merchant_detail(ld.rows, name)
    keys = sorted({t.merchant_key for t in all_rows if t.merchant_key})
    cache = {c.merchant_key: c for c in db.query(MerchantCategory).filter(MerchantCategory.merchant_key.in_(keys)).all()} if keys else {}
    spellings = []
    for k in keys:
        ks = [t for t in all_rows if t.merchant_key == k]
        c = cache.get(k)
        spellings.append({
            "merchant_key": k, "label": ks[0].merchant_label, "count": len(ks),
            "example": max(ks, key=lambda t: t.date).raw_description,
            "source": c.source if c else None, "confidence": c.confidence if c else None,
            # What the AI was sent stays on record after the user overrides its answer.
            "sent_label": c.sanitized_label if c and c.sanitized_label else None,
            "model": c.model if c and c.sanitized_label else None,
            "updated_at": c.updated_at.isoformat() if c and c.updated_at else None,
        })
    txns = sorted((r for r in ld.rows if r.canonical_merchant == name), key=lambda r: (r.date, r.id), reverse=True)
    family = next((t.merchant_family for t in all_rows if t.canonical_merchant == name), name)
    return {
        **detail, "family": family, "spellings": spellings, **{k: v for k, v in _scope_info(db, all_rows).items()
                                                                if k in ("count", "user_set")},
        "recurring": [it for it in ld.recurring_items if it["merchant"] == name],
        **_rules_for_merchant(db, name, all_rows),
        "transactions": [_txn_dict(ld.txns_by_id[r.id], r.card, r.recurring_kind, r.ai_confidence) for r in txns[:300]],
        "transactions_total": len(txns),
    }


# ── AI ───────────────────────────────────────────────────────────────────────

@router.get("/ai/status")
def ai_status(db: Session = Depends(get_db)):
    return {
        "enabled": card_ai.area_enabled("cards"), "reason": card_ai.area_off_reason("cards"), "model": card_ai.model_name(),
        "categorize_model": card_ai.model_name(card_ai.TASK_CATEGORIZE),
        "pending_merchants": len(card_ai.pending_merchants(db)) if card_ai.area_enabled("cards") else
        len(card_ai.pending_merchants(db, include_stale=False)),
        "problem": card_ai.key_problem(db) if card_ai.area_enabled("cards") else None,
        **card_ai.status(),
    }


@router.post("/categorize")
def categorize(background: BackgroundTasks, db: Session = Depends(get_db)):
    reason = card_ai.area_off_reason("cards")
    if reason:
        raise HTTPException(status_code=409, detail=reason)
    pending = len(card_ai.pending_merchants(db))
    running = card_ai.status()["running"]
    if pending and not running:
        # A manual run always tries, even after a key failure: it is how a fixed key is confirmed.
        background.add_task(card_ai.run_categorization, SessionLocal, True)
    reason = None if pending and not running else (
        "A run is already in progress." if running else "Nothing is waiting for the AI.")
    return {"pending_merchants": pending, "started": bool(pending) and not running, "reason": reason}


# ── Recurring ────────────────────────────────────────────────────────────────

class VerdictBody(BaseModel):
    merchant: str
    card_account_id: int | None = None  # the card this verdict is for; null = every card
    status: str | None = None          # 'confirmed' | 'dismissed' | null to clear


@router.post("/recurring/status")
def set_recurring_status(body: VerdictBody, db: Session = Depends(get_db)):
    card_id = body.card_account_id or card_recurring.ALL_CARDS
    if body.status not in (None, "confirmed", "dismissed"):
        raise HTTPException(status_code=422, detail="status must be confirmed, dismissed or null")
    v = _verdict_row(db, body.merchant, card_id)
    v.status = body.status
    _drop_if_empty(db, v)
    change_log.record(db, "recurring", "verdict", f"{change_log.clip(body.merchant)}: {body.status or 'left to detection'}" + ("" if card_id == card_recurring.ALL_CARDS else " on one card"),
                      {"merchant": body.merchant, "card_account_id": card_id, "status": body.status})
    db.commit()
    return {"merchant": body.merchant, "card_account_id": card_id, "status": body.status}


class EndedBody(BaseModel):
    merchant: str
    card_account_id: int
    ended: bool                        # true: ended as of its latest charge; false: still running


@router.post("/recurring/ended")
def set_recurring_ended(body: EndedBody, db: Session = Depends(get_db)):
    """Mark one card's series ended now (no waiting for missed charges), or undo that."""
    last = db.query(func.max(CardTransaction.date)).filter(
        CardTransaction.card_account_id == body.card_account_id,
        CardTransaction.canonical_merchant == body.merchant,
        CardTransaction.txn_type == "purchase").scalar()
    if last is None:
        raise HTTPException(status_code=404, detail="No charges from that merchant on that card")
    v = _verdict_row(db, body.merchant, body.card_account_id)
    v.ended_through = last if body.ended else None
    _drop_if_empty(db, v)
    change_log.record(db, "recurring", "ended", f"{change_log.clip(body.merchant)}: {'ended as of ' + last.isoformat() if body.ended else 'running again'}",
                      {"merchant": body.merchant, "card_account_id": body.card_account_id, "ended_through": last.isoformat() if body.ended else None})
    db.commit()
    return {"merchant": body.merchant, "card_account_id": body.card_account_id,
            "ended_through": last.isoformat() if body.ended else None}


class ManualBody(BaseModel):
    name: str
    merchant: str
    cadence: str = "monthly"
    kind: str = "subscription"
    card_account_id: int | None = None
    amount_min: float | None = None
    amount_max: float | None = None


def _manual_fields(body: ManualBody, db: Session) -> dict:
    name, merchant = body.name.strip()[:80], body.merchant.strip()[:120]
    if not name or not merchant:
        raise HTTPException(status_code=422, detail="Give it a name and a merchant")
    if body.cadence not in card_recurring.CADENCE_DAYS:
        raise HTTPException(status_code=422, detail="Unknown cadence")
    if body.kind not in card_recurring.MANUAL_KINDS:
        raise HTTPException(status_code=422, detail="Kind must be subscription, recurring bill or recurring service")
    lo, hi = body.amount_min, body.amount_max
    if (lo is not None and lo < 0) or (hi is not None and hi < 0) or (lo is not None and hi is not None and lo > hi):
        raise HTTPException(status_code=422, detail="The amount range is not valid")
    if body.card_account_id is not None and db.get(CardAccount, body.card_account_id) is None:
        raise HTTPException(status_code=422, detail="Unknown card")
    return {"name": name, "merchant": merchant, "cadence": body.cadence, "kind": body.kind,
            "card_account_id": body.card_account_id, "amount_min": lo, "amount_max": hi}


def _manual_dict(m: CardManualRecurring) -> dict:
    return {"id": m.id, "name": m.name, "merchant": m.merchant, "cadence": m.cadence, "kind": m.kind,
            "card_account_id": m.card_account_id, "amount_min": m.amount_min, "amount_max": m.amount_max,
            "ended_through": m.ended_through.isoformat() if m.ended_through else None}


@router.get("/recurring/manual")
def list_manual_recurring(db: Session = Depends(get_db)):
    return [_manual_dict(m) for m in db.query(CardManualRecurring).order_by(CardManualRecurring.name).all()]


@router.post("/recurring/manual", status_code=201)
def add_manual_recurring(body: ManualBody, db: Session = Depends(get_db)):
    """A recurring charge defined by hand: it claims the merchant's charges in its amount range."""
    m = CardManualRecurring(**_manual_fields(body, db))
    db.add(m)
    change_log.record(db, "recurring", "add", f"Added recurring charge {change_log.clip(m.name)} ({change_log.clip(m.merchant)}, {m.cadence})",
                      {"name": m.name, "merchant": m.merchant, "cadence": m.cadence, "kind": m.kind, "amount_min": m.amount_min, "amount_max": m.amount_max})
    db.commit()
    return _manual_dict(m)


@router.put("/recurring/manual/{manual_id}")
def update_manual_recurring(manual_id: int, body: ManualBody, db: Session = Depends(get_db)):
    m = db.get(CardManualRecurring, manual_id)
    if m is None:
        raise HTTPException(status_code=404, detail="Not found")
    was = m.name
    for k, v in _manual_fields(body, db).items():
        setattr(m, k, v)
    change_log.record(db, "recurring", "edit", f"Edited recurring charge {change_log.clip(was)}" + (f" → {change_log.clip(m.name)}" if m.name != was else ""),
                      {"id": m.id, "name": m.name, "merchant": m.merchant, "cadence": m.cadence, "kind": m.kind, "amount_min": m.amount_min, "amount_max": m.amount_max})
    db.commit()
    return _manual_dict(m)


@router.delete("/recurring/manual/{manual_id}", status_code=204)
def delete_manual_recurring(manual_id: int, db: Session = Depends(get_db)):
    m = db.get(CardManualRecurring, manual_id)
    if m is not None:
        change_log.record(db, "recurring", "delete", f"Deleted recurring charge {change_log.clip(m.name)}", {"id": m.id, "name": m.name, "merchant": m.merchant})
        db.delete(m)
        db.commit()


class ManualEndedBody(BaseModel):
    ended: bool


@router.post("/recurring/manual/{manual_id}/ended")
def set_manual_ended(manual_id: int, body: ManualEndedBody, db: Session = Depends(get_db)):
    m = db.get(CardManualRecurring, manual_id)
    if m is None:
        raise HTTPException(status_code=404, detail="Not found")
    spec = card_recurring.ManualSeries(id=m.id, name=m.name, merchant=m.merchant, cadence=m.cadence, kind=m.kind,
                                       card_account_id=m.card_account_id, amount_min=m.amount_min, amount_max=m.amount_max)
    q = db.query(CardTransaction).filter(CardTransaction.txn_type == "purchase")
    last = max((t.date for t in q.all() if card_recurring.claims(spec, card_recurring.Charge(
        id=t.id, card_account_id=t.card_account_id, card="", date=t.date, spend=-float(t.amount),
        canonical_merchant=t.canonical_merchant, cat_l1="", cat_l2="", cat_l3="",
        merchant_family=t.merchant_family or ""))), default=None)
    if body.ended and last is None:
        raise HTTPException(status_code=409, detail="No charge matches it yet")
    m.ended_through = last if body.ended else None
    change_log.record(db, "recurring", "ended", f"{change_log.clip(m.name)}: {'ended as of ' + last.isoformat() if body.ended else 'running again'}",
                      {"id": m.id, "ended_through": last.isoformat() if body.ended else None})
    db.commit()
    return _manual_dict(m)


def _verdict_row(db: Session, merchant: str, card_id: int) -> CardRecurringVerdict:
    v = db.get(CardRecurringVerdict, (merchant, card_id))
    if v is None:
        v = CardRecurringVerdict(canonical_merchant=merchant, card_account_id=card_id)
        db.add(v)
    return v


def _drop_if_empty(db: Session, v: CardRecurringVerdict) -> None:
    if v.status is None and v.ended_through is None:
        if v in db.new:
            db.expunge(v)
        else:
            db.delete(v)


# ── Analytics ────────────────────────────────────────────────────────────────

def _filters(accounts: str | None = None, date_from: DateType | None = Query(None, alias="from"),
             date_to: DateType | None = Query(None, alias="to"), common_period: bool = False,
             large: float = 500.0):
    return {"accounts": accounts, "date_from": date_from, "date_to": date_to,
            "common_period": common_period, "large": large}


def _loaded(f: dict, db: Session) -> Loaded:
    return _load(db, f["accounts"], f["date_from"], f["date_to"], f["common_period"])


@router.get("/analytics/summary")
def analytics_summary(f: dict = Depends(_filters), db: Session = Depends(get_db)):
    ld = _loaded(f, db)
    out = card_analytics.summary(ld.rows, ld.coverage, ld.card_names, ld.common_rows, f["large"])
    out["recurring"] = ld.recurring_totals
    out["common_period_applied"] = f["common_period"]
    return out


@router.get("/analytics/categories")
def analytics_categories(level: str = "l1", f: dict = Depends(_filters), db: Session = Depends(get_db)):
    return card_analytics.categories(_loaded(f, db).rows, level)


@router.get("/analytics/monthly")
def analytics_monthly(f: dict = Depends(_filters), db: Session = Depends(get_db)):
    ld = _loaded(f, db)
    return card_analytics.monthly(ld.rows, ld.coverage, f["large"])


@router.get("/analytics/merchants")
def analytics_merchants(f: dict = Depends(_filters), db: Session = Depends(get_db)):
    return card_analytics.merchants(_loaded(f, db).rows)


def review_result(db: Session, loaded: Loaded, large: float = 500.0) -> dict:
    """The Review tab's list for the loaded rows: what was ticked off, and every card's name (the
    co-brand flags need them all, whatever the filter). Shared with the Dashboard's attention panel."""
    reviewed = {i: (at.isoformat(), set(filter(None, (codes or "").split(","))))
                for i, at, codes in db.query(CardTransaction.id, CardTransaction.reviewed_at, CardTransaction.reviewed_reasons)
                .filter(CardTransaction.reviewed_at.isnot(None))}
    names = [n for (n,) in db.query(CardAccount.name)]
    return card_analytics.review(loaded.rows, large, reviewed, card_names=names)


@router.get("/analytics/review")
def analytics_review(f: dict = Depends(_filters), db: Session = Depends(get_db)):
    return review_result(db, _loaded(f, db), f["large"])


class ReviewMark(BaseModel):
    txn_id: int
    codes: list[str] = Field([], max_length=20)


class ReviewBody(BaseModel):
    items: list[ReviewMark] = Field(max_length=10_000)     # "Mark all reviewed" sends the whole open list
    reviewed: bool = True


@router.post("/review")
def mark_reviewed(body: ReviewBody, db: Session = Depends(get_db)):
    """Check items off the Review tab (or put them back). Each remembers the reasons it had."""
    ids = {m.txn_id: m for m in body.items}
    rows = db.query(CardTransaction).filter(CardTransaction.id.in_(ids)).all() if ids else []
    for t in rows:
        t.reviewed_at = func.now() if body.reviewed else None
        t.reviewed_reasons = ",".join(ids[t.id].codes) if body.reviewed else None
    db.commit()
    return {"updated": len(rows)}


@router.get("/analytics/cards")
def analytics_cards(f: dict = Depends(_filters), db: Session = Depends(get_db)):
    ld = _loaded(f, db)
    return card_analytics.cards(ld.rows, ld.common_rows, ld.card_names, f["large"])


@router.get("/analytics/category")
def analytics_category(l1: str, f: dict = Depends(_filters), db: Session = Depends(get_db)):
    out = card_analytics.category_detail(_loaded(f, db).rows, l1)
    rules = card_amount_rules.load(db)
    out["amount_rules"] = _sorted_rules([{**_rule_dict(db.get(CardAmountRule, r.id)), **card_amount_rules.coverage(db, r, rules)}
                                         for r in rules if r.scope_l1 == l1])
    return out


@router.get("/analytics/recurring")
def analytics_recurring(accounts: str | None = None, include_habitual: bool = True,
                        db: Session = Depends(get_db)):
    ld = _load(db, accounts, None, None)
    items = [it for it in ld.recurring_items if include_habitual or it["kind"] != "habitual"]
    names = {a.id: a.name for a in db.query(CardAccount).all()}
    dismissed = [{"merchant": v.canonical_merchant, "card_account_id": v.card_account_id,
                  "card": names.get(v.card_account_id, "every card")}
                 for v in db.query(CardRecurringVerdict).filter(CardRecurringVerdict.status == "dismissed").all()]
    return {"items": items, **ld.recurring_totals,
            "dismissed": sorted(dismissed, key=lambda d: (d["merchant"], d["card"]))}


# ── Insights ─────────────────────────────────────────────────────────────────

class InsightsBody(BaseModel):
    accounts: str | None = None
    date_from: DateType | None = None
    date_to: DateType | None = None
    common_period: bool = False
    large: float = 500.0


def _alias_names(r: CardInsightReport, db: Session) -> dict[str, str]:
    """{"Card A": current card name} for a report. Reports from before aliases were stored
    are mapped the way they were made: the report's cards in name order."""
    names = {a.id: a.name for a in db.query(CardAccount).all()}
    if r.card_aliases_json:
        ids = json.loads(r.card_aliases_json)
    else:
        cards = {int(i): {"name": names.get(int(i), "")} for i in r.account_ids.split(",") if i.strip()}
        ids = {alias: cid for cid, alias in card_analytics.card_aliases(cards).items()}
    return {alias: names[cid] for alias, cid in ids.items() if cid in names}


def _report_dict(r: CardInsightReport, db: Session, payload: dict | None = None) -> dict:
    """The report with real card names restored; ``payload`` (what was sent) stays aliased."""
    return {
        "id": r.id, "model": r.model, "account_ids": r.account_ids,
        "date_from": r.date_from.isoformat() if r.date_from else None,
        "date_to": r.date_to.isoformat() if r.date_to else None, "common_period": r.common_period,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "report": card_analytics.unalias_all(json.loads(r.report_json), _alias_names(r, db)), "payload": payload,
    }


@router.post("/insights")
def create_insights(body: InsightsBody, db: Session = Depends(get_db)):
    ld = _load(db, body.accounts, body.date_from, body.date_to, body.common_period)
    if not [r for r in ld.rows if r.is_spend]:
        raise HTTPException(status_code=422, detail="No spending in the selected period")
    payload = card_analytics.insights_payload(ld.rows, ld.coverage, ld.card_meta, ld.recurring_items,
                                              ld.recurring_totals, body.common_period, body.large)
    try:
        report, log_row = card_ai.generate_insights(db, payload)
    except card_ai.AiError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    rec = CardInsightReport(
        request_log_id=log_row.id, account_ids=",".join(str(i) for i in sorted(ld.card_names)),
        date_from=body.date_from, date_to=body.date_to, common_period=body.common_period,
        model=log_row.model, report_json=report.model_dump_json(),
        card_aliases_json=json.dumps({alias: cid for cid, alias in card_analytics.card_aliases(ld.card_meta).items()}),
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return _report_dict(rec, db, payload)


@router.get("/insights")
def list_insights(limit: int = 20, db: Session = Depends(get_db)):
    rows = db.query(CardInsightReport).order_by(CardInsightReport.id.desc()).limit(max(1, min(limit, 100))).all()
    out = []
    for r in rows:
        d = _report_dict(r, db)
        out.append({**d, "report": None, "headline": d["report"].get("headline")})
    return out


@router.get("/insights/{report_id}")
def get_insights(report_id: int, db: Session = Depends(get_db)):
    r = db.get(CardInsightReport, report_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Report not found")
    log_row = db.get(AiRequestLog, r.request_log_id) if r.request_log_id else None
    return _report_dict(r, db, json.loads(log_row.payload_json) if log_row else None)


# ── Export ───────────────────────────────────────────────────────────────────

EXPORT_COLUMNS = ["txn_id", "card", "txn_date", "post_date", "raw_description", "amount", "txn_type",
                  "txn_subtype", "canonical_merchant", "merchant_family", "cat_l1", "cat_l2", "cat_l3",
                  "class_source", "is_spend", "recurring_kind", "foreign_flag", "installment_flag",
                  "issuer_category", "notes"]


spreadsheet_safe = exports.spreadsheet_safe       # shared with the other exports; kept importable from here


@router.get("/export.csv")
def export_csv(accounts: str | None = None, date_from: DateType | None = Query(None, alias="from"),
               date_to: DateType | None = Query(None, alias="to"), db: Session = Depends(get_db)):
    """Normalized transactions in the card-spend-analyzer layout (spend positive)."""
    ld = _load(db, accounts, date_from, date_to)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(EXPORT_COLUMNS)
    for i, r in enumerate(ld.rows, 1):
        t = ld.txns_by_id[r.id]
        s = spreadsheet_safe            # text cells only: the amount column stays a number
        w.writerow([f"T{i:05d}", s(r.card), r.date.isoformat(), t.post_date.isoformat() if t.post_date else "",
                    s(r.raw_description), f"{r.spend:.2f}", s(r.txn_type), s(r.txn_subtype), s(r.canonical_merchant),
                    s(r.merchant_family), s(r.cat_l1), s(r.cat_l2), s(r.cat_l3), t.category_source, r.txn_type in
                    card_rules.SPEND_TYPES, s(r.recurring_kind), r.foreign_flag, t.installment_flag,
                    s(t.issuer_category or ""), s(t.notes or "")])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="card_transactions.csv"'})


@router.get("/export-year.csv")
def export_year_csv(year: int = Query(..., ge=2000, le=2100), db: Session = Depends(get_db)):
    """The year-end summary: card spending by category and subcategory for a calendar year (every
    card, archived ones included), net / gross / refunds / purchases and a column per month, with
    giving, taxes, medical, insurance and tuition marked; plus spending paid from the cash accounts by
    payee role (card payments and transfers left out, as in Cash Flow Analysis)."""
    from routers import cashflow
    start, end = DateType(year, 1, 1), DateType(year, 12, 31)
    ids = ",".join(str(a.id) for a in db.query(CardAccount.id).all())
    loaded = _load(db, ids, start, end) if ids else None
    rows, _spend, payments, _card_rows = cashflow._inputs(db, start, end)
    entries, _left = cash_flow.tag(rows, [], payments, cashflow._roles(db), start, end)
    summary = card_analytics.year_summary(loaded.rows if loaded else [], entries, year)
    return exports.csv_response(exports.YEAR_SUMMARY_COLUMNS, exports.year_summary_rows(summary), f"card-summary-{year}")

