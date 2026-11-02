"""
Spending taxonomy for credit-card transactions (L1 > L2 > L3).

The tree is derived from card_rules.py at import time, so every category a
rule can produce is pickable in the UI and listed in the AI prompt, and the
two can never drift apart. L1 and L2 are fixed: analytics and charts group on
them, so a new value would split a series. L3 is detail and may grow; user
edits and AI answers can add L3 values.
"""

from __future__ import annotations

from dataclasses import dataclass

import card_rules

L1_ORDER = [
    "Travel", "Food & Beverage", "Shopping", "Home", "Transportation", "Health & Personal",
    "Entertainment", "Bills & Utilities", "Digital & Media", "Education / Family", "Financial / Investment",
    "Pets", "Giving", "Government & Taxes", "Services", "Transfers / P2P",
    "Card Credits & Adjustments", "Uncategorized",
]

# Non-spend rows (payments, interest, carry-ins ...) live under this L1. It is
# assigned by transaction type, never picked by a user or returned by the AI.
EXCLUDED_L1 = "Excluded (non-spend)"
UNCATEGORIZED = ("Uncategorized", "Needs review", "Needs review")


def _build_tree() -> dict[str, dict[str, list[str]]]:
    tree: dict[str, dict[str, list[str]]] = {l1: {} for l1 in L1_ORDER}

    def add(l1: str, l2: str, l3: str) -> None:
        l3s = tree.setdefault(l1, {}).setdefault(l2, [])
        if l3 and l3 not in l3s:
            l3s.append(l3)

    for _rx, _canon, _fam, l1, l2, l3 in card_rules.ALL_MERCHANT_RULES:
        add(l1, l2, l3)
    for _rx, l1, l2, l3 in card_rules.WEAK_KEYWORDS:
        add(l1, l2, l3)
    for _key, l1, l2, l3 in card_rules.ISSUER_CATEGORY_MAP:
        add(l1, l2, l3)
    for l1, l2, l3 in card_rules.FEE_CLASS.values():
        add(l1, l2, l3)
    add("Card Credits & Adjustments", "Card benefit credit", "Card benefit credit")
    add(*UNCATEGORIZED)
    return tree


TREE = _build_tree()


# ── Your own subcategories and details ──────────────────────────────────────
#
# Added in Settings (or by applying an Assistant proposal) and stored in the
# custom_categories table; L1 stays fixed. A subcategory carries the behaviour
# that built-in ones get from name lists elsewhere: how fixed it is (analytics),
# its role in recurring detection, its rewards bucket, and whether its name may
# be sent to the AI. A detail inherits all of that from its subcategory.

FIXEDNESS_CHOICES = {"fixed": "Relatively fixed", "essential": "Essential, variable", "discretionary": "Discretionary"}
ROLE_CHOICES = ("none", "bill", "subscription", "habit")
REWARD_CHOICES = ("general", "dining", "grocery", "gas", "travel", "streaming", "drugstore", "home_improvement",
                  "utilities", "phone_plan", "transit")
PRIVATE_L2 = "(private subcategory)"
PRIVATE_L3 = "(private)"
CHOOSABLE_L1 = [l1 for l1 in L1_ORDER if l1 not in ("Uncategorized", "Card Credits & Adjustments")]


@dataclass
class Custom:
    id: int
    l1: str
    l2: str
    l3: str = ""                     # "" for a subcategory, else a detail under l2
    fixedness: str = "discretionary"
    role: str = "none"
    rewards: str = "general"
    ai_visible: bool = True


_custom: list[Custom] = []


def set_custom(items: list[Custom]) -> None:
    global _custom
    _custom = list(items)


def custom_items() -> list[Custom]:
    return list(_custom)


def refresh(db) -> None:
    """Reload your subcategories, details and renamed built-in names (startup and after changes)."""
    from models import CategoryRename, CustomCategory
    set_custom([Custom(c.id, c.l1, c.l2, c.l3 or "", c.fixedness, c.role, c.rewards, c.ai_visible)
                for c in db.query(CustomCategory).order_by(CustomCategory.id).all()])
    set_renames({(r.l1, r.l2, r.l3 or ""): r.new_name for r in db.query(CategoryRename).all()})


# ── Renamed built-in names ──────────────────────────────────────────────────
#
# A built-in subcategory or detail can be renamed (category_renames, keyed by its
# original names; l3 "" renames the subcategory). Stored rows, the merchant cache
# and rules carry the new name, and the rules' answers are renamed as they are
# assigned (current). Code that keys behaviour on built-in names (rewards buckets,
# bill and subscription detection, travel and food analytics) compares the
# original names (base), so a rename changes the label, never the behaviour.

_renames: dict[tuple[str, str, str], str] = {}
_inv_l2: dict[tuple[str, str], str] = {}            # (l1, new l2) -> original l2
_inv_l2_by_name: dict[str, str] = {}                 # new l2 -> original l2 (first across categories)
_inv_l3: dict[tuple[str, str, str], str] = {}        # (l1, original l2, new l3) -> original l3


def set_renames(items: dict[tuple[str, str, str], str]) -> None:
    global _renames, _inv_l2, _inv_l2_by_name, _inv_l3
    _renames = dict(items)
    _inv_l2 = {(l1, new): l2 for (l1, l2, l3), new in _renames.items() if not l3}
    _inv_l2_by_name = {}
    for (l1, l2, l3), new in _renames.items():
        if not l3:
            _inv_l2_by_name.setdefault(new, l2)
    _inv_l3 = {(l1, l2, new): l3 for (l1, l2, l3), new in _renames.items() if l3}


def renames() -> dict[tuple[str, str, str], str]:
    return dict(_renames)


def current(l1: str, l2: str, l3: str = "") -> tuple[str, str, str]:
    """Built-in names as they are now called. Names that aren't renamed pass through."""
    n2 = _renames.get((l1, l2, ""), l2)
    if l3 == l2 and (l1, l2, l3) not in _renames:
        return l1, n2, n2                   # a detail that mirrors its subcategory follows it
    return l1, n2, _renames.get((l1, l2, l3), l3) if l3 else l3


def base(l1: str, l2: str, l3: str = "") -> tuple[str, str, str]:
    """The original built-in names behind current ones (for behaviour keyed on built-in names)."""
    o2 = _inv_l2.get((l1, l2), l2)
    if l3 and l3 == l2:
        o3 = _inv_l3.get((l1, o2, l3), o2)
    else:
        o3 = _inv_l3.get((l1, o2, l3), l3) if l3 else l3
    return l1, o2, o3


def base_l2(l2: str) -> str:
    """The original name of a subcategory known only by name (FIXEDNESS and the like)."""
    return _inv_l2_by_name.get(l2, l2)


def _current_tree() -> dict[str, dict[str, list[str]]]:
    out: dict[str, dict[str, list[str]]] = {}
    for l1, l2s in TREE.items():
        out[l1] = {}
        for l2, l3s in l2s.items():
            n2 = current(l1, l2)[1]
            out[l1][n2] = [current(l1, l2, x)[2] for x in l3s]
    return out


def custom_subcategory(l1: str, l2: str) -> Custom | None:
    return next((c for c in _custom if not c.l3 and c.l1 == l1 and c.l2 == l2), None)


def custom_by_l2(l2: str) -> Custom | None:
    """Your subcategory with this name (names are unique within a category; first match across them)."""
    return next((c for c in _custom if not c.l3 and c.l2 == l2), None)


def is_builtin(l1: str, l2: str, l3: str = "") -> bool:
    """True for a built-in subcategory (or detail), by its current name."""
    _, o2, o3 = base(l1, l2, l3)
    if l1 not in TREE or o2 not in TREE[l1] or current(l1, o2)[1] != l2:
        return False                         # unknown, or an old name that was renamed
    if not l3:
        return True
    return o3 in TREE[l1][o2] and current(l1, o2, o3)[2] == l3


def tree() -> dict[str, dict[str, list[str]]]:
    """Built-in categories (by their current names) plus yours."""
    out = _current_tree()
    for c in _custom:
        if not c.l3:
            out.setdefault(c.l1, {}).setdefault(c.l2, [])
    for c in _custom:
        if c.l3 and c.l1 in out and c.l2 in out[c.l1] and c.l3 not in out[c.l1][c.l2]:
            out[c.l1][c.l2].append(c.l3)
    return out


def is_valid(l1: str, l2: str) -> bool:
    """True when (l1, l2) is a pickable category pair (built-in or yours)."""
    return is_builtin(l1, l2) or custom_subcategory(l1, l2) is not None


def is_private(l1: str, l2: str, l3: str = "") -> bool:
    sub = custom_subcategory(l1, l2)
    if sub is not None and not sub.ai_visible:
        return True
    return bool(l3) and any(c.l3 == l3 and c.l1 == l1 and c.l2 == l2 and not c.ai_visible for c in _custom)


def public_l2(l1: str, l2: str) -> str:
    """The subcategory name as it may be sent to the AI."""
    return PRIVATE_L2 if is_private(l1, l2) else l2


def public_l3(l1: str, l2: str, l3: str) -> str:
    return PRIVATE_L3 if is_private(l1, l2, l3) else l3


def as_list(for_ai: bool = False) -> list[dict]:
    """The tree in display order, for the API and the AI prompt (private names left out for the AI)."""
    t = tree()
    out = []
    for l1 in L1_ORDER:
        l2s = []
        for l2, l3s in t.get(l1, {}).items():
            if for_ai and is_private(l1, l2):
                continue
            l3_out = [x for x in l3s if not (for_ai and is_private(l1, l2, x))]
            entry = {"name": l2, "l3": l3_out}
            if not for_ai:
                entry["custom"] = not is_builtin(l1, l2)
                entry["custom_l3"] = [x for x in l3s if not is_builtin(l1, l2, x)]
            l2s.append(entry)
        out.append({"l1": l1, "l2": l2s})
    return out


# The old "Subscriptions / Telecom" L1 was split (see card_rules). Stored
# categories are rewritten once at startup by main.py with this mapping.
LEGACY_SUB_L1 = "Subscriptions / Telecom"
_SHOPPING_MEMBERSHIPS = {"Amazon Prime", "Walmart+", "Uber One", "DashPass"}


def split_legacy_category(l1: str, l2: str, l3: str | None) -> str | None:
    """The new L1 for a category stored before the split, or None when unchanged."""
    if l1 == "Home" and l2 == "Utilities":
        return "Bills & Utilities"
    if l1 != LEGACY_SUB_L1:
        return None
    if l2 in ("Mobile phone", "Internet", "TV", "Utilities"):
        return "Bills & Utilities"
    if l2 == "Memberships":
        return "Shopping" if l3 in _SHOPPING_MEMBERSHIPS else "Services"
    return "Digital & Media"
