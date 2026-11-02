"""
Assistant proposals: changes Claude may suggest, never make.

Each propose_* tool checks its suggestion against Flow's data (the category
exists or is proposed earlier in the same answer, the merchant exists, an amount
range does not overlap a rule in the same scope) and turns it into a proposal
card: a title, the lines the user reads (with counts the app computed), and the
exact request the browser sends to Flow's normal endpoint when the user presses
Apply. Those endpoints validate again; the Assistant has no write path of its own.

Claude gets back only a short confirmation ("shown to the user, not applied") or
an error to correct.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import ask_data
import card_amount_rules
import card_recurring
import card_taxonomy
import import_profile as ip

S = {"type": "string"}
N = {"type": "number"}
B = {"type": "boolean"}


def _schema(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def _enum(values) -> dict:
    return {"type": "string", "enum": list(values)}


REASON = {"type": "string", "description": "One short sentence the user sees: why this change."}

TOOLS = [
    {"name": "propose_new_subcategory",      "description": "Propose a new subcategory under one of Flow's categories, when nothing in the taxonomy fits "
                    "or the user asks for one. Set its behaviour: counts_as (how fixed the spending is), detection "
                    "(what its charges usually are, for subscription and bill detection), rewards (card bonus "
                    "bucket), private (true if the name is personal; it is then never sent to the AI).",
     "input_schema": _schema({
         "category": _enum(card_taxonomy.CHOOSABLE_L1), "name": S,
         "counts_as": _enum(card_taxonomy.FIXEDNESS_CHOICES), "detection": _enum(card_taxonomy.ROLE_CHOICES),
         "rewards": _enum(card_taxonomy.REWARD_CHOICES), "private": B, "reason": REASON})},
    {"name": "propose_merchant_category",      "description": "Propose a merchant's category (and optionally a new name) for all its charges and future imports. "
                    "merchant is the name as lookups show it; new_name empty keeps the name; detail empty uses the "
                    "subcategory as the detail. The category may be one proposed earlier in this answer.",
     "input_schema": _schema({"merchant": S, "new_name": S, "category": S, "subcategory": S, "detail": S,
                              "reason": REASON})},
    {"name": "propose_new_detail",      "description": "Propose a new detail (the level under a subcategory) when none of the subcategory's details fits "
                    "or the user asks for one. Details are entries in the category list, not free text: a merchant or "
                    "amount-rule proposal may only use a detail that exists (see the categories lookup) or one "
                    "proposed earlier in this answer.",
     "input_schema": _schema({"category": S, "subcategory": S, "name": S, "reason": REASON})},
    {"name": "propose_rename_category",      "description": "Propose renaming a subcategory (detail empty) or a detail, built-in or the user's own. One card "
                    "renames it everywhere: its charges, merchants, rules and future imports follow, and a built-in "
                    "keeps its behaviour (rewards bucket, bill and subscription detection). Top-level categories can't "
                    "be renamed. Use the names exactly as the categories lookup shows them.",
     "input_schema": _schema({"category": S, "subcategory": S, "detail": S, "new_name": S, "reason": REASON})},
    {"name": "propose_move_category",      "description": "Propose moving everything filed under one subcategory (from_detail empty) or one detail to another "
                    "subcategory or detail: every charge, its merchants (remembered, so future imports follow) and "
                    "amount-rule targets, in one card. Use it instead of one merchant proposal per merchant. to_detail "
                    "empty files them under the destination subcategory itself; otherwise it must be an existing "
                    "detail or one proposed earlier in this answer.",
     "input_schema": _schema({"from_category": S, "from_subcategory": S, "from_detail": S,
                              "to_category": S, "to_subcategory": S, "to_detail": S, "reason": REASON})},
    {"name": "propose_payee_role",      "description": "Propose what a cash-register payee's money is for, so the Cash Flow diagram sorts it: income, "
                    "card_payment, transfer (between the household's own accounts), spending, debt (loan payments), "
                    "saving (investing or savings) or ignore (leave out). payee is the name as cash lookups show it. "
                    "category (spending only) is a top-level category, or empty to show the payee by name.",
     "input_schema": _schema({"payee": S, "role": _enum(["income", "card_payment", "transfer", "spending", "debt",
                                                          "saving", "ignore"]),
                              "category": S, "reason": REASON})},
    {"name": "propose_amount_rule",      "description": "Propose a category for charges in an amount range, at one merchant (applies_to 'merchant') or "
                    "at every merchant in a subcategory (applies_to 'subcategory', e.g. gas stations: snacks under "
                    "$20). amount_from includes its value, amount_under excludes it; 0 means no bound. Unused scope "
                    "fields are empty strings.",
     "input_schema": _schema({"applies_to": _enum(["merchant", "subcategory"]), "merchant": S,
                              "scope_category": S, "scope_subcategory": S, "amount_from": N, "amount_under": N,
                              "category": S, "subcategory": S, "detail": S, "reason": REASON})},
    {"name": "propose_recurring",      "description": "Propose tracking a recurring charge Flow did not separate on its own, such as a subscription "
                    "billed under a shopping merchant's name. merchant is the merchant or parent brand; "
                    "amount_min / amount_max bound the charge (0 means no bound).",
     "input_schema": _schema({"name": S, "merchant": S, "amount_min": N, "amount_max": N,
                              "cadence": _enum(card_recurring.CADENCE_DAYS), "kind": _enum(card_recurring.MANUAL_KINDS),
                              "reason": REASON})},
    {"name": "propose_import_profile",      "description": "Propose an import format for the statement file the user attached (ATTACHED FILE in their "
                    "message): kind (cash = bank statements or a ledger, card, balance), a short name, the institution "
                    "if the headers make it plain (else empty), and columns as role -> list of 0-based column "
                    "indexes, e.g. {\"date\": [0], \"description\": [2], \"amount\": [3]}. Roles: date, post_date, "
                    "description (several columns allowed), memo, amount, debit (money out), credit (money in), "
                    "direction (a column saying in or out), running_balance, check_number, status, reconciled; cards "
                    "also card_number, cardholder, issuer_category, issuer_type, reference, extended, statement_as, "
                    "city_state, country; balance files account_ref, value, as_of. amount_mode single needs amount "
                    "and a sign (how money out is written); debit_credit needs debit and credit (sign none); "
                    "amount_plus_direction needs amount and direction (sign none). The user checks it against the "
                    "file in Flow's format editor before saving.",
     "input_schema": _schema({"kind": _enum(ip.KINDS), "name": S, "institution": S, "columns": {"type": "object"},
                              "amount_mode": _enum(ip.AMOUNT_MODES),
                              "sign": _enum(["negative_is_out", "positive_is_out", "none"]),
                              "date_format": _enum(ip.DATE_FORMATS), "decimal": _enum([".", ","]),
                              "skip_footer_rows": N, "reason": REASON})},
]
TOOL_NAMES = {t["name"] for t in TOOLS}


CADENCE_WORDS = {"weekly": "week", "biweekly": "2 weeks", "monthly": "month", "bimonthly": "2 months",
                 "quarterly": "quarter", "semiannual": "6 months", "annual": "year"}


class ProposalError(Exception):
    """Something Claude should correct (shown to it as a tool error)."""


@dataclass
class State:
    """Proposals made while answering one question."""
    proposals: list[dict] = field(default_factory=list)
    new_subcategories: set[tuple[str, str]] = field(default_factory=set)
    new_details: set[tuple[str, str, str]] = field(default_factory=set)
    attachment: dict | None = None      # ask_files.checked_attachment output for this question


def describe(name: str) -> str:
    return {"propose_new_subcategory": "Preparing a new subcategory",
            "propose_merchant_category": "Preparing a merchant change",
            "propose_amount_rule": "Preparing an amount rule",
            "propose_recurring": "Preparing a recurring charge", "propose_new_detail": "Preparing a new detail",
            "propose_rename_category": "Preparing a rename",
            "propose_move_category": "Preparing a move", "propose_payee_role": "Preparing a payee role",
            "propose_import_profile": "Preparing an import format"}.get(name, "Preparing a proposal")


def _valid(state: State, l1: str, l2: str) -> bool:
    return card_taxonomy.is_valid(l1, l2) or (l1, l2) in state.new_subcategories


def _details(state: State, data, l1: str, l2: str) -> set[str]:
    """The details a proposal may use under l1 › l2: the category list's, ones already on charges, ones proposed."""
    out = set(card_taxonomy.tree().get(l1, {}).get(l2, []))
    out |= {r.cat_l3 for r in data.rows if r.cat_l1 == l1 and r.cat_l2 == l2 and r.cat_l3}
    out |= {d for (a, b, d) in state.new_details if (a, b) == (l1, l2)}
    return out | {l2}


def _category(state: State, a: dict, k1="category", k2="subcategory", k3="detail", data=None) -> tuple[str, str, str]:
    l1, l2, l3 = a.get(k1, "").strip(), a.get(k2, "").strip(), a.get(k3, "").strip()
    if l1 == card_taxonomy.EXCLUDED_L1 or not _valid(state, l1, l2):
        raise ProposalError(f"{l1} › {l2} is not a category. Use one from the taxonomy, or propose the "
                            "new subcategory first.")
    if l3 and data is not None and l3 not in _details(state, data, l1, l2):
        known = sorted(_details(state, data, l1, l2) - {l2})
        raise ProposalError(f"{l2} has no detail {l3!r}. Use one of {known or 'none yet'}, leave it empty, "
                            "or call propose_new_detail first.")
    return l1, l2, (l3[:80] or l2)


def _name(v: str) -> str:
    name = (v or "").strip()
    if not name or len(name) > 60 or any(ch in name for ch in "|›>"):
        raise ProposalError("The name needs 1 to 60 characters and no | › or >.")
    return name


def _under(r, l1: str, l2: str, l3: str) -> bool:
    return r.cat_l1 == l1 and r.cat_l2 == l2 and (not l3 or r.cat_l3 == l3)


def _label(l1: str, l2: str, l3: str = "") -> str:
    return f"{l1} › {l2}" + (f" › {l3}" if l3 and l3 != l2 else "")


def _merchant(data: ask_data.AskData, name: str) -> tuple[str, str, list]:
    """(canonical name, parent brand, purchase/refund rows) for a merchant as lookups show it."""
    n = (name or "").strip().lower()
    if not n or n == ask_data.WITHHELD:
        raise ProposalError("Name a merchant as the lookups show it; withheld names cannot be changed here.")
    rows = [r for r in data.rows if r.txn_type in ("purchase", "refund")
            and (r.canonical_merchant.lower() == n or data.safe(r.canonical_merchant).lower() == n)]
    if not rows:
        raise ProposalError(f"No merchant called {name!r}. Use find_card_transactions or spending_breakdown "
                            "by merchant to get the exact name.")
    return rows[0].canonical_merchant, rows[0].merchant_family or rows[0].canonical_merchant, rows


def _bound(v) -> float | None:
    v = float(v or 0)
    if v < 0:
        raise ProposalError("Amounts cannot be negative.")
    return v or None


def _money(v: float) -> str:
    return f"${v:,.2f}"


def _range(lo: float | None, hi: float | None, upper_inclusive: bool = False) -> str:
    if lo is None and hi is None:
        return "any amount"
    if lo is None:
        return f"up to {_money(hi)}" if upper_inclusive else f"under {_money(hi)}"
    if hi is None:
        return f"{_money(lo)} and over"
    return f"{_money(lo)} to {_money(hi)}" if upper_inclusive else f"{_money(lo)} to under {_money(hi)}"


def checked_args(name: str, args: dict) -> dict:
    """
    The proposal tools aren't strict (strict schemas count toward the API's grammar-size limit, and
    every proposal is checked here and again by its endpoint), so check the input against the tool's
    own schema: every field present, strings as strings, numbers as numbers, and enum values matched
    case-insensitively to their canonical spelling.
    """
    schema = next(t["input_schema"] for t in TOOLS if t["name"] == name)
    out = {}
    for key, spec in schema["properties"].items():
        v = args.get(key)
        if "enum" in spec:
            match = next((e for e in spec["enum"] if isinstance(v, str) and e.lower() == v.strip().lower()), None)
            if match is None:
                raise ProposalError(f"{key} must be one of: {', '.join(spec['enum'])}.")
            out[key] = match
        elif spec.get("type") == "number":
            try:
                out[key] = float(v or 0)
            except (TypeError, ValueError):
                raise ProposalError(f"{key} must be a number (0 for none).")
        elif spec.get("type") == "boolean":
            out[key] = v if isinstance(v, bool) else str(v).strip().lower() in ("true", "yes", "1")
        elif spec.get("type") == "object":
            if not isinstance(v, dict):
                raise ProposalError(f"{key} must be an object.")
            out[key] = v
        else:
            if v is not None and not isinstance(v, (str, int, float)):
                raise ProposalError(f"{key} must be text.")
            out[key] = "" if v is None else str(v)
    return out


def run(name: str, args: dict, data: ask_data.AskData, state: State) -> tuple[str, dict]:
    """Check one proposal. Returns (what Claude is told, the proposal card for the user)."""
    if name not in TOOL_NAMES or not isinstance(args, dict):
        raise ProposalError(f"Unknown tool {name!r}")
    args = checked_args(name, args)
    p = {"propose_new_subcategory": _new_subcategory, "propose_merchant_category": _merchant_category,
         "propose_amount_rule": _amount_rule, "propose_recurring": _recurring, "propose_new_detail": _new_detail,
         "propose_rename_category": _rename_category, "propose_move_category": _move_category,
         "propose_payee_role": _payee_role, "propose_import_profile": _import_profile}[name](args, data, state)
    p["id"] = len(state.proposals) + 1
    p["reason"] = (args.get("reason") or "").strip()[:200]
    state.proposals.append(p)
    told = {"proposal": p["id"], "shown_to_user": True, "applied": False, "summary": p["title"],
            "details": p["lines"],
            "note": "Nothing has changed yet: the user applies or dismisses it. Do not say it is done."}
    return json.dumps(told), p


def _new_subcategory(a: dict, data, state: State) -> dict:
    l1, name = a["category"], (a.get("name") or "").strip()
    if not name or len(name) > 60 or any(ch in name for ch in "|›>"):
        raise ProposalError("The name needs 1 to 60 characters and no | › or >.")
    if _valid(state, l1, name):
        raise ProposalError(f"{l1} already has {name}; use it instead.")
    state.new_subcategories.add((l1, name))
    fixed, role = a["counts_as"], a["detection"]
    private = bool(a.get("private"))
    lines = [f"Counts as: {card_taxonomy.FIXEDNESS_CHOICES[fixed].lower()}",
             f"Detection: {dict(none='neither a bill nor a subscription', bill='a bill', subscription='a subscription', habit='an everyday habit')[role]}",
             f"Rewards bucket: {a['rewards'].replace('_', ' ')}",
             "Name kept private from the AI" if private else "Name can be sent to the AI"]
    return {"type": "new_subcategory", "title": f"New subcategory: {l1} › {name}", "lines": lines,
            "apply": {"method": "POST", "url": "/api/cards/categories",
                      "body": {"l1": l1, "l2": name, "fixedness": fixed, "role": role, "rewards": a["rewards"],
                               "ai_visible": not private}},
            "open": {"kind": "settings"}}


def _merchant_category(a: dict, data, state: State) -> dict:
    canonical, family, rows = _merchant(data, a["merchant"])
    l1, l2, l3 = _category(state, a, data=data)
    new_name = (a.get("new_name") or "").strip()[:120] or canonical
    total = sum(-r.amount for r in rows)
    lines = [f"Category: {l1} › {l2}" + (f" › {l3}" if l3 != l2 else ""),
             f"Applies to its {len(rows)} charge{'s' if len(rows) != 1 else ''} ({_money(total)}) and future imports"]
    if new_name != canonical:
        lines.insert(0, f"Rename to: {new_name}")
    return {"type": "merchant_category", "title": f"{canonical}: {l2}" + (f" (renamed {new_name})" if new_name != canonical else ""),
            "lines": lines,
            "apply": {"method": "POST", "url": "/api/cards/merchant/edit",
                      "body": {"name": canonical, "new_name": new_name,
                               "family": new_name if family == canonical else family,
                               "cat_l1": l1, "cat_l2": l2, "cat_l3": l3}},
            "open": {"kind": "merchant", "name": canonical}}


def _amount_rule(a: dict, data, state: State) -> dict:
    lo, hi = _bound(a.get("amount_from")), _bound(a.get("amount_under"))
    if lo is None and hi is None:
        raise ProposalError("Give the rule a lower or upper amount.")
    if lo is not None and hi is not None and lo >= hi:
        raise ProposalError("amount_from must be below amount_under.")
    l1, l2, l3 = _category(state, a, data=data)
    if a["applies_to"] == "merchant":
        canonical, _family, rows = _merchant(data, a["merchant"])
        spec = card_amount_rules.AmountRule(0, canonical, lo, hi, l1, l2, l3)
        scope, body_scope, open_ = canonical, {"merchant": canonical}, {"kind": "merchant", "name": canonical}
    else:
        s1, s2 = a.get("scope_category", "").strip(), a.get("scope_subcategory", "").strip()
        if not (s1 in card_taxonomy.TREE and (not s2 or _valid(state, s1, s2))):
            raise ProposalError(f"{s1} › {s2} is not a category the rule can cover.")
        spec = card_amount_rules.AmountRule(0, "", lo, hi, l1, l2, l3, s1, s2)
        rows = [r for r in data.rows if r.txn_type in ("purchase", "refund") and spec.covers_category(r.cat_l1, r.cat_l2)]
        scope, body_scope = (s2 or f"all of {s1}") + " (every merchant)", {"scope_l1": s1, "scope_l2": s2}
        open_ = {"kind": "category", "name": s1}
    for r in data.amount_rules:
        if r.same_scope(spec) and card_amount_rules.overlaps(lo, hi, r.amount_min, r.amount_max):
            raise ProposalError("That range overlaps an existing amount rule for the same merchant or category.")
    hit = [r for r in rows if r.category_source != "user" and card_amount_rules.in_range(abs(r.amount), lo, hi)]
    merchants = len({r.canonical_merchant for r in hit})
    lines = [f"Applies to: {scope}", f"Charges {_range(lo, hi)} → {l1} › {l2}" + (f" › {l3}" if l3 != l2 else ""),
             f"Covers about {len(hit)} charge{'s' if len(hit) != 1 else ''} ({_money(sum(-r.amount for r in hit))})"
             + (f" at {merchants} merchant{'s' if merchants != 1 else ''}" if a["applies_to"] != "merchant" else "")
             + ", and future imports"]
    return {"type": "amount_rule", "title": f"Amount rule: {scope.split(' (')[0]}, {_range(lo, hi)} → {l2}",
            "lines": lines,
            "apply": {"method": "POST", "url": "/api/cards/amount-rules",
                      "body": {**body_scope, "amount_min": lo, "amount_max": hi, "cat_l1": l1, "cat_l2": l2, "cat_l3": l3}},
            "open": open_}


def _recurring(a: dict, data, state: State) -> dict:
    name = (a.get("name") or "").strip()[:80]
    merchant = (a.get("merchant") or "").strip()
    if not name or not merchant:
        raise ProposalError("Give the recurring charge a name and a merchant.")
    lo, hi = _bound(a.get("amount_min")), _bound(a.get("amount_max"))
    if lo is not None and hi is not None and lo > hi:
        raise ProposalError("amount_min must not be above amount_max.")
    m = merchant.lower()
    rows = [r for r in data.rows if r.txn_type == "purchase"
            and m in (r.canonical_merchant.lower(), (r.merchant_family or "").lower(), data.safe(r.canonical_merchant).lower())]
    if not rows:
        raise ProposalError(f"No charges from {merchant!r}. Use the exact merchant or parent brand name from a lookup.")
    billed_as = rows[0].canonical_merchant if rows[0].canonical_merchant.lower() == m or data.safe(rows[0].canonical_merchant).lower() == m \
        else (rows[0].merchant_family or merchant)
    hit = [r for r in rows if (lo is None or -r.amount >= lo - 0.005) and (hi is None or -r.amount <= hi + 0.005)]
    lines = [f"Billed as: {billed_as}, {_range(lo, hi, upper_inclusive=True)}",
             f"Every {CADENCE_WORDS[a['cadence']]}, as a {a['kind']}",
             f"Matches {len(hit)} charge{'s' if len(hit) != 1 else ''} so far"]
    return {"type": "recurring", "title": f"Track {name} as a recurring {a['kind']}", "lines": lines,
            "apply": {"method": "POST", "url": "/api/cards/recurring/manual",
                      "body": {"name": name, "merchant": billed_as, "amount_min": lo, "amount_max": hi,
                               "cadence": a["cadence"], "kind": a["kind"]}},
            "open": {"kind": "subscriptions"}}


def _new_detail(a: dict, data, state: State) -> dict:
    l1, l2 = a["category"].strip(), a["subcategory"].strip()
    if l1 not in card_taxonomy.CHOOSABLE_L1 or not _valid(state, l1, l2):
        raise ProposalError(f"{l1} › {l2} is not a subcategory. Propose the subcategory first.")
    name = _name(a.get("name"))
    if name in _details(state, data, l1, l2):
        raise ProposalError(f"{l2} already has {name}; use it.")
    state.new_details.add((l1, l2, name))
    return {"type": "new_detail", "title": f"New detail: {_label(l1, l2, name)}",
            "lines": [f"Added under {l1} › {l2}; it takes that subcategory's settings"],
            "apply": {"method": "POST", "url": "/api/cards/categories", "body": {"l1": l1, "l2": l2, "l3": name}},
            "open": {"kind": "settings"}}


def _rename_category(a: dict, data, state: State) -> dict:
    l1, l2, l3 = a["category"].strip(), a["subcategory"].strip(), a.get("detail", "").strip()
    if l1 not in card_taxonomy.CHOOSABLE_L1:
        raise ProposalError(f"{l1!r} isn't a category whose names can change. Top-level categories are fixed.")
    tree = card_taxonomy.tree()
    if l2 not in tree.get(l1, {}):
        raise ProposalError(f"{l1} has no subcategory {l2!r}. Use the categories lookup for exact names.")
    if l3 and l3 not in _details(state, data, l1, l2):
        raise ProposalError(f"{l2} has no detail {l3!r}. Use the categories lookup for exact names.")
    new = _name(a.get("new_name"))
    if new == (l3 or l2):
        raise ProposalError("That is already its name.")
    if l3 and new in _details(state, data, l1, l2):
        raise ProposalError(f"{l2} already has {new}; propose a move instead.")
    if not l3 and (new in tree[l1] or (l1, new) in state.new_subcategories):
        raise ProposalError(f"{l1} already has {new}; propose a move instead.")
    rows = [r for r in data.rows if _under(r, l1, l2, l3)]
    builtin = card_taxonomy.is_builtin(l1, l2, l3)
    if l3:
        state.new_details.add((l1, l2, new))
    else:
        state.new_subcategories.add((l1, new))
    lines = [f"{_label(l1, l2, l3)} → {new}",
             f"{len(rows)} charge{'s' if len(rows) != 1 else ''} ({_money(sum(-r.amount for r in rows))}), "
             "their merchants and rules follow, and so do future imports"]
    if builtin:
        lines.append("Built-in: it keeps its rewards bucket and bill/subscription detection; rename it back any time")
    return {"type": "rename_category", "title": f"Rename {l3 or l2} to {new}", "lines": lines,
            "apply": {"method": "POST", "url": "/api/cards/categories/rename",
                      "body": {"l1": l1, "l2": l2, "l3": l3, "new_name": new}},
            "open": {"kind": "category", "name": l1}}


def _move_category(a: dict, data, state: State) -> dict:
    f1, f2, f3 = a["from_category"].strip(), a["from_subcategory"].strip(), a.get("from_detail", "").strip()
    if not card_taxonomy.is_valid(f1, f2):
        raise ProposalError(f"{f1} › {f2} is not a subcategory. Use the categories lookup for exact names.")
    rows = [r for r in data.rows if _under(r, f1, f2, f3)]
    if not rows:
        raise ProposalError(f"Nothing is filed under {_label(f1, f2, f3)}.")
    t1, t2, t3 = _category(state, {"c": a["to_category"], "s": a["to_subcategory"], "d": a.get("to_detail", "")},
                           "c", "s", "d", data=data)
    if t1 not in card_taxonomy.CHOOSABLE_L1:
        raise ProposalError(f"Charges can't be moved into {t1}.")
    if (f1, f2, f3 or None) == (t1, t2, a.get("to_detail", "").strip() or None) or (f3 and (f1, f2, f3) == (t1, t2, t3)):
        raise ProposalError("That's the same category.")
    merchants = len({r.canonical_merchant for r in rows if r.txn_type in ("purchase", "refund")})
    lines = [f"From {_label(f1, f2, f3)} to {_label(t1, t2, t3)}",
             f"Moves {len(rows)} charge{'s' if len(rows) != 1 else ''} ({_money(sum(-r.amount for r in rows))}) "
             f"at {merchants} merchant{'s' if merchants != 1 else ''}; future charges from them follow"]
    if not f3:
        lines.append(f"{f2} stays in the list, empty")
    return {"type": "move_category", "title": f"Move {f3 or f2} → {t3 if t3 != t2 else t2}", "lines": lines,
            "apply": {"method": "POST", "url": "/api/cards/categories/move",
                      "body": {"from_l1": f1, "from_l2": f2, "from_l3": f3, "to_l1": t1, "to_l2": t2,
                               "to_l3": a.get("to_detail", "").strip()}},
            "open": {"kind": "category", "name": t1}}


ROLE_WORDS = {"income": "Income", "card_payment": "Card payment", "transfer": "Transfer between your accounts",
              "spending": "Spending", "debt": "Debt payment", "saving": "Saving or investing", "ignore": "Leave out"}


def _payee_role(a: dict, data, state: State) -> dict:
    name = (a.get("payee") or "").strip()
    if not name or name == ask_data.WITHHELD or ask_data.p2p_label(name):
        raise ProposalError("Name a payee as the cash lookups show it; withheld names and person-to-person "
                            "transfers can't be sorted here (the user can do that in Sort payees).")
    rows = [t for t in data.cash_txns if t.key and t.label.lower() == name.lower()]
    if not rows:
        raise ProposalError(f"No cash payee called {name!r}. Use find_cash_transactions or cash_flow for exact names.")
    keys = {t.key for t in rows}
    if len(keys) > 1:
        raise ProposalError(f"{name!r} covers several payees; the user can sort them in Sort payees.")
    key = keys.pop()
    role, category = a["role"], (a.get("category") or "").strip()
    if category and (role != "spending" or category not in card_taxonomy.CHOOSABLE_L1):
        raise ProposalError("category applies to spending only and must be a top-level category, or empty.")
    out = sum(-t.amount for t in rows if t.amount < 0)
    inn = sum(t.amount for t in rows if t.amount > 0)
    current = data.payee_roles.get(key)
    lines = [f"Role: {ROLE_WORDS[role]}" + (f" › {category}" if category else ""),
             f"{len(rows)} payment{'s' if len(rows) != 1 else ''} on file"
             + (f", {_money(out)} out" if out else "") + (f", {_money(inn)} in" if inn else "")
             + "; future ones follow"]
    if current is not None:
        lines.append(f"Replaces: {ROLE_WORDS.get(current.role, current.role)}")
    return {"type": "payee_role", "title": f"{rows[0].label}: {ROLE_WORDS[role].lower()}", "lines": lines,
            "apply": {"method": "PUT", "url": f"/api/cashflow/payees/{key}",
                      "body": {"role": role, "label": rows[0].label, "category": category, "target": ""}},
            "open": {"kind": "payees", "name": rows[0].label}}


def _import_profile(a: dict, data, state: State) -> dict:
    """A format for the attached file. Columns are checked against its outline and the whole spec
    against Flow's own validation; the browser fills in header text and the rest from its full draft."""
    att = state.attachment
    if not att:
        raise ProposalError("No file is attached to this question. Ask the user to attach a sample statement "
                            "file in the Assistant (the paperclip) and ask again.")
    outline = att["outline"]
    width = len(outline["columns"])
    kind = a["kind"]
    name = (a.get("name") or "").strip()
    if not 1 <= len(name) <= 80:
        raise ProposalError("name needs 1 to 80 characters.")
    institution = (a.get("institution") or "").strip()[:80]
    columns: dict[str, list[int]] = {}
    for role, idx in a["columns"].items():
        if role not in ip.ROLES[kind]:
            raise ProposalError(f"{role!r} is not a column role for {kind} files. Roles: {', '.join(ip.ROLES[kind])}.")
        idx = idx if isinstance(idx, list) else [idx]
        clean = []
        for i in idx:
            if isinstance(i, bool) or not isinstance(i, (int, float)) or int(i) != i or not 0 <= int(i) < width:
                raise ProposalError(f"Column {i!r} for {role} isn't one of the file's columns (0 to {width - 1}).")
            clean.append(int(i))
        if clean:
            columns[role] = clean
    sign = None if a["sign"] == "none" else a["sign"]
    if a["amount_mode"] == "single" and sign is None:
        raise ProposalError("amount_mode single needs a sign: negative_is_out or positive_is_out.")
    footer = int(a["skip_footer_rows"])
    if not 0 <= footer < 1000 or footer != a["skip_footer_rows"]:
        raise ProposalError("skip_footer_rows must be a whole number from 0.")
    spec = {"kind": kind, "has_header": False, "columns": {r: [{"index": i} for i in v] for r, v in columns.items()},
            "amount_mode": a["amount_mode"], "sign": sign if a["amount_mode"] == "single" else None,
            "date_format": a["date_format"], "decimal": a["decimal"], "thousands": "." if a["decimal"] == "," else ",",
            "skip_footer_rows": footer}
    try:
        ip.Spec.from_json(spec)
    except ip.SpecError as exc:
        raise ProposalError(str(exc))

    def col(i):
        h = outline["columns"][i]["header"]
        return f"column {i + 1}" + (f" ({h})" if h and h != "~" else "")
    labels = {"cash": "bank statements", "card": "card statements", "balance": "balance files"}
    lines = [f"Reads {labels[kind]}" + (f" from {institution}" if institution else "")]
    lines += [f"{role.replace('_', ' ').capitalize()}: {', '.join(col(i) for i in v)}" for role, v in columns.items()]
    if kind != "balance":
        lines.append({"single": "Money out is written as a " + ("negative" if sign == "negative_is_out" else "positive")
                                + " number",
                      "debit_credit": "Separate money-out and money-in columns",
                      "amount_plus_direction": "A column says whether money went in or out"}[a["amount_mode"]])
        lines.append({"MDY": "Dates are month first", "DMY": "Dates are day first", "YMD": "Dates start with the year"}
                     [a["date_format"]] + ("; decimal comma" if a["decimal"] == "," else ""))
    if footer:
        lines.append(f"Last {footer} row(s) skipped")
    return {"type": "import_profile", "title": f"New import format: {name}", "lines": lines,
            "action": "open_import_editor",
            "import_profile": {"kind": kind, "name": name, "institution": institution, "columns": columns,
                               "amount_mode": a["amount_mode"], "sign": spec["sign"], "date_format": a["date_format"],
                               "decimal": a["decimal"], "thousands": spec["thousands"], "skip_footer_rows": footer}}
