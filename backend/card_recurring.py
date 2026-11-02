"""
Recurring-charge and subscription detection for card transactions.

Purchases are grouped by (card, canonical merchant); each group's cadence
comes from the median gap between charges and its stability from the
coefficient of variation of the amounts.

Kinds, first match wins:
  installment        plan / installment wording; never annualized as permanent
  recurring bill     regular cadence in a bill category (utilities, phone, internet ...)
  subscription       regular cadence, near-constant amount, not an everyday category
  recurring bill     regular cadence in a bill-like L1, 3+ charges
  recurring service  regular cadence, 3+ charges, moderately stable amount
  habitual           6+ charges at an everyday merchant (coffee, grocery, fuel): a pattern, not a bill

Flow addition: a merchant flagged as subscription-like (by the AI cache or by
the issuer's extended details, e.g. "MONTHLY DUES") counts as a subscription
after two identical charges one cadence apart, so a new subscription is seen
before its third bill.

Flow additions: a series that moves to another card is followed as one
series; series the user defines (ManualSeries) claim their charges first, which
is how a subscription billed under a shopping merchant's name is split out;
and every series is active, overdue or ended (see detect).

Heuristics, not truth: the user can confirm or dismiss each merchant.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date

import card_taxonomy

CADENCES = [("weekly", 5, 9, 7), ("biweekly", 12, 17, 14), ("monthly", 25, 36, 30.4), ("bimonthly", 55, 67, 61),
            ("quarterly", 82, 100, 91), ("semiannual", 165, 200, 182), ("annual", 340, 390, 365)]
BILL_L1 = {"Bills & Utilities", "Digital & Media", "Home", "Financial / Investment", "Education / Family", "Health & Personal"}
BILL_L2 = {"Utilities", "Mobile phone", "Internet", "TV", "Insurance", "Tuition", "Tutoring",
           "Landscaping / tree care", "Contractors"}
HABIT_L2 = {"Dining", "Groceries", "Fuel", "Alcohol / Wine", "General retail / online", "Ground transportation",
            "Parking / tolls"}
CORE_KINDS = ("subscription", "recurring bill", "recurring service")


def _role(l2: str) -> str:
    """Your subcategory's role in detection ('bill', 'subscription', 'habit' or 'none'); '' if built-in."""
    c = card_taxonomy.custom_by_l2(l2)
    return c.role if c else ""
# Categories that are subscriptions by nature: two identical charges a cadence apart are enough.
SUBSCRIPTION_L2 = {"Memberships", "Streaming", "Software", "Cloud / technology", "News & media"}
KIND_ORDER = {"subscription": 0, "recurring bill": 1, "recurring service": 2, "installment": 3, "habitual": 4}


@dataclass
class Charge:
    id: int
    card_account_id: int
    card: str
    date: date
    spend: float                 # purchase amount, positive
    canonical_merchant: str
    cat_l1: str
    cat_l2: str
    cat_l3: str
    installment_flag: bool = False
    subscription_hint: bool = False
    merchant_family: str = ""


def cadence(med_gap: float) -> tuple[str | None, float | None]:
    for name, lo, hi, days in CADENCES:
        if lo <= med_gap <= hi:
            return name, days
    return None, None


def _mode(values: list[str]) -> str:
    return statistics.mode(values) if values else ""


def _month(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


ALL_CARDS = 0   # verdict card id meaning "this merchant on every card" (verdicts saved before per-card)
OVERDUE_CADENCES = 1.6   # no charge for this many cadences: overdue, still counted
ENDED_CADENCES = 2.5     # this many (about two missed charges): ended, no longer counted


def verdict_for(verdicts: dict[tuple[str, int], str], merchant: str, card_id: int) -> str | None:
    """The user's call for this merchant on this card; a card's own verdict beats an all-cards one."""
    return verdicts.get((merchant, card_id)) or verdicts.get((merchant, ALL_CARDS))


CADENCE_DAYS = {name: days for name, _lo, _hi, days in CADENCES}
MANUAL_KINDS = ("subscription", "recurring bill", "recurring service")
MOVE_GAP_CADENCES = 1.5    # a series that stops on one card and starts on another within this many periods
MOVE_AMOUNT_TOLERANCE = 0.10


@dataclass
class ManualSeries:
    """A recurring charge the user defined: charges from ``merchant`` (on ``card_account_id``,
    if set) whose amount is within [amount_min, amount_max] (bounds optional)."""
    id: int
    name: str
    merchant: str
    cadence: str
    kind: str
    card_account_id: int | None = None
    amount_min: float | None = None
    amount_max: float | None = None
    ended_through: date | None = None


def claims(m: ManualSeries, c: Charge) -> bool:
    """``merchant`` matches the merchant's name or its parent brand ("Amazon" covers "Amazon Prime")."""
    return (m.merchant.lower() in (c.canonical_merchant.lower(), (c.merchant_family or "").lower())
            and (m.card_account_id is None or c.card_account_id == m.card_account_id)
            and (m.amount_min is None or c.spend >= m.amount_min - 0.005)
            and (m.amount_max is None or c.spend <= m.amount_max + 0.005))


def _period(g: list[Charge]) -> float | None:
    gaps = [x for x in ((g[k + 1].date - g[k].date).days for k in range(len(g) - 1)) if x > 2]
    return float(statistics.median(gaps)) if gaps else None


def _link_moves(groups: dict[tuple[int, str], list[Charge]], verdicts: dict) -> list[tuple[int, str, list[Charge], list[int]]]:
    """
    Join a series that moved cards: it stops on one card and, within
    MOVE_GAP_CADENCES periods, the same merchant starts charging about the same
    amount on another. Returns (current card, merchant, charges, earlier cards).
    """
    by_merchant: dict[str, list[tuple[int, list[Charge]]]] = {}
    for (card_id, merch), g in groups.items():
        by_merchant.setdefault(merch, []).append((card_id, g))
    out = []
    for merch, gs in by_merchant.items():
        gs = [(cid, g, []) for cid, g in sorted(gs, key=lambda x: x[1][0].date)]
        merged = True
        while merged:
            merged = False
            for i, (cid_a, a, moved_a) in enumerate(gs):
                if verdict_for(verdicts, merch, cid_a) == "dismissed":
                    continue
                typical = statistics.median(c.spend for c in a)
                for j, (cid_b, b, moved_b) in enumerate(gs):
                    if j == i or cid_b == cid_a or verdict_for(verdicts, merch, cid_b) == "dismissed":
                        continue
                    # The billing period comes from whichever side has one: a series that moved
                    # after a single charge on the old card still has its period on the new one.
                    period = _period(a) or _period(b)
                    gap = (b[0].date - a[-1].date).days
                    if (period and 0.5 * period <= gap <= MOVE_GAP_CADENCES * period and typical
                            and abs(statistics.median(c.spend for c in b) - typical) <= MOVE_AMOUNT_TOLERANCE * typical):
                        gs[i] = (cid_b, a + b, moved_a + [cid_a] + moved_b)
                        del gs[j]
                        merged = True
                        break
                if merged:
                    break
        out.extend((cid, merch, g, moved) for cid, g, moved in gs)
    return out


def _status(kind: str, period: float, since: int, last_charge: date, ended_through: date | None,
            changes: list[str]) -> tuple[str, str | None]:
    if kind != "habitual" and ended_through is not None and last_charge <= ended_through:
        changes.append("you marked this ended")
        return "ended", "you"
    if kind == "habitual" or not period:
        return "active", None
    if ended_through is not None:
        changes.append(f"restarted after you marked it ended ({ended_through.isoformat()})")
    if since > ENDED_CADENCES * period:
        changes.append(f"ended: no charge in {since}d")
        return "ended", "auto"
    if since > OVERDUE_CADENCES * period:
        changes.append(f"overdue: no charge in {since}d")
        return "overdue", None
    return "active", None


def _price_changes(amts: list[float]) -> list[str]:
    out = []
    last, prev = amts[-1], (amts[-2] if len(amts) >= 2 else amts[-1])
    if prev and abs(last - prev) / prev > 0.02:
        out.append(f"price change {prev:.2f} -> {last:.2f}")
    if len(amts) >= 3 and amts[0] and (last - amts[0]) / amts[0] > 0.05:
        out.append(f"up {100 * (last - amts[0]) / amts[0]:.0f}% since first seen")
    return out


def detect(charges: list[Charge], coverage: dict[int, tuple[date, date]],
           verdicts: dict[tuple[str, int], str] | None = None,
           ended: dict[tuple[str, int], date] | None = None,
           manual: list[ManualSeries] | None = None,
           card_names: dict[int, str] | None = None) -> tuple[list[dict], dict[int, str]]:
    """
    Returns (items, kind_by_charge_id).

    ``coverage`` maps card_account_id -> (first, last) date the card's data covers;
    "overdue", "ended" and "new during period" are measured against it.
    ``verdicts`` maps (canonical merchant, card id) -> 'confirmed' | 'dismissed';
    card id ALL_CARDS applies to the merchant on every card.
    ``ended`` maps (canonical merchant, card id) -> the last charge date the
    user's "Ended" covers.
    ``manual`` are series the user defined. They claim their charges first, so
    a subscription billed under a shopping merchant's name (Kindle Unlimited as
    "Amazon") is split from the shopping; they are always confirmed, and one
    with no charges yet is "expected" (not counted).

    Each (card, merchant) series is judged on its own, except that a series
    that stops on one card and continues on another (same merchant, about the
    same amount, the next charge on time) is one series, on its current card.
    One charge is never recurring, even if the merchant is confirmed.

    Whether a series is real (confirm / dismiss) and whether it is still running
    are separate. ``status`` is "active", "overdue" (no charge for
    OVERDUE_CADENCES; still counted) or "ended" (ENDED_CADENCES, or the user said
    so; not counted). Gaps are measured to the card's last covered date, not
    today. Any charge after the date an "Ended" covers makes the series active
    again, noted "restarted".
    """
    verdicts = verdicts or {}
    ended = ended or {}
    manual = manual or []
    card_names = dict(card_names or {})
    for c in charges:
        card_names.setdefault(c.card_account_id, c.card)

    claimed: dict[int, list[Charge]] = {m.id: [] for m in manual}
    rest: list[Charge] = []
    for c in charges:
        m = next((m for m in manual if claims(m, c)), None)
        (claimed[m.id] if m else rest).append(c)

    groups: dict[tuple[int, str], list[Charge]] = {}
    for c in rest:
        groups.setdefault((c.card_account_id, c.canonical_merchant), []).append(c)
    for g in groups.values():
        g.sort(key=lambda c: (c.date, c.id))

    items: list[dict] = []
    kinds: dict[int, str] = {}
    for card_id, merch, g, moved in _link_moves(groups, verdicts):
        verdict = verdict_for(verdicts, merch, card_id)
        if verdict == "dismissed":
            continue
        g.sort(key=lambda c: (c.date, c.id))
        n = len(g)
        if n < 2:
            continue
        gaps = [(g[i + 1].date - g[i].date).days for i in range(n - 1)]
        gaps_nz = [x for x in gaps if x > 2]          # same-trip repeats are not cadence evidence
        med = float(statistics.median(gaps_nz)) if gaps_nz else 0.0
        cad, cad_days = cadence(med)
        amts = [c.spend for c in g]
        mean = statistics.mean(amts)
        cv = (statistics.pstdev(amts) / mean) if mean else 9.9
        l1 = _mode([c.cat_l1 for c in g])
        l2 = _mode([c.cat_l2 for c in g])
        l3 = _mode([c.cat_l3 for c in g])
        inst = any(c.installment_flag for c in g)
        role = _role(l2)
        b2 = card_taxonomy.base(l1, l2)[1]          # built-in name lists use the original names
        bill_l2, habit_l2 = b2 in BILL_L2 or role == "bill", b2 in HABIT_L2 or role == "habit"
        hinted = any(c.subscription_hint for c in g) or b2 in SUBSCRIPTION_L2 or role == "subscription"

        regular = cad is not None and (len(gaps_nz) >= 2 or cad in ("semiannual", "annual"))
        if gaps_nz and regular:
            regular = sum(1 for x in gaps_nz if med * 0.6 <= x <= med * 1.45) / len(gaps_nz) >= 0.6

        kind = None
        if inst and n >= 2:
            kind = "installment"
        elif regular and bill_l2 and n >= 2:
            kind = "recurring bill"
        elif regular and cv <= 0.08 and not habit_l2 and (n >= 3 or cad in ("semiannual", "annual")):
            kind = "subscription"
        elif regular and l1 in BILL_L1 and n >= 3:
            kind = "recurring bill"
        elif regular and n >= 3 and not habit_l2 and cv <= 0.35:
            kind = "recurring service"
        elif n >= 6 and habit_l2:
            kind = "habitual"
        elif hinted and cad is not None and n >= 2 and cv <= 0.02 and not habit_l2:
            kind = "subscription"
        if kind is None and verdict == "confirmed" and gaps_nz:
            kind = "subscription" if cv <= 0.08 else "recurring service"
        if kind is None:
            continue

        first_cov, last_cov = coverage.get(card_id, (g[0].date, g[-1].date))
        last = amts[-1]
        changes: list[str] = []
        if moved:
            changes.append("moved from " + ", ".join(card_names.get(x, "another card") for x in moved)
                           + f" on {next(c.date for c in g if c.card_account_id == card_id).isoformat()}")
        if kind in ("subscription", "recurring service"):
            changes.extend(_price_changes(amts))
        period = cad_days or med
        since = (last_cov - g[-1].date).days
        status, ended_by = _status(kind, period, since, g[-1].date, ended.get((merch, card_id)), changes)
        if cad_days and kind != "habitual":
            if (g[0].date - first_cov).days > 1.5 * cad_days:
                changes.append("new during period")
            big = [x for x in gaps_nz if x > 1.8 * cad_days]
            if big:
                changes.append(f"gap of {max(big)}d")
        if kind != "habitual" and cad in ("monthly", "bimonthly", "quarterly", "semiannual", "annual"):
            months = [_month(c.date) for c in g]
            dup = sorted({m for m in months if months.count(m) > 1})
            if dup:
                changes.append("2+ charges in one month: " + ", ".join(dup))

        typical = float(statistics.median(amts))
        span = max((last_cov - first_cov).days + 1, 30)
        if kind == "habitual":
            monthly_equiv = sum(amts) / span * 30.4      # actual run rate over the card's coverage
        else:
            # Typical charge per period: the cadence, else (a confirmed irregular series) the median gap.
            # Never a charge spread over months it did not recur in.
            monthly_equiv = typical * 30.4 / (cad_days or med)

        items.append(dict(
            merchant=merch, label=None, manual_id=None, moved_from=[card_names.get(x, "") for x in moved],
            card_account_id=card_id, card=card_names.get(card_id, g[-1].card),
            cat_l1=l1, cat_l2=l2, cat_l3=l3, kind=kind,
            first_charge=g[0].date.isoformat(), most_recent_charge=g[-1].date.isoformat(),
            occurrences=n, typical_amount=round(typical, 2), min_amount=round(min(amts), 2),
            max_amount=round(max(amts), 2), most_recent_amount=round(last, 2),
            cadence=cad or "irregular", median_gap_days=med, amount_cv=round(cv, 3),
            monthly_equivalent=round(monthly_equiv, 2),
            annualized_run_rate=round(monthly_equiv * 12, 2) if kind in CORE_KINDS else None,
            total_in_period=round(sum(amts), 2), change_detected="; ".join(changes),
            notes="", verdict=verdict, subscription_hint=hinted,
            status=status, ended_by=ended_by, days_since_last_charge=since,
        ))
        for c in g:
            kinds[c.id] = kind

    for m in manual:
        items.append(_manual_item(m, claimed[m.id], coverage, card_names))
        for c in claimed[m.id]:
            kinds[c.id] = m.kind

    # The same merchant still recurring on two or more cards may be a duplicate subscription.
    cards_by_merchant: dict[str, set[int]] = {}
    for it in items:
        if it["kind"] in ("subscription", "recurring service") and it["status"] not in ("ended", "expected"):
            cards_by_merchant.setdefault(it["label"] or it["merchant"], set()).add(it["card_account_id"])
    for it in items:
        if it["status"] not in ("ended", "expected") and len(cards_by_merchant.get(it["label"] or it["merchant"], ())) > 1:
            it["notes"] = "same merchant recurring on multiple cards: check for duplicate subscription"

    items.sort(key=lambda it: (it["status"] == "ended", KIND_ORDER[it["kind"]], -it["monthly_equivalent"]))
    return items, kinds


def _manual_item(m: ManualSeries, g: list[Charge], coverage: dict[int, tuple[date, date]],
                 card_names: dict[int, str]) -> dict:
    """A user-defined series from the charges it claimed (possibly none yet)."""
    g = sorted(g, key=lambda c: (c.date, c.id))
    days = CADENCE_DAYS.get(m.cadence, 30.4)
    card_id = m.card_account_id or (g[-1].card_account_id if g else None)
    changes: list[str] = []
    if g:
        amts = [c.spend for c in g]
        typical = float(statistics.median(amts))
        ends = [coverage[c.card_account_id][1] for c in g if c.card_account_id in coverage]
        last_cov = coverage[m.card_account_id][1] if m.card_account_id in coverage else max(ends, default=g[-1].date)
        since = (last_cov - g[-1].date).days
        changes.extend(_price_changes(amts))
        status, ended_by = _status(m.kind, days, since, g[-1].date, m.ended_through, changes)
    else:
        amts, since, ended_by = [], None, None
        bounds = [x for x in (m.amount_min, m.amount_max) if x is not None]
        typical = sum(bounds) / len(bounds) if bounds else 0.0
        status = "expected"
        changes.append("no matching charge yet")
    cards = sorted({card_names.get(c.card_account_id, c.card) for c in g})
    return dict(
        merchant=m.merchant, label=m.name, manual_id=m.id, moved_from=[],
        card_account_id=card_id, card=card_names.get(m.card_account_id, "") if m.card_account_id else (", ".join(cards) or "any card"),
        cat_l1=_mode([c.cat_l1 for c in g]) if g else "", cat_l2=_mode([c.cat_l2 for c in g]) if g else "",
        cat_l3=_mode([c.cat_l3 for c in g]) if g else "", kind=m.kind,
        first_charge=g[0].date.isoformat() if g else None, most_recent_charge=g[-1].date.isoformat() if g else None,
        occurrences=len(g), typical_amount=round(typical, 2), min_amount=round(min(amts), 2) if amts else None,
        max_amount=round(max(amts), 2) if amts else None, most_recent_amount=round(amts[-1], 2) if amts else None,
        cadence=m.cadence, median_gap_days=days, amount_cv=None,
        monthly_equivalent=round(typical * 30.4 / days, 2), annualized_run_rate=round(typical * 30.4 / days * 12, 2),
        total_in_period=round(sum(amts), 2), change_detected="; ".join(changes), notes="",
        verdict="confirmed", subscription_hint=True,
        status=status, ended_by=ended_by, days_since_last_charge=since,
        amount_min=m.amount_min, amount_max=m.amount_max,
    )


def totals(items: list[dict]) -> dict:
    """Monthly totals of what is still running: ended and not-yet-seen series are left out."""
    by_kind = {k: 0.0 for k in KIND_ORDER}
    for it in items:
        if it.get("status") not in ("ended", "expected"):
            by_kind[it["kind"]] += it["monthly_equivalent"]
    return {
        "core_monthly_recurring": round(sum(by_kind[k] for k in CORE_KINDS), 2),
        "by_kind": {k: round(v, 2) for k, v in by_kind.items()},
    }
