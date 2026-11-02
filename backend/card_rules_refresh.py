"""
Bring stored card charges in line with the merchant rules after the rules change
(the one-off ``card_rules_generic_v1`` migration in main.py).

A charge filed by a rule (``category_source = 'rule'``, or an amount rule whose recorded
category before it came from a rule) is classified again from its stored description:

* a rule still recognizes it: the stored category follows that rule's current answer
  (a flight filed under "Other airlines" moves to its airline's own detail);
* no rule recognizes it any more: the charge keeps its category, and that category is
  remembered for its merchant (``merchant_categories``, source ``user``), so later imports
  of the same merchant keep it as well.

Charges the user set, AI answers and merchant-cache answers are left alone. ``plan`` is
pure (no database); ``run`` reads and writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import card_classifier
import card_rules
import card_taxonomy

Cat = tuple[str, str, str]


@dataclass
class StoredRow:
    id: int
    raw_description: str
    issuer_category: str | None
    merchant_key: str
    merchant_label: str
    canonical_merchant: str
    merchant_family: str
    cat: Cat
    category_source: str
    pre_rule_category: str | None
    txn_type: str


@dataclass
class Plan:
    updates: dict[int, Cat] = field(default_factory=dict)            # rule-filed rows: new category
    pre_rule_updates: dict[int, str] = field(default_factory=dict)   # amount-rule rows: new "l1|l2|l3|rule"
    remember: dict[str, tuple[str, str, str, str, str]] = field(default_factory=dict)  # key -> name, family, l1-3
    merchants: set[str] = field(default_factory=set)                  # canonical names whose rows changed


def rule_answer(row: StoredRow) -> Cat | None:
    """The category the current rules give this charge, or None when no rule recognizes it."""
    desc_u = card_classifier.upper_desc(row.raw_description)
    r = card_rules.classify_merchant(desc_u, row.issuer_category or "", fallback_name=row.merchant_label or None)
    if r["class_source"] not in card_classifier.CONFIDENT_RULE_SOURCES:
        return None
    return card_taxonomy.current(r["l1"], r["l2"], r["l3"])


def _rule_category(row: StoredRow) -> Cat | None:
    """The category a rule gave this row: its own, or the one recorded before an amount rule."""
    if row.category_source == "rule":
        return row.cat
    if row.category_source == "amount_rule" and row.pre_rule_category:
        parts = row.pre_rule_category.split("|")
        if len(parts) == 4 and parts[3] == "rule":
            return parts[0], parts[1], parts[2]
    return None


def plan(rows: list[StoredRow], user_keys: set[str]) -> Plan:
    """``user_keys``: merchant keys the user already set (those answers win; nothing to remember)."""
    out = Plan()
    for row in rows:
        if row.txn_type not in card_classifier.MERCHANT_TYPES or row.txn_type == "benefit_credit":
            continue
        current = _rule_category(row)
        if current is None:
            continue
        new = rule_answer(row)
        if new is None:
            key = row.merchant_key
            if key and key not in user_keys and key not in out.remember:
                out.remember[key] = (row.canonical_merchant, row.merchant_family or row.canonical_merchant, *current)
            continue
        if new == current:
            continue
        if row.category_source == "rule":
            out.updates[row.id] = new
        else:
            out.pre_rule_updates[row.id] = "|".join((*new, "rule"))
        out.merchants.add(row.canonical_merchant)
    return out


def run(db) -> dict:
    """Apply the plan to the database (the caller commits). Returns counts for the log."""
    import card_amount_rules
    from models import CardTransaction, MerchantCategory

    stored = db.query(CardTransaction).filter(CardTransaction.category_source.in_(("rule", "amount_rule"))).all()
    user_keys = {k for (k,) in db.query(MerchantCategory.merchant_key).filter(MerchantCategory.source == "user")}
    rows = [StoredRow(t.id, t.raw_description, t.issuer_category, t.merchant_key or "", t.merchant_label or "",
                      t.canonical_merchant, t.merchant_family, (t.cat_l1, t.cat_l2, t.cat_l3),
                      t.category_source, t.pre_rule_category, t.txn_type) for t in stored]
    p = plan(rows, user_keys)
    by_id = {t.id: t for t in stored}
    for i, (l1, l2, l3) in p.updates.items():
        by_id[i].cat_l1, by_id[i].cat_l2, by_id[i].cat_l3 = l1, l2, l3
    for i, value in p.pre_rule_updates.items():
        by_id[i].pre_rule_category = value
    for key, (name, family, l1, l2, l3) in p.remember.items():
        c = db.get(MerchantCategory, key)
        if c is None:
            c = MerchantCategory(merchant_key=key)
            db.add(c)
        c.canonical_merchant, c.merchant_family = name, family
        c.cat_l1, c.cat_l2, c.cat_l3 = l1, l2, l3
        c.source = "user"
    db.flush()
    if p.merchants:
        card_amount_rules.apply(db, p.merchants)
    return {"updated": len(p.updates), "pre_rule_updated": len(p.pre_rule_updates), "remembered": len(p.remember)}
