"""
Amount rules: categories by charge size, for merchants that sell more than one
kind of thing (a gas station: fuel at the pump, snacks inside).

A rule is (scope, amount range, category). The scope is either one merchant
("Circle K") or a category (Transportation › Fuel: every gas station). Rules in
one scope may not overlap. A range includes its lower bound and excludes its
upper one ("under $20" is max 20); either bound may be open.

A charge (purchase or refund, by size) takes the category of the first rule
that covers it: its merchant's rules first, then rules for its merchant's own
category. Category rules match the category the charge has without any amount
rule (``base``), so a snack charge a rule moved to "Convenience store" still
counts as fuel. Charges outside every rule keep that category; charges the user
changed on their own (category_source 'user') are never touched.

``apply`` re-derives this for stored charges. It runs after a rule changes, after
an import, after an AI run and after a merchant edit, so new and renamed charges
follow the rules too.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

SOURCE = "amount_rule"

Base = Callable[[object], tuple[str, str, str, str]]   # row -> its category and source without amount rules


@dataclass
class AmountRule:
    id: int
    merchant: str                  # merchant scope ("" for a category rule)
    amount_min: float | None
    amount_max: float | None
    cat_l1: str
    cat_l2: str
    cat_l3: str
    scope_l1: str = ""             # category scope: L1, and L2 or "" for all of the L1
    scope_l2: str = ""

    @property
    def category(self) -> tuple[str, str, str]:
        return self.cat_l1, self.cat_l2, self.cat_l3

    @property
    def is_category_rule(self) -> bool:
        return bool(self.scope_l1)

    def same_scope(self, other: "AmountRule") -> bool:
        if self.is_category_rule or other.is_category_rule:
            return (self.scope_l1, self.scope_l2) == (other.scope_l1, other.scope_l2)
        return self.merchant.lower() == other.merchant.lower()

    def covers_category(self, l1: str, l2: str) -> bool:
        return self.is_category_rule and l1 == self.scope_l1 and (not self.scope_l2 or l2 == self.scope_l2)


def in_range(amount: float, lo: float | None, hi: float | None) -> bool:
    return (lo is None or amount >= lo - 0.005) and (hi is None or amount < hi - 0.005)


def overlaps(a_lo: float | None, a_hi: float | None, b_lo: float | None, b_hi: float | None) -> bool:
    inf = float("inf")
    a0, a1 = (-inf if a_lo is None else a_lo), (inf if a_hi is None else a_hi)
    b0, b1 = (-inf if b_lo is None else b_lo), (inf if b_hi is None else b_hi)
    return a0 < b1 and b0 < a1


def rule_for(rules: list[AmountRule], merchant: str, amount: float,
             base_cat: tuple[str, str] | None = None) -> AmountRule | None:
    """The rule that sets this charge's category: the merchant's own rules, then category rules."""
    m, a = (merchant or "").lower(), abs(amount)
    hit = next((r for r in rules if not r.is_category_rule and r.merchant.lower() == m
                and in_range(a, r.amount_min, r.amount_max)), None)
    if hit is None and base_cat is not None:
        hit = next((r for r in rules if r.covers_category(*base_cat) and in_range(a, r.amount_min, r.amount_max)), None)
    return hit


def _base_cat(t, base: Base) -> tuple[str, str]:
    if t.category_source == SOURCE:
        l1, l2, _l3, _src = base(t)
        return l1, l2
    return t.cat_l1, t.cat_l2


def decide(rows, rules: list[AmountRule], base: Base) -> list[tuple[object, AmountRule | None, tuple[str, str, str], str]]:
    """(row, rule or None, category, source) for every row a rule covers or once covered (user rows skipped)."""
    out = []
    for t in rows:
        if t.category_source == "user":
            continue
        r = rule_for(rules, t.canonical_merchant, float(t.amount), _base_cat(t, base))
        if r is not None:
            out.append((t, r, r.category, SOURCE))
        elif t.category_source == SOURCE:
            l1, l2, l3, source = base(t)
            out.append((t, None, (l1, l2, l3), source))
    return out


def plan(rows, rules: list[AmountRule], base: Base) -> list[tuple[object, tuple[str, str, str], str]]:
    """The category changes the rules call for: (row, (l1, l2, l3), source)."""
    return [(t, cat, src) for t, _r, cat, src in decide(rows, rules, base)
            if (t.cat_l1, t.cat_l2, t.cat_l3, t.category_source) != (*cat, src)]


# ── Database ─────────────────────────────────────────────────────────────────

def from_row(r) -> AmountRule:
    return AmountRule(r.id, r.merchant or "", r.amount_min, r.amount_max, r.cat_l1, r.cat_l2, r.cat_l3,
                      r.scope_l1 or "", r.scope_l2 or "")


def load(db) -> list[AmountRule]:
    from models import CardAmountRule
    return [from_row(r) for r in db.query(CardAmountRule).order_by(CardAmountRule.id).all()]


def _candidates(db, rules: list[AmountRule], merchants: set[str]):
    """Rows any rule might cover, plus every row a rule set (it may need to go back)."""
    from sqlalchemy import and_, func, or_

    from models import CardTransaction

    names = {r.merchant.lower() for r in rules if not r.is_category_rule} | {m.lower() for m in merchants if m}
    conds = [CardTransaction.category_source == SOURCE]
    if names:
        conds.append(func.lower(CardTransaction.canonical_merchant).in_(names))
    for r in rules:
        if r.is_category_rule:
            c = CardTransaction.cat_l1 == r.scope_l1
            conds.append(and_(c, CardTransaction.cat_l2 == r.scope_l2) if r.scope_l2 else c)
    return db.query(CardTransaction).filter(
        or_(*conds), CardTransaction.txn_type.in_(["purchase", "refund"]),
    ).all()


def stored_base(t) -> tuple[str, str, str, str] | None:
    """The category a row had before an amount rule changed it, if recorded."""
    raw = getattr(t, "pre_rule_category", None)
    parts = raw.split("|") if raw else []
    return tuple(parts) if len(parts) == 4 else None


def base_lookup(db) -> Base:
    """A row's category without amount rules: what it had before a rule changed it, else its
    merchant's cached category, else the local rules."""
    import card_classifier
    import card_rules
    import card_taxonomy
    from models import MerchantCategory

    cache: dict = {}

    def base(t) -> tuple[str, str, str, str]:
        kept = stored_base(t)
        if kept:
            return kept
        if t.merchant_key not in cache:
            cache[t.merchant_key] = db.get(MerchantCategory, t.merchant_key) if t.merchant_key else None
        c = cache[t.merchant_key]
        if c is not None:
            return c.cat_l1, c.cat_l2, c.cat_l3, ("merchant" if c.source == "user" else c.source)
        r = card_rules.classify_merchant(card_classifier.upper_desc(t.raw_description), t.issuer_category or "",
                                         fallback_name=t.merchant_label or None)
        return (*card_taxonomy.current(r["l1"], r["l2"], r["l3"]), "rule")
    return base


def apply(db, merchants: set[str] | None = None) -> int:
    """
    Bring stored charges in line with the rules. ``merchants``: merchants whose
    rules were just removed or renamed, so their charges are looked at too.
    Returns the number of charges changed; the caller commits.
    """
    rules = load(db)
    rows = _candidates(db, rules, merchants or set())
    changes = plan(rows, rules, base_lookup(db))
    for t, (l1, l2, l3), source in changes:
        if source == SOURCE and t.category_source != SOURCE:
            t.pre_rule_category = "|".join((t.cat_l1, t.cat_l2, t.cat_l3, t.category_source))
        elif source != SOURCE:
            t.pre_rule_category = None
        t.cat_l1, t.cat_l2, t.cat_l3, t.category_source = l1, l2, l3, source
    return len(changes)


def coverage(db, rule: AmountRule, others: list[AmountRule]) -> dict:
    """What ``rule`` covers, with every other rule in place: for previews and the rule tables."""
    rules = [r for r in others if r.id != rule.id]
    rules = [r for r in rules if not r.is_category_rule] + ([rule] if not rule.is_category_rule else []) + \
            [r for r in rules if r.is_category_rule] + ([rule] if rule.is_category_rule else [])
    base = base_lookup(db)
    rows = _candidates(db, rules, {rule.merchant} if rule.merchant else set())
    mine = [t for t, r, _c, _s in decide(rows, rules, base) if r is rule]
    kept = [t for t in rows if t.category_source == "user"
            and in_range(abs(float(t.amount)), rule.amount_min, rule.amount_max)
            and (rule.covers_category(t.cat_l1, t.cat_l2) if rule.is_category_rule
                 else t.canonical_merchant.lower() == rule.merchant.lower())]
    in_scope = [t for t in rows if t.category_source != "user"
                and (rule.covers_category(*_base_cat(t, base)) if rule.is_category_rule
                     else t.canonical_merchant.lower() == rule.merchant.lower())]
    return {"matches": len(mine), "total": round(sum(-float(t.amount) for t in mine), 2),
            "kept_user_overrides": len(kept), "of": len(in_scope),
            "merchants": len({t.canonical_merchant for t in mine}),
            "examples": [round(-float(t.amount), 2) for t in sorted(mine, key=lambda t: t.date, reverse=True)[:8]]}
