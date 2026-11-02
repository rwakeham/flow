"""
Cash flow for the Dashboard's Sankey diagram. Pure functions: no DB, no network.

Money in comes from the cash register (deposits). Money out is the register's
payments, each sorted by its payee's role, with two exceptions that would count
money twice: payments to credit cards are replaced by the card charges in the
period (by charge date, by category, as the Cards section shows them), and
transfers between your own cash accounts are left out. What's left over is
"Added to cash", or "Drawn from cash" when more went out than came in, so both
sides of the diagram always balance. Payments whose payee has no role yet show
as "Not sorted yet" instead of being guessed into a group.

Roles come from the user (payee_roles), else from what a row matches (a card
payment on a card statement, the other half of a transfer), else from keywords
in the payee name. Deposits default to income.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

import card_privacy

ROLES = {
    "income": "Income",
    "card_payment": "Card payment",
    "transfer": "Transfer between my accounts",
    "spending": "Spending",
    "debt": "Debt payment",
    "saving": "Saving or investing",
    "ignore": "Leave out",
}
OUT_ROLES = ("spending", "debt", "saving")
CARD_PAYMENT_DAYS = 6           # a card payment posts on the card within this many days
TRANSFER_DAYS = 4               # the two halves of a transfer post within this many days
TOP_DETAILS = 6                 # detail bars per group before "Other …"
TOP_SOURCES = 5


# ── Payees ───────────────────────────────────────────────────────────────────

def payee(description: str) -> tuple[str, str]:
    """(key, label) for a register description: the cleaned merchant label and its compact key."""
    label = card_privacy.sanitize(description or "").label.strip()
    if not label:
        label = " ".join(str(description or "").split()).title()[:60] or "Unknown"
    key = card_privacy.compact_key(label) or card_privacy.compact_key(description or "") or "UNKNOWN"
    return key, label


_KEYWORDS = [
    ("card_payment", r"\b(E-?PAYMENT|EPAY|CRD ?PMT|CARD ?PAYMENT|CREDIT ?CRD|CREDIT CARD|AMEX|AMERICAN EXPRESS|"
                     r"CHASE CREDIT|CAPITAL ONE|CITI ?CARD|DISCOVER|BARCLAYCARD|SYNCHRONY)\b"),
    ("debt", r"\b(MORTGAGE|MTG|LOAN|HELOC|AUTO ?PAY LOAN|LENDING|SALLIE|NAVIENT|NELNET|SERVICING)\b"),
    ("saving", r"\b(VANGUARD|FIDELITY|SCHWAB|E\*?TRADE|BROKERAGE|ROBINHOOD|BETTERMENT|WEALTHFRONT|529|IRA|"
               r"CLEARING|INVEST(MENT)?S?|TREASURYDIRECT)\b"),
    ("transfer", r"\b(TRANSFER|XFER|TRNSFR)\b"),
    ("income", r"\b(PAYROLL|DIRECT ?DEP(OSIT)?|SALARY|DIR DEP|INTEREST|DIVIDEND)\b"),
]


def keyword_role(description: str, amount: float) -> str | None:
    """A guess from the payee's words. Card payments, loans and investing apply to money out only."""
    d = " ".join(str(description or "").upper().split())
    for role, rx in _KEYWORDS:
        if re.search(rx, d):
            if role in ("card_payment", "debt", "saving") and amount > 0:
                continue
            if role == "income" and amount < 0:
                continue
            return role
    return None


# ── Rows ─────────────────────────────────────────────────────────────────────

@dataclass
class CashRow:
    id: int
    account_id: int
    date: date
    amount: float               # positive in, negative out
    description: str
    key: str = ""
    label: str = ""

    def __post_init__(self):
        if not self.key:
            self.key, self.label = payee(self.description)


@dataclass
class CardSpend:
    """One card charge or refund (spend-positive), with its top-level category."""
    date: date
    spend: float
    category: str
    id: int = 0
    sub: str = ""               # its subcategory, for the drill-in
    merchant: str = ""
    card_id: int = 0


@dataclass
class CardPayment:
    """A payment row on a card statement (the money that arrived on the card)."""
    date: date
    amount: float               # positive


@dataclass
class Role:
    role: str
    category: str = ""          # spending: a top-level category, so cash spending lines up with cards
    target: str = ""            # debt / saving: the balance-sheet account or a name


def detect_pairs(rows: list[CashRow], card_payments: list[CardPayment]) -> dict[int, str]:
    """
    Rows whose role their data proves: a register payment equal to a card statement
    payment a few days apart is a card payment; an amount leaving one cash account and
    arriving in another within a few days is a transfer. One-to-one, nearest date first.
    """
    out: dict[int, str] = {}
    used_cards: set[int] = set()
    for r in sorted((r for r in rows if r.amount < 0), key=lambda r: r.date):
        best = None
        for i, p in enumerate(card_payments):
            if i in used_cards or abs(p.amount + r.amount) > 0.005:
                continue
            gap = abs((p.date - r.date).days)
            if gap <= CARD_PAYMENT_DAYS and (best is None or gap < best[0]):
                best = (gap, i)
        if best is not None:
            used_cards.add(best[1])
            out[r.id] = "card_payment"
    by_amount: dict[str, list[CashRow]] = defaultdict(list)
    for r in rows:
        if r.id not in out:
            by_amount[f"{abs(r.amount):.2f}"].append(r)
    for group in by_amount.values():
        outs = sorted((r for r in group if r.amount < 0), key=lambda r: r.date)
        ins = sorted((r for r in group if r.amount > 0), key=lambda r: r.date)
        taken: set[int] = set()
        for o in outs:
            match = next((i for i in ins if i.id not in taken and i.account_id != o.account_id
                          and abs((i.date - o.date).days) <= TRANSFER_DAYS), None)
            if match is not None:
                taken.add(match.id)
                out[o.id] = out[match.id] = "transfer"
    return out


def role_for(row: CashRow, roles: dict[str, Role], pairs: dict[int, str]) -> tuple[Role | None, str]:
    """(role, where it came from: 'you' | 'matched' | 'keyword' | 'default' | 'none')."""
    if row.key in roles:
        return roles[row.key], "you"
    if row.id in pairs:
        return Role(pairs[row.id]), "matched"
    kw = keyword_role(row.description, row.amount)
    if kw == "transfer" and row.amount < 0:
        # Called a transfer, but no cash account received it: likely money going to an
        # investment or savings account Flow doesn't track. Left for the user, not dropped.
        return None, "unmatched_transfer"
    if kw:
        return Role(kw), "keyword"
    if row.amount > 0:
        return Role("income"), "default"
    return None, "none"


# ── The diagram ──────────────────────────────────────────────────────────────

@dataclass
class Flow:
    nodes: list[dict] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    totals: dict = field(default_factory=dict)
    unsorted: list[dict] = field(default_factory=list)


def _top(items: dict[str, float], n: int, other: str) -> list[tuple[str, float]]:
    ranked = sorted(((k, v) for k, v in items.items() if v > 0.005), key=lambda kv: -kv[1])
    if len(ranked) <= n + 1:
        return ranked
    rest = sum(v for _, v in ranked[n:])
    return ranked[:n] + [(other, rest)]


@dataclass
class Entry:
    """One transaction's place in the diagram (what the drill-in lists)."""
    source: str                 # 'cash' | 'card'
    id: int
    date: date
    amount: float               # what it adds to its bar (a refund in a spending group is negative)
    group: str                  # income | spend | debt | save | unsorted
    detail: str                 # its bar: the source payee (income), the category / payee / account (out)
    sub: str                    # one level further: the card subcategory or the cash payee
    label: str                  # merchant or payee
    key: str = ""               # payee key (cash rows)
    account_id: int = 0         # register account (cash) or card account (card)


GROUP_OF = {"spending": "spend", "debt": "debt", "saving": "save"}
UNMATCHED_TRANSFER_HINT = ("Looks like a transfer, but the money didn't arrive in any of your cash accounts. If it "
                           "went to an investment or savings account Flow doesn't track, choose Saving or investing.")


def tag(rows: list[CashRow], card_spend: list[CardSpend], card_payments: list[CardPayment],
        roles: dict[str, Role], start: date, end: date) -> tuple[list[Entry], dict]:
    """
    Every transaction in [start, end] with the bar it belongs to, plus what was left out
    (card payments, transfers, "leave out"). The diagram and the drill-in both come from this.
    """
    pairs = detect_pairs(rows, card_payments)
    out: list[Entry] = []
    left = {"card_payments": 0.0, "transfers": 0.0, "left_out": 0.0}
    for r in rows:
        if not (start <= r.date <= end):
            continue
        role, _why = role_for(r, roles, pairs)
        e = Entry("cash", r.id, r.date, 0.0, "", r.label, r.label, r.label, r.key, r.account_id)
        if role is None:
            if r.amount < 0:
                e.group, e.amount = "unsorted", -r.amount
                out.append(e)
            continue
        if role.role == "income":
            e.group, e.amount = "income", r.amount
        elif role.role == "card_payment":
            left["card_payments"] += -r.amount
            continue
        elif role.role == "transfer":
            left["transfers"] += -r.amount if r.amount < 0 else 0.0     # each transfer once, by the money leaving
            continue
        elif role.role == "ignore":
            left["left_out"] += abs(r.amount)
            continue
        elif role.role in OUT_ROLES:
            e.group, e.amount = GROUP_OF[role.role], -r.amount          # a deposit from such a payee reduces it
            if role.role == "spending":
                e.detail = (role.category or r.label) + " (cash)"
            else:
                e.detail = role.target or r.label
        else:
            continue
        out.append(e)
    for c in card_spend:
        if start <= c.date <= end:
            out.append(Entry("card", c.id, c.date, c.spend, "spend", c.category, c.sub or c.category,
                             c.merchant or c.category, "", c.card_id))
    return out, left


def build(rows: list[CashRow], card_spend: list[CardSpend], card_payments: list[CardPayment],
          roles: dict[str, Role], start: date, end: date, months: float = 1.0) -> Flow:
    """
    The Sankey for [start, end]: values divided by ``months`` (1 for totals). ``rows`` and
    ``card_payments`` may extend beyond the period (for matching across its edges).
    """
    entries, left = tag(rows, card_spend, card_payments, roles, start, end)
    div = months if months and months > 0 else 1.0
    income: dict[str, float] = defaultdict(float)
    groups: dict[str, dict[str, float]] = {g: defaultdict(float) for g in ("spend", "debt", "save")}
    unsorted: dict[str, dict] = {}
    card_cats: set[str] = set()
    for e in entries:
        if e.group == "income":
            income[e.detail] += e.amount
        elif e.group == "unsorted":
            u = unsorted.setdefault(e.key, {"key": e.key, "label": e.label, "count": 0, "total": 0.0})
            u["count"] += 1
            u["total"] += e.amount
        else:
            groups[e.group][e.detail] += e.amount
            if e.source == "card":
                card_cats.add(e.detail)
    card_paid, transfers, left_out = left["card_payments"], left["transfers"], left["left_out"]
    card_total = sum(e.amount for e in entries if e.source == "card")

    f = Flow()
    idx: dict[str, int] = {}

    def node(key, name, col, group, **extra):
        idx[key] = len(f.nodes)
        f.nodes.append({"key": key, "name": name, "col": col, "group": group, **extra})

    def link(s, t, v):
        if v > 0.005:
            f.links.append({"source": idx[s], "target": idx[t], "value": round(v / div, 2)})

    inc_total = sum(v for v in income.values() if v > 0)
    spend_detail = _top(groups["spend"], TOP_DETAILS, "Other spending")
    debt_detail = _top(groups["debt"], TOP_DETAILS, "Other debt payments")
    save_detail = _top(groups["save"], TOP_DETAILS, "Other saving")
    spend = sum(v for _, v in spend_detail)
    debt = sum(v for _, v in debt_detail)
    save = sum(v for _, v in save_detail)
    unsorted_total = sum(u["total"] for u in unsorted.values())
    net = inc_total - spend - debt - save - unsorted_total

    def members(items: dict[str, float], shown: list[tuple[str, float]], i: int) -> list[str]:
        """The detail names behind bar i: itself, or everything folded into the last "Other …" bar."""
        named = [n for n, _ in shown]
        if i == len(shown) - 1 and named[i] not in items:
            return sorted(k for k, v in items.items() if v > 0.005 and k not in named[:-1])
        return [named[i]]

    income_shown = _top(income, TOP_SOURCES, "Other income")
    for i, (name, _v) in enumerate(income_shown):
        node(f"in:{i}", name, 0, "income", details=members(income, income_shown, i))
    if net < -0.005:
        node("fromcash", "Drawn from cash", 0, "cash")
    node("hub", "Money in and cash used" if net < -0.005 else "Money in", 1, "hub")
    if spend > 0.005:
        node("spend", "Spending", 2, "spend")
    if debt > 0.005:
        node("debt", "Debt payments", 2, "debt")
    if save > 0.005:
        node("save", "Saving and investing", 2, "save")
    if net > 0.005:
        node("tocash", "Added to cash", 2, "cash")
    if unsorted_total > 0.005:
        node("unsorted", "Not sorted yet", 2, "unsorted")
    for prefix, detail, group in (("s", spend_detail, "spend"), ("d", debt_detail, "debt"), ("v", save_detail, "save")):
        for i, (name, _v) in enumerate(detail):
            card = prefix == "s" and name in card_cats
            node(f"{prefix}:{i}", name, 3, group, details=members(groups[group], detail, i),
                 **({"category": name, "from": "card"} if card else {}))

    for i, (_n, v) in enumerate(_top(income, TOP_SOURCES, "Other income")):
        link(f"in:{i}", "hub", v)
    if net < -0.005:
        link("fromcash", "hub", -net)
    link("hub", "spend", spend)
    link("hub", "debt", debt)
    link("hub", "save", save)
    if net > 0.005:
        link("hub", "tocash", net)
    link("hub", "unsorted", unsorted_total)
    for prefix, detail, parent in (("s", spend_detail, "spend"), ("d", debt_detail, "debt"), ("v", save_detail, "save")):
        for i, (_n, v) in enumerate(detail):
            link(parent, f"{prefix}:{i}", v)

    r2 = lambda v: round(v / div, 2)  # noqa: E731
    f.totals = {"income": r2(inc_total), "spending": r2(spend), "card_spending": r2(card_total),
                "debt": r2(debt), "saving": r2(save), "unsorted": r2(unsorted_total), "net": r2(net),
                "card_payments": r2(card_paid), "transfers": r2(transfers), "left_out": r2(left_out),
                "months": round(div, 2)}
    f.unsorted = sorted(({**u, "total": round(u["total"], 2)} for u in unsorted.values()), key=lambda u: -u["total"])
    return f


def months_between(start: date, end: date) -> float:
    """Months in [start, end] for per-month averages: exact for whole calendar months, else days / 30.44."""
    nxt = end + timedelta(days=1)
    if start.day == 1 and nxt.day == 1:
        return max(float((nxt.year - start.year) * 12 + nxt.month - start.month), 1.0)
    days = (end - start).days + 1
    return max(days / 30.4375, 1.0)
