"""
Card statement import: de-duplication, typing, sanitizing and classifying.

De-duplication reuses the register's import_dedup planner (counts, not
existence, so repeated identical charges survive), with one extra tier in
front: an issuer reference number already on file (some issuers supply a stable
18-digit reference) skips the row outright, because descriptions can change
between downloads while references do not.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

import card_classifier
import card_privacy
import card_rules
from import_profile import CardParseResult, CardRow
import import_dedup
import settings_store
from import_dedup import amount_key, date_bounds, line_key, normalize_description, plan_bank_import
from models import CardAccount, CardImport, CardTransaction, MerchantCategory

_US = {"", "UNITED STATES", "US", "USA", "UNITED STATES OF AMERICA", "NAN"}
_ISO3_TAIL = re.compile(
    r"\s(?:ITA|FRA|GBR|DEU|ESP|MEX|JPN|NLD|PRT|GRC|CHE|AUT|IRL|ISL|DNK|SWE|NOR|FIN|BEL|CZE|HRV|"
    r"AUS|NZL|CRI|BHS|JAM|DOM|ARG|BRA|CHL|PER|COL|ZAF|THA|SGP|HKG|KOR|VNM|IDN|TUR|ARE|ISR|MAR|EGY|POL|HUN|CAN)$")
_FOREIGN_RE = re.compile(r"FOREIGN SPEND AMOUNT:\s*([\d,\.]+)\s*([A-Z]{3})", re.I)


@dataclass
class PlannedRow:
    row: CardRow
    merchant_key: str
    merchant_label: str
    processor: str
    cls: card_classifier.TxnClass
    foreign: bool
    installment: bool


@dataclass
class ImportPlan:
    to_import: list[PlannedRow]
    skipped_duplicates: int
    preview: list[dict] = field(default_factory=list)

    @property
    def needs_ai(self) -> int:
        return len({p.merchant_key for p in self.to_import if p.cls.needs_ai})


def account_names(acct: CardAccount) -> list[str]:
    names = [n.strip() for n in (acct.cardholder_names or "").splitlines() if n.strip()]
    names.extend(settings_store.redact_words())
    return names


def load_cache(db: Session) -> card_classifier.MerchantIndex:
    """The merchant cache as a truncation-tolerant index (see MerchantIndex)."""
    return card_classifier.MerchantIndex({
        c.merchant_key: card_classifier.CacheEntry(
            c.canonical_merchant, c.merchant_family, c.cat_l1, c.cat_l2, c.cat_l3, c.source,
            c.subscription_hint, c.confidence)
        for c in db.query(MerchantCategory).all()
    })


def _existing_coverage(db: Session, account_id: int, lo, hi) -> tuple[Counter, Counter]:
    rows = db.execute(text("""
        SELECT date, amount, raw_description, reference FROM card_transactions
         WHERE card_account_id = :aid AND date BETWEEN :lo AND :hi
    """), {"aid": account_id, "lo": lo, "hi": hi}).fetchall()
    lines = Counter(line_key(r.date, r.amount, r.raw_description) for r in rows)
    refs = Counter(r.reference for r in rows if r.reference)
    return lines, refs


def card_numbers(value: str | None) -> list[str]:
    """The last digits of each card on an account (``CardAccount.last4``: comma, space or newline separated)."""
    out = []
    for part in re.split(r"[,;\s/]+", value or ""):
        d = re.sub(r"\D", "", part)
        if d and d not in out:
            out.append(d)
    return out


def remember_numbers(acct: CardAccount, numbers: list[str]) -> list[str]:
    """Add card numbers seen in an imported file to the account; returns the ones that were new."""
    known = card_numbers(acct.last4)
    new = [n for n in numbers if n and not any(k[-4:] == n[-4:] for k in known)]
    if new:
        acct.last4 = ", ".join(known + new)
    return new


def _sanitize_row(row: CardRow, acct: CardAccount, names: list[str]) -> card_privacy.Sanitized:
    return card_privacy.sanitize(row.raw_description, card_privacy.Redaction(
        names=names + ([row.cardholder] if row.cardholder else []),
        cities=[row.city] if row.city else [],
        account_digits=card_numbers(acct.last4) + ([row.account] if row.account else []),
    ))


def _is_foreign(row: CardRow, ttype: str) -> bool:
    if row.country.strip().upper() not in _US:
        return True
    if _FOREIGN_RE.search(f"{row.extended} {row.raw_description}"):
        return True
    return ttype in ("purchase", "refund") and bool(_ISO3_TAIL.search(row.raw_description.upper().rstrip()))


def plan_import(db: Session, acct: CardAccount, parsed: CardParseResult) -> ImportPlan:
    rows = parsed.rows
    lo, hi = date_bounds(rows)
    lines, refs = _existing_coverage(db, acct.id, lo, hi)

    # Tier 0: issuer reference numbers already on file.
    remaining_refs = Counter(refs)
    candidates: list[CardRow] = []
    skipped = 0
    for r in rows:
        if r.reference and remaining_refs[r.reference] > 0:
            remaining_refs[r.reference] -= 1
            skipped += 1
            continue
        candidates.append(r)
    new_rows, dupes = plan_bank_import(candidates, lines, Counter()) if candidates else ([], 0)
    skipped += dupes
    new_ids = {id(r) for r in new_rows}

    cache = load_cache(db)
    names = account_names(acct)
    planned: list[PlannedRow] = []
    for r in new_rows:
        san = _sanitize_row(r, acct, names)
        cls = card_classifier.classify_transaction(
            r.raw_description, r.amount, r.issuer_type, r.issuer_category, acct.name,
            san.merchant_key, san.label, cache)
        planned.append(PlannedRow(r, san.merchant_key, san.label, san.processor, cls,
                                  _is_foreign(r, cls.txn_type),
                                  bool(card_rules.INSTALLMENT_RE.search(r.raw_description.upper()))))
    _infer_foreign_from_fees(planned)

    preview = []
    for r in rows[:12]:
        san = _sanitize_row(r, acct, names)
        cls = card_classifier.classify_transaction(
            r.raw_description, r.amount, r.issuer_type, r.issuer_category, acct.name,
            san.merchant_key, san.label, cache)
        preview.append({
            "date": r.date.isoformat(), "description": r.raw_description, "amount": r.amount,
            "txn_type": cls.txn_type, "category": " › ".join(x for x in (cls.cat_l1, cls.cat_l2) if x),
            "merchant": cls.canonical_merchant, "already_on_file": id(r) not in new_ids,
        })
    return ImportPlan(planned, skipped, preview)


def _infer_foreign_from_fees(planned: list[PlannedRow]) -> None:
    """A foreign-transaction-fee line within a day, sized 1-3.5% of a purchase, marks that purchase foreign."""
    for f in planned:
        if f.cls.txn_type != "card_fee" or f.cls.txn_subtype != "foreign transaction fee":
            continue
        fee = -f.row.amount
        best, best_err = None, None
        for p in planned:
            spend = -p.row.amount
            if p.cls.txn_type != "purchase" or spend <= 0 or abs((p.row.date - f.row.date).days) > 1:
                continue
            ratio = fee / spend
            if 0.009 <= ratio <= 0.036:
                err = abs(ratio - 0.03)
                if best_err is None or err < best_err:
                    best, best_err = p, err
        if best is not None:
            best.foreign = True


def apply_import(db: Session, acct: CardAccount, parsed: CardParseResult, plan: ImportPlan,
                 filename: str | None) -> CardImport:
    dates = [r.date for r in parsed.rows]
    imp = CardImport(
        card_account_id=acct.id, filename=filename, issuer_format=parsed.issuer_format,
        sign_convention=parsed.sign_convention, sign_reason=parsed.sign_reason,
        rows_parsed=len(parsed.rows), rows_imported=len(plan.to_import),
        rows_skipped=plan.skipped_duplicates, pending_dropped=parsed.pending_dropped,
        date_min=parsed.statement_start or min(dates), date_max=parsed.statement_end or max(dates),
    )
    db.add(imp)
    db.flush()
    for p in plan.to_import:
        r, c = p.row, p.cls
        db.add(CardTransaction(
            card_account_id=acct.id, import_id=imp.id, date=r.date, post_date=r.post_date,
            raw_description=r.raw_description, merchant_key=p.merchant_key, merchant_label=p.merchant_label,
            processor=p.processor or None, canonical_merchant=c.canonical_merchant[:200],
            merchant_family=c.merchant_family[:200], amount=r.amount, txn_type=c.txn_type,
            txn_subtype=c.txn_subtype or None, is_spend=c.is_spend, cat_l1=c.cat_l1, cat_l2=c.cat_l2,
            cat_l3=c.cat_l3, category_source=c.category_source, issuer_category=r.issuer_category or None,
            issuer_type=r.issuer_type or None, cardholder=r.cardholder or None, reference=r.reference or None,
            foreign_flag=p.foreign, installment_flag=p.installment, subscription_hint=r.subscription_hint,
        ))
    if not acct.issuer and parsed.institution:
        acct.issuer = parsed.institution
    return imp


def coverage_by_card(db: Session, account_ids: list[int]) -> dict[int, tuple]:
    """(first, last) date each card's data covers: statement periods and transactions combined."""
    if not account_ids:
        return {}
    out: dict[int, list] = {}
    for aid, lo, hi in db.execute(text("""
        SELECT card_account_id, MIN(date_min), MAX(date_max) FROM card_imports
         WHERE card_account_id = ANY(:ids) GROUP BY card_account_id
    """), {"ids": account_ids}).fetchall():
        if lo and hi:
            out[aid] = [lo, hi]
    for aid, lo, hi in db.execute(text("""
        SELECT card_account_id, MIN(date), MAX(date) FROM card_transactions
         WHERE card_account_id = ANY(:ids) GROUP BY card_account_id
    """), {"ids": account_ids}).fetchall():
        if aid in out:
            out[aid] = [min(out[aid][0], lo), max(out[aid][1], hi)]
        else:
            out[aid] = [lo, hi]
    return {k: (v[0], v[1]) for k, v in out.items()}


def clip_coverage(cov: dict[int, tuple], lo, hi) -> dict[int, tuple]:
    out = {}
    for k, (a, b) in cov.items():
        a2 = max(a, lo) if lo else a
        b2 = min(b, hi) if hi else b
        if a2 <= b2:
            out[k] = (a2, b2)
    return out



# ── Wrong-card check ─────────────────────────────────────────────────────────
#
# Some issuers' exports carry no card number or cardholder, so the card
# picked in the import dialog is the only attribution. The rule itself is
# shared with cash accounts: import_dedup.best_other_account.

def overlap(file_rows, existing: list[tuple], window: tuple | None) -> tuple[int, int]:
    return import_dedup.overlap([(r.date, r.amount, r.raw_description) for r in file_rows], existing, window)


is_likely_match = import_dedup.is_likely_match


def other_card_match(db: Session, acct: CardAccount, parsed: CardParseResult) -> dict | None:
    """The other card this file most likely belongs to, or None."""
    rows = parsed.rows
    if not rows:
        return None
    lo, hi = date_bounds(rows)
    cards = {a.id: a.name for a in db.query(CardAccount).all()}
    existing: dict[int, list[tuple]] = {}
    for cid, d, amt, desc in db.execute(text("""
        SELECT card_account_id, date, amount, raw_description FROM card_transactions
         WHERE date BETWEEN :lo AND :hi
    """), {"lo": lo, "hi": hi}).fetchall():
        existing.setdefault(cid, []).append((d, amt, desc))
    m = import_dedup.best_other_account([(r.date, r.amount, r.raw_description) for r in rows], acct.id, cards,
                                        existing, coverage_by_card(db, list(cards)))
    return m and {"card_account_id": m["account_id"], "card": m["account"], "matched": m["matched"],
                  "compared": m["compared"], "matched_on_chosen_card": m["matched_on_chosen"]}


def other_card_message(match: dict, chosen: str) -> str:
    return import_dedup.other_account_message(
        {"account": match["card"], "matched": match["matched"], "compared": match["compared"],
         "matched_on_chosen": match["matched_on_chosen_card"]}, chosen)
