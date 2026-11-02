"""
What Ask may tell Claude: the index, the trim list and the lookup tools.

Everything here runs locally over all card and cash-register transactions and
returns summaries. Privacy rules for anything that leaves the server:
  * cards are "Card A", "Card B", cash accounts "Account A" and balance-sheet
    accounts "Asset A" / "Liability A" (aliases are turned back into real names
    in the answer the user sees; institutions are never loaded);
  * merchant and payee names are cleaned (card_privacy.sanitize for register
    descriptions) and must pass card_privacy.is_safe_label, else "(withheld)";
  * person-to-person transfers keep only the service ("Zelle transfer"): the
    other party is usually a person, whom the cleaner cannot recognise;
  * never raw statement descriptions, card numbers, cardholder names or notes;
  * single transactions only from the two find_* lookups, at most 25 rows.
Every number is rounded once, half up, to what the pages show (money to the
cent, percentages to one decimal), so Claude quotes what the user can see.
"""

from __future__ import annotations

import json
import re
import string
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import card_analytics
import card_privacy
import card_taxonomy
from card_analytics import Row

MAX_RESULT_CHARS = 40_000
MAX_ROWS = 25
WITHHELD = "(withheld)"
DELIVERY_L3 = {"Delivery", "Grocery delivery"}
DELIVERY_MEMBERSHIPS = {"DashPass", "Uber One"}
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SIZE_BUCKETS = [(0, 25, "under $25"), (25, 100, "$25 to $99.99"), (100, 500, "$100 to $499.99"),
                (500, 1e18, "$500 and over")]


# Payments to people: the counterparty is dropped, whatever else the text says.
_P2P = [(re.compile(p), label) for p, label in [
    (r"\bZELLE\b", "Zelle transfer"), (r"\bVENMO\b", "Venmo transfer"),
    (r"\bCASH ?APP\b|\bSQUARE CASH\b|\bSQ \*?CASH\b", "Cash App transfer"),
    (r"\bAPPLE ?CASH\b", "Apple Cash transfer"), (r"\bPAYPAL\b.*\b(TO|FROM|INST XFER)\b", "PayPal transfer"),
    (r"\b(TRANSFER|XFER|WIRE)\b", "Transfer"), (r"^(CHECK|CHK)\b", "Check"),
]]


def p2p_label(text: str) -> str | None:
    upper = (text or "").upper()
    return next((label for rx, label in _P2P if rx.search(upper)), None)


class LookupError_(Exception):
    """A lookup Claude asked for that cannot run (bad input, not available, too large)."""


# ── Data ─────────────────────────────────────────────────────────────────────

@dataclass
class Card:
    id: int
    name: str
    alias: str
    start: date | None
    end: date | None


@dataclass
class CashAccount:
    id: int
    name: str
    alias: str


@dataclass
class BalanceAccount:
    """A balance-sheet account (investment, loan, line of credit ...), aliased "Asset A" / "Liability A"."""
    id: int
    name: str
    alias: str
    account_type: str      # 'asset' | 'liability'
    ignored: bool = False
    archived: bool = False


@dataclass
class CashTxn:
    account_id: int
    date: date
    amount: float          # positive in, negative out
    label: str             # cleaned payee, or WITHHELD
    key: str = ""          # cash_flow payee key (for payee-role proposals; never sent)


@dataclass
class Upcoming:
    account_id: int
    label: str
    amount: float
    next_date: date
    frequency: str


@dataclass
class AskData:
    today: date
    cards: list[Card]
    rows: list[Row]                        # every card transaction (spend and non-spend)
    recurring: list[dict]                  # card_recurring.detect items
    cash: list[CashAccount] = field(default_factory=list)
    cash_txns: list[CashTxn] = field(default_factory=list)
    upcoming: list[Upcoming] = field(default_factory=list)
    names: list[str] = field(default_factory=list)   # people's names never to send
    amount_rules: list = field(default_factory=list)  # card_amount_rules.AmountRule, for proposal checks
    cash_rows: list = field(default_factory=list)     # cash_flow.CashRow, for the cash flow lookup (never sent)
    payee_roles: dict = field(default_factory=dict)   # payee key -> cash_flow.Role
    balance_accounts: list[BalanceAccount] = field(default_factory=list)
    balance_periods: list[date] = field(default_factory=list)          # every period with a balance, oldest first
    balance_matrix: dict[int, tuple[list, list]] = field(default_factory=dict)  # account id -> (values, carried)

    def card_by_alias(self, alias: str) -> Card | None:
        return next((c for c in self.cards if c.alias.lower() == alias.strip().lower()), None)

    def account_by_alias(self, alias: str) -> CashAccount | None:
        return next((a for a in self.cash if a.alias.lower() == alias.strip().lower()), None)

    def balance_by_alias(self, alias: str) -> BalanceAccount | None:
        return next((a for a in self.balance_accounts if a.alias.lower() == alias.strip().lower()), None)

    def alias_map(self) -> dict[str, str]:
        out = {c.alias: c.name for c in self.cards}
        out.update({a.alias: a.name for a in self.cash})
        out.update({a.alias: a.name for a in self.balance_accounts})
        return out

    def safe(self, name: str) -> str:
        p2p = p2p_label(name)
        if p2p:
            return p2p
        return name if card_privacy.is_safe_label(name or "", self.names) else WITHHELD


def alias(prefix: str, i: int) -> str:
    letters = string.ascii_uppercase
    return f"{prefix} {letters[i]}" if i < 26 else f"{prefix} {letters[i // 26 - 1]}{letters[i % 26]}"


def unalias(text: str, mapping: dict[str, str]) -> str:
    """Replace "Card A" / "Account A" / "Asset A" / "Liability A" (and "Cards A and B") with the real names."""
    return card_analytics.unalias(text, mapping)


def realias(text: str, data: AskData) -> str:
    """The reverse, for text coming back from the browser (earlier answers, questions naming a card)."""
    for a, real in sorted(data.alias_map().items(), key=lambda kv: -len(kv[1])):
        text = re.sub(rf"(?<!\w){re.escape(real)}(?!\w)", a, text, flags=re.IGNORECASE)
    return text


AREAS = ("cards", "cash", "balances")      # card_ai.AREAS: what the AI may see, each with its own switch
ALL_AREAS = {a: True for a in AREAS}


def areas_on(areas: dict | None) -> dict[str, bool]:
    return {a: bool((areas if areas is not None else ALL_AREAS).get(a)) for a in AREAS}


def load(db, areas: dict | None = None) -> AskData:
    """
    Everything Ask can look up, read once per question. An area switched off under Settings → AI
    (``areas``: cards, cash, balances) is not read at all, so nothing of it can reach the index or a lookup.
    """
    import cash_flow
    from models import PayeeRole, RegisterAccount, Transaction
    from recurring import detect_recurring_patterns
    from routers.cards import _load
    from card_ai import _cardholder_names

    on = areas_on(areas)
    names = _cardholder_names(db)          # people's names: used only to withhold labels, never sent
    data = AskData(today=date.today(), cards=[], rows=[], recurring=[], names=names)
    if on["cards"]:
        import card_amount_rules
        ld = _load(db, None, None, None)
        cov = ld.coverage
        data.cards = [Card(cid, ld.card_names[cid], alias("Card", i), *(cov.get(cid) or (None, None)))
                      for i, cid in enumerate(sorted(ld.card_names))]
        data.rows, data.recurring, data.amount_rules = ld.rows, ld.recurring_items, card_amount_rules.load(db)
    if on["balances"]:
        load_balances(db, data)
    if not on["cash"]:
        return data

    redact = card_privacy.Redaction(names=names)
    for i, acct in enumerate(db.query(RegisterAccount).order_by(RegisterAccount.id).all()):
        data.cash.append(CashAccount(acct.id, acct.name, alias("Account", i)))
        cutoff = acct.cutoff_date if acct.cutoff_balance is not None else None
        txns = db.query(Transaction).filter(Transaction.register_account_id == acct.id).all()
        if cutoff:
            txns = [t for t in txns if t.date >= cutoff]
        opening = float(acct.cutoff_balance) if cutoff else float(acct.opening_balance)
        data.cash_txns.append(CashTxn(acct.id, cutoff or date.min, opening, "Opening balance"))
        for t in txns:
            # The register's ledger: manual rows, plus bank rows not yet matched to one.
            if t.source == "manual" or t.matched_to_id is None:
                label = p2p_label(t.description) or card_privacy.sanitize(t.description or "", redact).label
                row = cash_flow.CashRow(t.id, acct.id, t.date, float(t.amount), t.description or t.bank_description or "")
                data.cash_rows.append(row)
                data.cash_txns.append(CashTxn(acct.id, t.date, float(t.amount), data.safe(label), row.key))
        for p in detect_recurring_patterns(txns, days_ahead=30):
            label = p2p_label(p.description) or card_privacy.sanitize(p.description or "", redact).label
            data.upcoming.append(Upcoming(acct.id, data.safe(label), float(p.amount), p.next_expected_date,
                                          p.frequency))
    data.payee_roles = {r.key: cash_flow.Role(r.role, r.category or "", r.target or "")
                        for r in db.query(PayeeRole).all()}
    return data


def load_balances(db, data: AskData) -> None:
    """The balance sheet as the Dashboard draws it (routers/data.balance_matrix), aliased by type."""
    from routers.data import balance_matrix

    periods, accounts, matrix = balance_matrix(db)
    data.balance_periods = list(periods)
    data.balance_matrix = matrix
    counts = {"asset": 0, "liability": 0}
    for a in sorted(accounts, key=lambda x: x.id):
        kind = "asset" if a.account_type == "asset" else "liability"
        data.balance_accounts.append(BalanceAccount(a.id, a.name, alias(kind.capitalize(), counts[kind]), kind,
                                                    bool(a.ignored), bool(a.archived)))
        counts[kind] += 1


# ── Rounding and size ────────────────────────────────────────────────────────

def _round(v: float, places: int) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(v)).quantize(q, rounding=ROUND_HALF_UP))


def clean(obj, key: str = ""):
    """Round numbers once (money 2 dp, *_pct 1 dp), dates to ISO, drop links and ids."""
    if isinstance(obj, dict):
        return {k: clean(v, k) for k, v in obj.items() if k not in ("nav", "url", "id", "txn_id")}
    if isinstance(obj, (list, tuple)):
        return [clean(v, key) for v in obj]
    if isinstance(obj, bool) or obj is None or isinstance(obj, (int, str)):
        return obj
    if isinstance(obj, float):
        return _round(obj, 1 if key.endswith("_pct") else 2)
    if isinstance(obj, date):
        return obj.isoformat()
    return obj


def _longest_list(obj, path=()):
    best = (0, None)
    if isinstance(obj, dict):
        for k, v in obj.items():
            cand = _longest_list(v, path + (k,))
            if cand[0] > best[0]:
                best = cand
    elif isinstance(obj, list):
        best = (len(obj), path)
    return best


def fit(result: dict, limit: int = MAX_RESULT_CHARS) -> str:
    """JSON text under ``limit``: halve the longest list until it fits, noting it; else refuse."""
    text = json.dumps(result, sort_keys=True)
    notes = []
    while len(text) > limit:
        n, path = _longest_list(result)
        if not path or n <= 1:
            raise LookupError_("This lookup is too large: ask for fewer sections or something narrower.")
        parent = result
        for k in path[:-1]:
            parent = parent[k]
        parent[path[-1]] = parent[path[-1]][: n // 2]
        notes.append(f"{'.'.join(path)} shortened to its first {n // 2} of {n} entries to fit")
        result["note"] = "; ".join(notes)
        text = json.dumps(result, sort_keys=True)
    return text


# ── Helpers ──────────────────────────────────────────────────────────────────

def spend(rows: list[Row]) -> list[Row]:
    return card_analytics.spend_rows(rows)


def data_end(data: AskData) -> date:
    ends = [c.end for c in data.cards if c.end]
    return min(max(ends), data.today) if ends else data.today


def window(data: AskData, months: int) -> tuple[date | None, date]:
    end = data_end(data)
    if months <= 0:
        return None, end
    return end - timedelta(days=round(months * 30.44) - 1), end


def _parse_date(s: str, what: str) -> date | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise LookupError_(f"{what} must be YYYY-MM-DD or empty")


def _months_between(lo: date, hi: date) -> float:
    return max((hi - lo).days + 1, 1) / 30.44


def _matches_category(r: Row, cat: str) -> bool:
    c = cat.strip().lower()
    return not c or c in (r.cat_l1.lower(), r.cat_l2.lower(), (r.cat_l3 or "").lower())


def _matches_merchant(r: Row, merchant: str) -> bool:
    m = merchant.strip().lower()
    return not m or m in (r.canonical_merchant or "").lower() or m in (r.merchant_family or "").lower()


def cat_name(l1: str, l2: str) -> str:
    """'Category > Subcategory' as it may be sent: your private subcategories are masked."""
    return f"{l1} > {card_taxonomy.public_l2(l1, l2)}"


def detail_name(l1: str, l2: str, l3: str) -> str:
    """'Category > Subcategory > Detail' as it may be sent (private names masked)."""
    return f"{cat_name(l1, l2)} > {card_taxonomy.public_l3(l1, l2, l3 or l2)}"


def card_alias(data: AskData, card_id: int) -> str:
    return next((c.alias for c in data.cards if c.id == card_id), "?")


def _card_filter(data: AskData, card: str) -> Card | None:
    if not card.strip():
        return None
    c = data.card_by_alias(card)
    if c is None:
        raise LookupError_(f"No card called {card!r}. Cards: {', '.join(x.alias for x in data.cards)}")
    return c


def _tx_nav(merchant: str = "", l1: str = "", l2: str = "", card: Card | None = None) -> dict:
    nav = {"section": "cards", "tab": "transactions"}
    if merchant:
        nav["q"] = merchant
    if l1:
        nav["l1"] = l1
    if l2:
        nav["l2"] = l2
    if card:
        nav["card"] = card.id
    return nav


# ── Trim list (code owns the ranking) ────────────────────────────────────────

EASE_ORDER = {"easy": 0, "moderate": 1, "harder": 2}


def trim_items(data: AskData) -> list[dict]:
    """
    Where spending could be trimmed, easiest first, then by monthly cost, over
    the last ~6 months of card data. Claude explains these; it does not add any.
    """
    lo, hi = window(data, 6)
    rows = [r for r in data.rows if lo <= r.date <= hi]
    s = spend(rows)
    months = _months_between(lo, hi)
    items: list[dict] = []

    def per_merchant(g: list[Row], top: int = 8) -> list[dict]:
        by: dict[str, list[Row]] = defaultdict(list)
        for r in g:
            by[data.safe(r.canonical_merchant)].append(r)
        out = [{"merchant": k, "total": sum(x.spend for x in v), "count": len(v),
                "avg": sum(x.spend for x in v) / len(v), "monthly": sum(x.spend for x in v) / months}
               for k, v in by.items()]
        return sorted(out, key=lambda d: -d["total"])[:top]

    # 1. Subscriptions still running.
    subs = [it for it in data.recurring if it["kind"] == "subscription" and it.get("verdict") != "dismissed"
            and it.get("status", "active") != "ended"]
    if subs:
        detail = sorted(({
            "merchant": data.safe(it.get("label") or it["merchant"]), "card": card_alias(data, it["card_account_id"]),
            "cadence": it["cadence"], "typical_amount": it["typical_amount"], "monthly": it["monthly_equivalent"],
            "status": it.get("status", "active"), "last_charge": it["most_recent_charge"], "change": it.get("change_detected") or "",
            "note": it.get("notes") or "",
        } for it in subs), key=lambda d: -d["monthly"])
        items.append({"kind": "subscriptions", "title": "Subscriptions", "ease": "easy",
                      "why": "Recurring charges you can cancel or downgrade; check price increases and overlaps.",
                      "monthly": sum(d["monthly"] for d in detail), "count": len(detail),
                      "price_increases": sum(1 for d in detail if "price change" in d["change"] or "up " in d["change"]),
                      "possible_duplicates": sum(1 for d in detail if "duplicate" in d["note"]),
                      "detail": detail, "nav": {"section": "cards", "tab": "subscriptions"}})

    # 2. Delivery, delivery fees and delivery memberships.
    deliv = [r for r in s if r.b3 in DELIVERY_L3 or (r.b2 == "Memberships" and r.b3 in DELIVERY_MEMBERSHIPS)]
    if deliv:
        total = sum(r.spend for r in deliv)
        items.append({"kind": "delivery", "title": "Food and grocery delivery", "ease": "easy",
                      "why": "Delivery carries fees and markups over the same food bought directly.",
                      "monthly": total / months, "total": total, "orders": len(deliv),
                      "detail": per_merchant(deliv), "nav": _tx_nav(l2="Dining")})

    # 3. Card fees and interest.
    fees = [r for r in rows if r.txn_type in ("card_fee", "interest") and r.amount < 0]
    if fees:
        by: dict[str, float] = defaultdict(float)
        for r in fees:
            by[r.txn_subtype or r.txn_type] += -r.amount
        total = sum(by.values())
        items.append({"kind": "card_fees", "title": "Card fees and interest", "ease": "easy",
                      "why": "Annual fees, foreign transaction fees and interest are avoidable costs.",
                      "monthly": total / months, "total": total,
                      "detail": [{"fee": k, "total": v} for k, v in sorted(by.items(), key=lambda kv: -kv[1])],
                      "nav": _tx_nav(l1="Financial / Investment")})

    # 4. Small habitual purchases (coffee, convenience): frequent and cheap.
    by_m: dict[str, list[Row]] = defaultdict(list)
    for r in s:
        if r.txn_type == "purchase":
            by_m[r.canonical_merchant].append(r)
    habit = [(m, g) for m, g in by_m.items() if len(g) >= 8 and sum(x.spend for x in g) / len(g) < 25]
    if habit:
        g_all = [r for _, g in habit for r in g]
        items.append({"kind": "small_habits", "title": "Small frequent purchases", "ease": "moderate",
                      "why": "Many small purchases add up; fewer visits is the easiest lever.",
                      "monthly": sum(r.spend for r in g_all) / months, "purchases": len(g_all),
                      "detail": per_merchant(g_all), "nav": {"section": "cards", "tab": "merchants"}})

    # 5. Merchants growing fastest: last 3 months against the 3 before.
    mid = hi - timedelta(days=91)
    recent = defaultdict(float)
    prior = defaultdict(float)
    for r in s:
        if r.cat_l1 in ("Bills & Utilities", "Financial / Investment", "Government & Taxes", "Transfers / P2P"):
            continue
        (recent if r.date > mid else prior)[r.canonical_merchant] += r.spend
    for m in sorted(recent, key=lambda m: -(recent[m] - prior.get(m, 0))):
        before, now = prior.get(m, 0.0), recent[m]
        if before <= 0 or (now - before) / 3 < 50 or now < before * 1.2:
            continue
        rs = [r for r in s if r.canonical_merchant == m]
        items.append({"kind": "growing_merchant", "title": f"Spending up at {data.safe(m)}", "ease": "moderate",
                      "why": "Spending here rose noticeably in the last three months.",
                      "merchant": data.safe(m), "category": card_taxonomy.public_l2(rs[0].cat_l1, rs[0].cat_l2),
                      "monthly": now / 3, "previous_monthly": before / 3,
                      "change_pct": (now - before) / before * 100,
                      "visits_last_3_months": sum(1 for r in rs if r.date > mid),
                      "avg_purchase": sum(r.spend for r in rs) / len(rs),
                      "nav": _tx_nav(merchant=m)})
        if sum(1 for it in items if it["kind"] == "growing_merchant") >= 3:
            break

    # 6. Dining out (not delivery) and other discretionary categories.
    dining = [r for r in s if r.b2 == "Dining" and r.b3 not in DELIVERY_L3]
    if dining:
        items.append({"kind": "dining_out", "title": "Dining out", "ease": "moderate",
                      "why": "Restaurants and bars are usually the largest flexible food cost.",
                      "monthly": sum(r.spend for r in dining) / months, "visits": len(dining),
                      "detail": per_merchant(dining), "nav": _tx_nav(l2="Dining")})
    disc: dict[tuple, list[Row]] = defaultdict(list)
    for r in s:
        if (card_analytics._fixedness(r.cat_l2, r.recurring_kind in card_analytics.RECURRING_KINDS) == "Discretionary"
                and r.cat_l1 not in ("Food & Beverage", "Transfers / P2P", "Uncategorized")
                and r.b3 not in DELIVERY_L3):
            disc[(r.cat_l1, r.cat_l2)].append(r)
    for (l1, l2), g in sorted(disc.items(), key=lambda kv: -sum(r.spend for r in kv[1]))[:3]:
        items.append({"kind": "discretionary_category", "title": f"{card_taxonomy.public_l2(l1, l2)} ({l1})", "ease": "harder",
                      "why": "A flexible category: spending less here means choosing to buy less.",
                      "monthly": sum(r.spend for r in g) / months, "purchases": len(g),
                      "detail": per_merchant(g, 5), "nav": _tx_nav(l1=l1, l2=l2)})

    items = [it for it in items if it["monthly"] > 0]
    items.sort(key=lambda it: (EASE_ORDER[it["ease"]], -it["monthly"]))
    for i, it in enumerate(items, 1):
        it["rank"] = i
        it["annual"] = it["monthly"] * 12
        it["basis"] = f"card spending {lo.isoformat()} to {hi.isoformat()}"
    return items


def trim_summary(it: dict) -> dict:
    keep = ("rank", "kind", "title", "ease", "why", "monthly", "annual", "count", "orders", "merchant",
            "change_pct", "price_increases", "possible_duplicates")
    return {k: it[k] for k in keep if k in it}


# ── Index ────────────────────────────────────────────────────────────────────

def page_context(data: AskData, page: dict | None) -> dict:
    """The page on screen, checked against what exists; unknown ids are dropped."""
    page = page if isinstance(page, dict) else {}
    section = page.get("section")
    out: dict = {"section": section if section in ("dashboard", "cashflow", "cards") else "unknown"}
    if section == "cards":
        tab = page.get("tab")
        out["tab"] = tab if isinstance(tab, str) and re.fullmatch(r"[a-z]{3,20}", tab) else "overview"
        card = next((c for c in data.cards if c.id == page.get("card_id")), None)
        out["card"] = card.alias if card else "all cards"
        category = page.get("category")
        if out["tab"] == "categories" and any(r.cat_l1 == category for r in data.rows):
            out["category"] = category
        merchant = page.get("merchant")
        if out["tab"] == "merchants" and any(r.canonical_merchant == merchant for r in data.rows):
            out["merchant"] = data.safe(merchant)
    elif section == "cashflow":
        acct = next((a for a in data.cash if a.id == page.get("account_id")), None)
        out["account"] = acct.alias if acct else None
    elif section == "dashboard":
        tab = page.get("tab")
        out["tab"] = tab if tab in ("overview", "assets", "liabilities", "cashflow") else "overview"
    return out


def build_index(data: AskData, page: dict | None, areas: dict | None = None) -> dict:
    """The index: each area's summary while that area is on (``areas``), and which areas are off."""
    on = areas_on(areas)
    index: dict = {"today": data.today}
    if on["cards"]:
        lo, hi = window(data, 6)
        s6 = [r for r in spend(data.rows) if lo <= r.date <= hi]
        total6 = sum(r.spend for r in s6)
        months = _months_between(lo, hi)
        by_l1: dict[str, float] = defaultdict(float)
        for r in s6:
            by_l1[r.cat_l1] += r.spend
        lo12, _ = window(data, 12)
        by_month: dict[str, float] = defaultdict(float)
        for r in spend(data.rows):
            if r.date >= lo12:
                by_month[card_analytics.month_key(r.date)] += r.spend
        index.update({
            "cards": [{"card": c.alias, "data_from": c.start, "data_to": c.end,
                       "transactions": sum(1 for r in data.rows if r.card_account_id == c.id)} for c in data.cards],
            "card_spending_by_category": {
                "period": f"{lo.isoformat()} to {hi.isoformat()}", "total": total6,
                "rows": [{"category": k, "total": v, "monthly_avg": v / months,
                          "share_pct": (v / total6 * 100) if total6 else 0.0}
                         for k, v in sorted(by_l1.items(), key=lambda kv: -kv[1])]},
            "card_net_spend_by_month": [{"month": m, "net": v} for m, v in sorted(by_month.items())],
            "category_list": {"top_level_categories": len(card_taxonomy.CHOOSABLE_L1),
                              "subcategories": sum(len(card_taxonomy.tree().get(c, {})) for c in card_taxonomy.CHOOSABLE_L1),
                              "lookup": "categories"},
            "trim_items": [trim_summary(it) for it in trim_items(data)[:8]],
        })
    if on["cash"]:
        balances = cash_balances(data)
        index["cash_accounts"] = [{"account": a.alias, **balances.get(a.id, {})} for a in data.cash]
    if on["balances"]:
        index["net_worth"] = net_worth_index(data)
    off = [a for a in AREAS if not on[a]]
    if off:
        index["off"] = off
    index["page"] = page_context(data, page)
    return clean(index)


def cash_balances(data: AskData) -> dict[int, dict]:
    out = {}
    for a in data.cash:
        txns = [t for t in data.cash_txns if t.account_id == a.id]
        now = sum(t.amount for t in txns if t.date <= data.today)
        later = sum(t.amount for t in txns if t.date > data.today)
        real = [t.date for t in txns if t.label != "Opening balance"]
        out[a.id] = {"balance_today": now, "scheduled_after_today": later,
                     "data_from": min(real) if real else None, "data_to": max(real) if real else None}
    return out


# ── Net worth ────────────────────────────────────────────────────────────────
# The Dashboard's net worth chart (index.html deriveSeries) lines the balance sheet up with the cash
# register by calendar month and counts cash accounts as assets; the same arithmetic runs here, over the
# data already loaded, so the Assistant's figures are the chart's.

NET_WORTH_MAX_MONTHS = 24


def _ym(d: date) -> str:
    return d.isoformat()[:7]


def cash_month_ends(data: AskData) -> dict[int, dict[str, float]]:
    """Each cash account's balance at the end of every month it has rows in (routers/register's timeseries rule)."""
    out: dict[int, dict[str, float]] = {}
    for a in data.cash:
        txns = [t for t in data.cash_txns if t.account_id == a.id]
        running = sum(t.amount for t in txns if t.label == "Opening balance")
        by_month: dict[str, float] = {}
        for t in sorted((t for t in txns if t.label != "Opening balance"), key=lambda t: t.date):
            running += t.amount
            by_month[_ym(t.date)] = running        # the month's last row leaves its month-end balance
        out[a.id] = by_month
    return out


def net_worth_series(data: AskData) -> tuple[list[str], dict[str, list], dict[str, list]]:
    """
    (months, values by alias, carried flags by alias): one entry per calendar month from the balance
    sheet's first month to today's, each account's value carried into months it has no entry for.
    Balance-sheet accounts come from the matrix; cash accounts from their month-end balances.
    """
    bal_months: dict[str, int] = {}
    for i, p in enumerate(data.balance_periods):
        bal_months[_ym(p)] = i                      # the newest period in a month wins, as on the chart
    cash = cash_month_ends(data)
    first = min(bal_months) if bal_months else min((m for mm in cash.values() for m in mm), default=None)
    if first is None:
        return [], {}, {}
    months = sorted(m for m in {*bal_months, *(m for mm in cash.values() for m in mm)} if first <= m <= _ym(data.today))
    values: dict[str, list] = {}
    carried: dict[str, list] = {}
    for a in data.balance_accounts:
        vals, flags = data.balance_matrix.get(a.id) or ([None] * len(data.balance_periods), [False] * len(data.balance_periods))
        last, last_carried, out, outc = None, False, [], []
        for m in months:
            if m in bal_months:
                last, last_carried = vals[bal_months[m]], flags[bal_months[m]]
                outc.append(last_carried)
            else:
                outc.append(last is not None)       # a month with no balance at all: every value is carried
            out.append(last)
        values[a.alias], carried[a.alias] = out, outc
    for a in data.cash:
        last, out = None, []
        for m in months:
            last = cash[a.id].get(m, last)
            out.append(last)
        values[a.alias], carried[a.alias] = out, [False] * len(months)
    return months, values, carried


def net_worth_totals(data: AskData, months: list[str], values: dict[str, list], carried: dict[str, list]) -> list[dict]:
    """Per month: assets (balance-sheet assets plus cash), liabilities (negative), net worth, carried count."""
    live = [a for a in data.balance_accounts if not a.ignored]
    rows = []
    for i, m in enumerate(months):
        assets = sum(values[a.alias][i] or 0.0 for a in live if a.account_type == "asset")
        assets += sum(values[a.alias][i] or 0.0 for a in data.cash)
        liabs = sum(values[a.alias][i] or 0.0 for a in live if a.account_type != "asset")
        rows.append({"month": m, "net_worth": assets + liabs, "assets": assets, "liabilities": liabs,
                     "carried_accounts": sum(1 for a in live if carried[a.alias][i] and values[a.alias][i] is not None)})
    return rows


def net_worth_index(data: AskData) -> dict:
    """The index's net worth block: the latest month's totals and the change since the month before."""
    months, values, carried = net_worth_series(data)
    block = {"balance_accounts": len(data.balance_accounts), "cash_accounts_included": len(data.cash), "lookup": "net_worth"}
    if not months:
        return block
    rows = net_worth_totals(data, months, values, carried)
    cur, prev = rows[-1], rows[-2] if len(rows) > 1 else None
    return {"as_of": cur["month"], "net_worth": cur["net_worth"], "assets": cur["assets"], "liabilities": cur["liabilities"],
            "change_last_month": None if prev is None else cur["net_worth"] - prev["net_worth"],
            "data_from": months[0], "data_to": months[-1], **block}


# ── Lookups ──────────────────────────────────────────────────────────────────

def _schema(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


S = {"type": "string"}
I = {"type": "integer"}
N = {"type": "number"}

TOOLS = [
    {"name": "trim_details", "strict": True,
     "description": "Full detail for one item of the app's trim list (the index lists them by rank).",
     "input_schema": _schema({"rank": I})},
    {"name": "spending_breakdown", "strict": True,
     "description": "Card spending totals grouped one way, over the last N months (0 = all data). "
                    "Filters are optional: empty string means any. category matches a category, subcategory "
                    "or detail name exactly (group_by merchant with a subcategory or detail lists every merchant "
                    "filed under it); merchant matches part of a merchant name; card is an alias like 'Card A'. Each row "
                    "has total, count, average, share_pct and the same figures for the equal period just before.",
     "input_schema": _schema({
         "group_by": {"type": "string", "enum": ["category", "subcategory", "detail", "merchant", "month", "weekday", "size", "card"]},
         "months": I, "category": S, "merchant": S, "card": S})},
    {"name": "categories", "strict": True,
     "description": "Flow's category list. category empty: the top-level categories with how many subcategories and "
                    "charges each has. A category name: its subcategories and their details, each marked built_in or "
                    "yours (built-in ones renamed by the user show original_name), with charge counts. Use it for "
                    "exact names before proposing a rename, move, detail or merchant change.",
     "input_schema": _schema({"category": S})},
    {"name": "subscriptions", "strict": True,
     "description": "Recurring card charges the app detected: cadence, typical amount, monthly cost, last charge, "
                    "price changes and possible duplicates. include_habitual adds frequent everyday merchants.",
     "input_schema": _schema({"include_habitual": {"type": "boolean"}})},
    {"name": "find_card_transactions", "strict": True,
     "description": "Individual card transactions, newest first, at most 25, with the match count and total. "
                    "Use only when the question is about specific charges. Empty string or 0 means any. "
                    "Amounts are spend-positive.",
     "input_schema": _schema({"merchant": S, "category": S, "card": S, "start": S, "end": S,
                              "min_amount": N, "max_amount": N, "limit": I})},
    {"name": "cash_summary", "strict": True,
     "description": "Cash account (checking/savings register) summary: balance, money in and out by month for the "
                    "last N months, the largest payees and upcoming recurring items in the next 30 days. "
                    "account is an alias like 'Account A', or empty for all.",
     "input_schema": _schema({"account": S, "months": I})},
    {"name": "cash_flow", "strict": True,
     "description": "Where the household's money went, as the Dashboard's Cash Flow Analysis diagram shows it: money in by "
                    "payee, then spending (card charges by category plus cash spending), debt payments, saving and "
                    "investing, added to or drawn from cash, and payments not sorted yet (payees without a role), per "
                    "month on average over the last N full months (1 = last month). Card payments and transfers between "
                    "the household's own accounts are left out so nothing counts twice.",
     "input_schema": _schema({"months": I})},
    {"name": "find_cash_transactions", "strict": True,
     "description": "Individual cash-register transactions matching part of a payee name, newest first, at most 25, "
                    "with the match count and total. Use only for questions about specific transactions.",
     "input_schema": _schema({"account": S, "payee": S, "start": S, "end": S, "limit": I})},
    {"name": "net_worth", "strict": True,
     "description": "The household's net worth as the Dashboard shows it: balance-sheet assets and liabilities "
                    "(investments, loans, lines of credit: 'Asset A', 'Liability A') plus cash accounts counted as "
                    "assets, by month for the last N months (0 = all history; for 'this year' pass the months since "
                    "January). Gives the totals, the change over the window, and each account's value and change. "
                    "account is an alias like 'Asset A', 'Liability B' or 'Account A' for that account's own history, "
                    "or empty for all.",
     "input_schema": _schema({"months": I, "account": S})},
]
TOOL_NAMES = {t["name"] for t in TOOLS}
# The data area each lookup reads; a lookup is offered only while its area is on.
TOOL_AREA = {"trim_details": "cards", "spending_breakdown": "cards", "categories": "cards", "subscriptions": "cards",
             "find_card_transactions": "cards", "cash_summary": "cash", "cash_flow": "cash",
             "find_cash_transactions": "cash", "net_worth": "balances"}


def tools_for(areas: dict | None) -> list[dict]:
    on = areas_on(areas)
    return [t for t in TOOLS if on[TOOL_AREA[t["name"]]]]


def describe(name: str, args: dict, data: AskData) -> str:
    """The status line shown while a lookup runs (with real names)."""
    a = args or {}
    real = data.alias_map()
    if name == "trim_details":
        return f"Reading trim item {a.get('rank')}"
    if name == "spending_breakdown":
        what = a.get("merchant") or a.get("category") or "card spending"
        return f"Looking up {what} by {a.get('group_by', 'category')}"
    if name == "subscriptions":
        return "Reading subscriptions and recurring charges"
    if name == "cash_flow":
        return "Reading where the money went"
    if name == "categories":
        return f"Reading the {a.get('category') or 'category'} list"
    if name == "find_card_transactions":
        return f"Finding {a.get('merchant') or a.get('category') or 'card'} transactions"
    if name == "cash_summary":
        return f"Reading {real.get(a.get('account') or '', 'cash account')} cash flow"
    if name == "find_cash_transactions":
        return f"Finding {a.get('payee') or 'cash'} transactions"
    if name == "net_worth":
        return f"Reading {real.get(a.get('account') or '', 'the balance sheet')}"
    return "Looking something up"


def run(name: str, args: dict, data: AskData) -> tuple[str, list[dict]]:
    """Run one lookup: (JSON result text, sources). Raises LookupError_ with a message for Claude."""
    if name not in TOOL_NAMES or not isinstance(args, dict):
        raise LookupError_(f"Unknown lookup {name!r}")
    fn = {"trim_details": _trim_details, "spending_breakdown": _breakdown, "subscriptions": _subscriptions,
          "categories": _categories, "cash_flow": _cash_flow,
          "find_card_transactions": _find_card, "cash_summary": _cash_summary,
          "find_cash_transactions": _find_cash, "net_worth": _net_worth}[name]
    try:
        result, sources = fn(data, args)
    except (KeyError, TypeError, ValueError) as exc:
        raise LookupError_(f"Bad input for {name}: {exc}")
    return fit(clean(result)), sources


def _trim_details(data: AskData, a: dict):
    items = trim_items(data)
    it = next((x for x in items if x["rank"] == int(a["rank"])), None)
    if it is None:
        raise LookupError_(f"There is no trim item {a['rank']}; there are {len(items)}.")
    return {k: v for k, v in it.items() if k != "nav"}, [{"label": it["title"], "nav": it["nav"]}]


def _group_key(r: Row, by: str, data: AskData) -> str:
    if by == "category":
        return r.cat_l1
    if by == "subcategory":
        return cat_name(r.cat_l1, r.cat_l2)
    if by == "detail":
        return detail_name(r.cat_l1, r.cat_l2, r.cat_l3)
    if by == "merchant":
        return data.safe(r.canonical_merchant)
    if by == "month":
        return card_analytics.month_key(r.date)
    if by == "weekday":
        return WEEKDAYS[r.date.weekday()]
    if by == "card":
        return card_alias(data, r.card_account_id)
    if by == "size":
        return next(label for lo, hi, label in SIZE_BUCKETS if lo <= r.spend < hi) if r.spend >= 0 else "refunds and credits"
    raise LookupError_(f"Unknown group_by {by!r}")


def _breakdown(data: AskData, a: dict):
    by, months = a["group_by"], int(a["months"])
    card = _card_filter(data, a.get("card", ""))
    lo, hi = window(data, months)

    def pick(rows, start, end):
        return [r for r in spend(rows) if (start is None or r.date >= start) and r.date <= end
                and (card is None or r.card_account_id == card.id)
                and _matches_category(r, a.get("category", "")) and _matches_merchant(r, a.get("merchant", ""))]

    cur = pick(data.rows, lo, hi)
    prev = pick(data.rows, lo - (hi - lo) - timedelta(days=1), lo - timedelta(days=1)) if lo and by != "month" else []
    total = sum(r.spend for r in cur)
    groups: dict[str, list[Row]] = defaultdict(list)
    for r in cur:
        groups[_group_key(r, by, data)].append(r)
    before: dict[str, float] = defaultdict(float)
    for r in prev:
        before[_group_key(r, by, data)] += r.spend
    rows = []
    for k, g in groups.items():
        t = sum(r.spend for r in g)
        row = {"group": k, "total": t, "count": len(g), "average": t / len(g),
               "share_pct": (t / total * 100) if total else 0.0}
        if prev:
            row["previous_period_total"] = before.get(k, 0.0)
            if before.get(k):
                row["change_pct"] = (t - before[k]) / before[k] * 100
        rows.append(row)
    rows.sort(key=lambda d: d["group"] if by in ("month", "size") else -d["total"])
    if by == "weekday":
        rows.sort(key=lambda d: WEEKDAYS.index(d["group"]))
    result = {"group_by": by, "period": {"from": lo or "start of data", "to": hi},
              "filters": {k: a.get(k) for k in ("category", "merchant", "card") if a.get(k)},
              "total": total, "count": len(cur), "rows": rows}
    if prev:
        result["previous_period"] = {"from": lo - (hi - lo) - timedelta(days=1), "to": lo - timedelta(days=1),
                                     "total": sum(r.spend for r in prev)}
    merchant, category = a.get("merchant", "").strip(), a.get("category", "").strip()
    if merchant or category:
        l1 = next((r.cat_l1 for r in cur if r.cat_l1.lower() == category.lower()), "")
        l2 = next((r.cat_l2 for r in cur if r.cat_l2.lower() == category.lower()), "")
        l1 = l1 or (next((r.cat_l1 for r in cur if r.cat_l2 == l2), "") if l2 else "")
        nav = _tx_nav(merchant=merchant, l1=l1, l2=l2, card=card)
        label = f"Transactions: {merchant or category}"
    else:
        tab, label = {"category": ("categories", "Categories"), "subcategory": ("categories", "Categories"),
                      "merchant": ("merchants", "Merchants"), "month": ("monthly", "Monthly")}.get(by, ("overview", "Overview"))
        nav = {"section": "cards", "tab": tab}
        if card:
            nav["card"] = card.id
    return result, [{"label": label, "nav": nav}]


def _cash_flow(data: AskData, a: dict):
    import cash_flow
    months = max(1, min(int(a["months"]), 24))
    first = data.today.replace(day=1)
    end = first - timedelta(days=1)
    y, m = first.year, first.month - months
    while m <= 0:
        y, m = y - 1, m + 12
    start = date(y, m, 1)
    spend = [cash_flow.CardSpend(r.date, r.spend, r.cat_l1) for r in data.rows
             if r.is_spend and r.cat_l1 != card_taxonomy.EXCLUDED_L1]
    payments = [cash_flow.CardPayment(r.date, r.amount) for r in data.rows if r.txn_type == "payment" and r.amount > 0]
    f = cash_flow.build(data.cash_rows, spend, payments, data.payee_roles, start, end, float(months))
    name = lambda n: n["name"] if n["group"] in ("hub", "cash", "unsorted") or n.get("from") == "card" \
        else data.safe(n["name"].removesuffix(" (cash)")) + (" (cash)" if n["name"].endswith(" (cash)") else "")  # noqa: E731
    flows = [{"from": name(f.nodes[l["source"]]), "to": name(f.nodes[l["target"]]), "per_month": l["value"]} for l in f.links]
    return {"period": {"from": start, "to": end, "months": months}, "per_month": f.totals, "flows": flows,
            "not_sorted_yet": [{"payee": data.safe(u["label"]), "total_in_period": u["total"], "payments": u["count"]}
                               for u in f.unsorted[:15]],
            "note": "Card spending is by charge date. Payees without a role can be given one with propose_payee_role."}, \
        [{"label": "Dashboard: Cash Flow Analysis", "nav": {"section": "dashboard", "tab": "cashflow"}}]


def _categories(data: AskData, a: dict):
    tree = card_taxonomy.tree()
    counts: dict[tuple, int] = defaultdict(int)
    for r in data.rows:
        counts[(r.cat_l1,)] += 1
        counts[(r.cat_l1, r.cat_l2)] += 1
        counts[(r.cat_l1, r.cat_l2, r.cat_l3)] += 1
    l1 = (a.get("category") or "").strip()
    nav = [{"label": "Categories", "nav": {"section": "cards", "tab": "categories"}}]
    if not l1:
        return {"categories": [{"category": c, "subcategories": len(tree.get(c, {})), "charges": counts[(c,)]}
                               for c in card_taxonomy.CHOOSABLE_L1],
                "fixed": "Top-level categories are fixed; subcategories and details can be added, renamed and moved."}, nav
    match = next((c for c in card_taxonomy.CHOOSABLE_L1 if c.lower() == l1.lower()), None)
    if match is None:
        raise LookupError_(f"No category {l1!r}; call categories with an empty category for the list.")
    subs = []
    for l2, l3s in tree[match].items():
        if card_taxonomy.is_private(match, l2):
            subs.append({"subcategory": card_taxonomy.PRIVATE_L2, "yours": True, "charges": counts[(match, l2)]})
            continue
        builtin = card_taxonomy.is_builtin(match, l2)
        entry = {"subcategory": l2, "built_in": builtin, "charges": counts[(match, l2)]}
        orig = card_taxonomy.base(match, l2)[1]
        if builtin and orig != l2:
            entry["original_name"] = orig
        details = []
        for l3 in dict.fromkeys(list(l3s) + sorted({r.cat_l3 for r in data.rows
                                                     if r.cat_l1 == match and r.cat_l2 == l2 and r.cat_l3 and r.cat_l3 != l2})):
            name = card_taxonomy.public_l3(match, l2, l3)
            d = {"detail": name, "charges": counts[(match, l2, l3)]}
            if name == l3:
                d["kind"] = ("built_in" if card_taxonomy.is_builtin(match, l2, l3)
                             else "yours" if l3 in l3s else "only on charges (not in the list)")
                o3 = card_taxonomy.base(match, l2, l3)[2]
                if d["kind"] == "built_in" and o3 != l3:
                    d["original_name"] = o3
            details.append(d)
        entry["details"] = details
        subs.append(entry)
    return {"category": match, "subcategories": subs}, [{"label": f"Categories: {match}",
                                                         "nav": {"section": "cards", "tab": "categories"}}]


def _subscriptions(data: AskData, a: dict):
    keep = ("subscription", "recurring bill", "recurring service", "installment") + (("habitual",) if a["include_habitual"] else ())
    items = [{
        "merchant": data.safe(it.get("label") or it["merchant"]), "billed_as": data.safe(it["merchant"]),
        "added_by_you": bool(it.get("manual_id")),
        "card": card_alias(data, it["card_account_id"]) if it["card_account_id"] else "any card", "kind": it["kind"],
        "category": cat_name(it["cat_l1"], it["cat_l2"]), "cadence": it["cadence"], "typical_amount": it["typical_amount"],
        "monthly_equivalent": it["monthly_equivalent"], "first_charge": it["first_charge"],
        "last_charge": it["most_recent_charge"], "charges": it["occurrences"],
        "change": it.get("change_detected") or "", "note": it.get("notes") or "",
        "your_verdict": it.get("verdict") or "", "status": it.get("status", "active"),
        "ended_by": {"you": "you", "auto": "no recent charges"}.get(it.get("ended_by") or "", ""),
    } for it in data.recurring if it["kind"] in keep]
    by_kind: dict[str, float] = defaultdict(float)
    for it in items:
        if it["status"] != "ended":
            by_kind[it["kind"]] += it["monthly_equivalent"]
    return ({"monthly_by_kind_still_running": dict(by_kind), "items": items,
             "note": "Ended items are past spending and are left out of the monthly totals."},
            [{"label": "Subscriptions", "nav": {"section": "cards", "tab": "subscriptions"}}])


def _limit(a: dict) -> int:
    n = int(a.get("limit") or MAX_ROWS)
    return max(1, min(n, MAX_ROWS))


def _find_card(data: AskData, a: dict):
    card = _card_filter(data, a.get("card", ""))
    start, end = _parse_date(a.get("start", ""), "start"), _parse_date(a.get("end", ""), "end")
    lo_amt, hi_amt = float(a.get("min_amount") or 0), float(a.get("max_amount") or 0)
    hits = [r for r in data.rows if r.is_spend
            and (card is None or r.card_account_id == card.id)
            and (start is None or r.date >= start) and (end is None or r.date <= end)
            and _matches_category(r, a.get("category", "")) and _matches_merchant(r, a.get("merchant", ""))
            and (not lo_amt or -r.amount >= lo_amt) and (not hi_amt or -r.amount <= hi_amt)]
    hits.sort(key=lambda r: (r.date, r.id), reverse=True)
    shown = hits[:_limit(a)]
    result = {"match_count": len(hits), "match_total": sum(-r.amount for r in hits), "shown": len(shown),
              "transactions": [{"date": r.date, "merchant": data.safe(r.canonical_merchant), "amount": -r.amount,
                                "type": r.txn_type, "category": cat_name(r.cat_l1, r.cat_l2),
                                "card": card_alias(data, r.card_account_id)} for r in shown]}
    label = f"Transactions: {a.get('merchant') or a.get('category') or 'card'}"
    return result, [{"label": label, "nav": _tx_nav(merchant=a.get("merchant", ""), card=card)}]


def _account_filter(data: AskData, account: str) -> list[CashAccount]:
    if not account.strip():
        return data.cash
    acct = data.account_by_alias(account)
    if acct is None:
        raise LookupError_(f"No cash account called {account!r}. Accounts: {', '.join(x.alias for x in data.cash)}")
    return [acct]


def _cash_summary(data: AskData, a: dict):
    accts = _account_filter(data, a.get("account", ""))
    if not accts:
        raise LookupError_("There are no cash accounts in Flow.")
    months = int(a["months"]) or 6
    lo = data.today - timedelta(days=round(months * 30.44))
    balances = cash_balances(data)
    out = []
    for acct in accts:
        txns = [t for t in data.cash_txns if t.account_id == acct.id and t.label != "Opening balance"
                and lo <= t.date <= data.today]
        by_month: dict[str, dict] = defaultdict(lambda: {"money_in": 0.0, "money_out": 0.0})
        payees: dict[str, list[float]] = defaultdict(list)
        for t in txns:
            m = by_month[card_analytics.month_key(t.date)]
            if t.amount >= 0:
                m["money_in"] += t.amount
            else:
                m["money_out"] += -t.amount
                payees[t.label].append(-t.amount)
        out.append({
            "account": acct.alias, **balances[acct.id],
            "by_month": [{"month": k, **v, "net": v["money_in"] - v["money_out"]} for k, v in sorted(by_month.items())],
            "largest_payees": [{"payee": k, "total": sum(v), "count": len(v)}
                               for k, v in sorted(payees.items(), key=lambda kv: -sum(kv[1]))[:12]],
            "upcoming_30_days": sorted(({"payee": u.label, "amount": u.amount, "next_date": u.next_date,
                                         "frequency": u.frequency} for u in data.upcoming if u.account_id == acct.id),
                                       key=lambda d: d["next_date"]),
        })
    return ({"period_from": lo, "period_to": data.today, "accounts": out,
             "note": "Money out includes card payments and transfers between accounts."},
            [{"label": f"Cash Flow: {acct.name}", "nav": {"section": "cashflow", "account": acct.id}} for acct in accts])


def _find_cash(data: AskData, a: dict):
    accts = {x.id for x in _account_filter(data, a.get("account", ""))}
    start, end = _parse_date(a.get("start", ""), "start"), _parse_date(a.get("end", ""), "end")
    q = (a.get("payee") or "").strip().lower()
    hits = [t for t in data.cash_txns if t.account_id in accts and t.label != "Opening balance"
            and (not q or q in t.label.lower())
            and (start is None or t.date >= start) and (end is None or t.date <= end)]
    hits.sort(key=lambda t: t.date, reverse=True)
    shown = hits[:_limit(a)]
    alias_of = {x.id: x.alias for x in data.cash}
    result = {"match_count": len(hits), "match_total": sum(t.amount for t in hits), "shown": len(shown),
              "transactions": [{"date": t.date, "payee": t.label, "amount": t.amount, "account": alias_of[t.account_id]}
                               for t in shown]}
    return result, [{"label": f"Cash Flow: {x.name}", "nav": {"section": "cashflow", "account": x.id}}
                    for x in data.cash if x.id in accts][:3]


NET_WORTH_NOTE = ("Liabilities are negative; net worth = assets + liabilities. Cash accounts count as assets, as on "
                  "the Dashboard. Ignored accounts are listed but left out of the totals. A carried value is the last "
                  "one entered, not a new reading.")


def _net_worth(data: AskData, a: dict):
    months, values, carried = net_worth_series(data)
    if not months:
        raise LookupError_("There is no balance sheet yet: no account balances have been entered in Flow.")
    n = int(a.get("months", 0) or 0)
    start = max(0, len(months) - min(n, NET_WORTH_MAX_MONTHS)) if n > 0 else 0
    last = len(months) - 1
    idx = range(start, last + 1)

    def change(seq: list) -> float | None:
        return None if seq[start] is None or seq[last] is None else seq[last] - seq[start]

    def span(seq: list) -> tuple[str | None, str | None]:
        have = [months[i] for i, v in enumerate(seq) if v is not None]
        return (have[0], have[-1]) if have else (None, None)

    result: dict = {"as_of": months[last], "window": {"from": months[start], "to": months[last], "months": last - start + 1},
                    "cash_included": True}
    account = (a.get("account") or "").strip()
    if account:
        acct = data.balance_by_alias(account) or data.account_by_alias(account)
        if acct is None:
            known = [x.alias for x in data.balance_accounts] + [x.alias for x in data.cash]
            raise LookupError_(f"No account called {account!r}. Accounts: {', '.join(known)}")
        seq, kind = values[acct.alias], getattr(acct, "account_type", "cash")
        data_from, data_to = span(seq)
        result.update({
            "account": acct.alias, "type": kind, "value": seq[last], "change": change(seq),
            "ignored": bool(getattr(acct, "ignored", False)), "data_from": data_from, "data_to": data_to,
            "by_month": [{"month": months[i], "value": seq[i], "carried": carried[acct.alias][i]} for i in idx],
            "note": NET_WORTH_NOTE})
        if kind == "cash":
            nav = {"section": "cashflow", "account": acct.id}
        else:
            nav = {"section": "dashboard", "tab": "assets" if kind == "asset" else "liabilities"}
        return result, [{"label": f"Dashboard: {acct.name}", "nav": nav}]

    rows = net_worth_totals(data, months, values, carried)
    first_nw, last_nw = rows[start]["net_worth"], rows[last]["net_worth"]
    result.update({
        "net_worth": last_nw, "assets": rows[last]["assets"], "liabilities": rows[last]["liabilities"],
        "change": {"net_worth": last_nw - first_nw,
                   "net_worth_pct": ((last_nw - first_nw) / abs(first_nw) * 100) if first_nw else None,
                   "assets": rows[last]["assets"] - rows[start]["assets"],
                   "liabilities": rows[last]["liabilities"] - rows[start]["liabilities"]},
        "by_month": rows[start:],
        "accounts": [],
        "note": NET_WORTH_NOTE})
    for acct in data.balance_accounts:
        seq = values[acct.alias]
        data_from, data_to = span(seq)
        result["accounts"].append({"account": acct.alias, "type": acct.account_type, "value": seq[last], "change": change(seq),
                                   "carried": carried[acct.alias][last] and seq[last] is not None, "ignored": acct.ignored,
                                   "data_from": data_from, "data_to": data_to})
    for acct in data.cash:
        seq = values[acct.alias]
        data_from, data_to = span(seq)
        result["accounts"].append({"account": acct.alias, "type": "cash", "value": seq[last], "change": change(seq),
                                   "carried": False, "ignored": False, "data_from": data_from, "data_to": data_to})
    return result, [{"label": "Dashboard: Net worth", "nav": {"section": "dashboard", "tab": "overview"}}]
