"""
Spend analytics over card transactions: the Overview, Card Summary, Monthly,
Categories, Merchants, Large & Review, Travel and Food & Wine tables.

Conventions:
  * "spend" rows are txn_type in SPEND_TYPES (purchases, refunds, benefit
    credits, card fees). Payments, carry-ins, transfers, interest and cash
    advances never enter spend totals; they are reported separately.
  * spend amounts are positive for purchases and negative for refunds. Flow
    stores charges negative, so ``Row.spend`` flips the sign.
  * run rates (daily, 30.4-day month, annualized) are extrapolations and are
    labelled as such.
  * the common period is the window every selected card covers.

Pure functions over in-memory rows: the router loads and filters, these compute.
"""

from __future__ import annotations

import re
import statistics
from calendar import monthrange
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

import card_taxonomy
from card_rules import SPEND_TYPES

BUCKETS = [(-1e18, 25, "<$25"), (25, 50, "$25-$49.99"), (50, 100, "$50-$99.99"), (100, 250, "$100-$249.99"),
           (250, 500, "$250-$499.99"), (500, 1000, "$500-$999.99"), (1000, 1e18, "$1,000+")]
FIXEDNESS = {  # inferred; always labelled as such
    "Relatively fixed": {"Utilities", "Mobile phone", "Internet", "TV", "Streaming", "Software", "Cloud / technology",
                         "Memberships", "News & media", "Insurance", "Tuition", "Tutoring", "Subscription healthcare",
                         "Fees"},
    "Essential, variable": {"Groceries", "Fuel", "Medical", "Pharmacy", "Dental", "Auto purchase / repair / maintenance",
                            "Home maintenance", "Contractors", "School-related", "Pet care", "Government fees"},
}
RECURRING_KINDS = {"subscription", "recurring bill", "recurring service", "installment"}


@dataclass
class Row:
    id: int
    card_account_id: int
    card: str
    date: date
    amount: float                # Flow convention: charges negative
    txn_type: str
    txn_subtype: str
    is_spend: bool
    canonical_merchant: str
    merchant_family: str
    cat_l1: str
    cat_l2: str
    cat_l3: str
    category_source: str = ""
    foreign_flag: bool = False
    raw_description: str = ""
    recurring_kind: str = ""
    ai_confidence: float | None = None

    @property
    def spend(self) -> float:
        return -self.amount

    # The original built-in names (card_taxonomy.base), for analytics keyed on them.
    @property
    def b2(self) -> str:
        return card_taxonomy.base(self.cat_l1, self.cat_l2)[1]

    @property
    def b3(self) -> str:
        return card_taxonomy.base(self.cat_l1, self.cat_l2, self.cat_l3)[2]


def r2(x: float | None) -> float | None:
    return None if x is None else round(float(x), 2)


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def spend_rows(rows: list[Row]) -> list[Row]:
    return [r for r in rows if r.txn_type in SPEND_TYPES]


def common_period(coverage: dict[int, tuple[date, date]]) -> dict:
    if not coverage:
        return {"exists": False, "start": None, "end": None, "days": 0}
    start = max(v[0] for v in coverage.values())
    end = min(v[1] for v in coverage.values())
    ok = start <= end
    return {"exists": ok, "start": start.isoformat(), "end": end.isoformat(),
            "days": (end - start).days + 1 if ok else 0}


def _stats(g: list[Row]) -> dict:
    p = [r.spend for r in g if r.txn_type == "purchase"]
    rf = [r.spend for r in g if r.txn_type in ("refund", "benefit_credit")]
    return {
        "net_spend": r2(sum(r.spend for r in g)),
        "gross_purchases": r2(sum(p)),
        "refunds_credits": r2(sum(rf)),
        "purchases": len(p),
        "avg_purchase": r2(statistics.mean(p)) if p else 0,
        "median_purchase": r2(statistics.median(p)) if p else 0,
    }


def _by_card(g: list[Row]) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for r in g:
        out[r.card] += r.spend
    return {k: r2(v) for k, v in sorted(out.items(), key=lambda kv: -kv[1])}


def rollup(rows: list[Row], key, total: float, with_cards: bool = True) -> list[dict]:
    groups: dict[tuple, list[Row]] = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    out = []
    for k, g in groups.items():
        d = {"key": list(k), **_stats(g), "share_of_net": (sum(r.spend for r in g) / total) if total else 0}
        if with_cards:
            d["by_card"] = _by_card(g)
        out.append(d)
    out.sort(key=lambda d: -d["net_spend"])
    return out


def _span_days(rows: list[Row]) -> int:
    if not rows:
        return 0
    return (max(r.date for r in rows) - min(r.date for r in rows)).days + 1


# ── Overview ─────────────────────────────────────────────────────────────────

def summary(rows: list[Row], coverage: dict[int, tuple[date, date]], card_names: dict[int, str],
            common_rows: list[Row], large: float = 500.0) -> dict:
    s = spend_rows(rows)
    p = [r for r in s if r.txn_type == "purchase"]
    total = sum(r.spend for r in s)
    fees = sum(r.spend for r in s if r.txn_type == "card_fee")
    cp = common_period(coverage)

    excluded: dict[str, dict] = {}
    for r in rows:
        if r.txn_type not in SPEND_TYPES:
            e = excluded.setdefault(r.txn_type, {"count": 0, "amount": 0.0})
            e["count"] += 1
            e["amount"] += r.spend
    for e in excluded.values():
        e["amount"] = r2(e["amount"])

    cov_rows = []
    cs = spend_rows(common_rows)
    common_total = sum(r.spend for r in cs)
    for cid, (first, last) in sorted(coverage.items(), key=lambda kv: card_names.get(kv[0], "")):
        mine = [r for r in s if r.card_account_id == cid]
        days = (last - first).days + 1
        net = sum(r.spend for r in mine)
        cnet = sum(r.spend for r in cs if r.card_account_id == cid)
        cov_rows.append({
            "card_account_id": cid, "card": card_names.get(cid, str(cid)),
            "first": first.isoformat(), "last": last.isoformat(), "covered_days": days,
            "spend_txns": len(mine), "net_spend": r2(net),
            "daily_rate": r2(net / days) if days else 0,
            "monthly_equiv_30_4d": r2(net / days * 30.4) if days else 0,
            "annualized_run_rate_EXTRAPOLATED": r2(net / days * 365) if days else 0,
            "common_period_net": r2(cnet),
            "common_period_share": (cnet / common_total) if common_total else None,
        })

    span = _span_days(s)
    days_basis = cp["days"] if cp["exists"] else max(span, 1)
    common_daily = common_total / max(cp["days"], 1) if cp["exists"] else 0

    sens = []
    for label, frame, days in (("Full history", s, max(span, 1)), ("Common period", cs, max(cp["days"], 1))):
        for thr in (None, 1000, 500):
            f = frame if thr is None else [r for r in frame if not (r.txn_type == "purchase" and r.spend >= thr)]
            net = sum(r.spend for r in f)
            sens.append({
                "perspective": label, "exclusion": "none" if thr is None else f"purchases >= ${thr:,}",
                "net_spend": r2(net), "excluded_amount": r2(sum(r.spend for r in frame) - net),
                "daily": r2(net / days), "monthly_equiv_30_4d": r2(net / days * 30.4),
                "annualized_EXTRAPOLATED": r2(net / days * 365),
            })

    buckets = []
    for label, frame in (("Full history", p), ("Common period", [r for r in cs if r.txn_type == "purchase"])):
        gross = sum(r.spend for r in frame)
        for lo, hi, name in BUCKETS:
            b = [r for r in frame if lo <= r.spend < hi]
            buckets.append({"perspective": label, "bucket": name, "txns": len(b),
                            "pct_txns": len(b) / len(frame) if frame else 0,
                            "gross": r2(sum(r.spend for r in b)),
                            "pct_gross": (sum(r.spend for r in b) / gross) if gross else 0})

    unclassified = sum(r.spend for r in s if r.cat_l1 == "Uncategorized")
    return {
        "overall": {
            "first": min(r.date for r in s).isoformat() if s else None,
            "last": max(r.date for r in s).isoformat() if s else None,
            "days": span,
            "gross_purchases": r2(sum(r.spend for r in p)),
            "refunds": r2(sum(r.spend for r in s if r.txn_type == "refund")),
            "benefit_credits": r2(sum(r.spend for r in s if r.txn_type == "benefit_credit")),
            "card_fees": r2(fees),
            "net_spend": r2(total),
            "net_spend_ex_fees": r2(total - fees),
            "purchase_count": len(p),
            "avg_purchase": r2(statistics.mean([r.spend for r in p])) if p else 0,
            "median_purchase": r2(statistics.median([r.spend for r in p])) if p else 0,
            "canonical_merchants": len({r.canonical_merchant for r in p}),
            "daily_rate": r2(total / days_basis) if s else 0,
            "monthly_equiv_30_4d": r2(total / max(span, 1) * 30.4) if s else 0,
            "common_period_net": r2(common_total),
            "common_daily": r2(common_daily),
            "common_monthly_equiv": r2(common_daily * 30.4),
            "common_annualized_EXTRAPOLATED": r2(common_daily * 365),
            "unclassified_spend": r2(unclassified),
            "unclassified_share": (unclassified / total) if total else 0,
            "excluded_by_type": excluded,
            "interest": excluded.get("interest", {}).get("amount", 0),
            "cash_advances": excluded.get("cash_advance", {}).get("amount", 0),
            "payments": excluded.get("payment", {}).get("amount", 0),
        },
        "common_period": cp,
        "coverage": cov_rows,
        "sensitivity": sens,
        "size_buckets": buckets,
        "large_threshold": large,
    }


# ── Categories ───────────────────────────────────────────────────────────────

def _fixedness(l2: str, recurring: bool) -> str:
    custom = card_taxonomy.custom_by_l2(l2)
    if custom is not None:
        return card_taxonomy.FIXEDNESS_CHOICES.get(custom.fixedness, "Discretionary")
    for k, members in FIXEDNESS.items():
        if card_taxonomy.base_l2(l2) in members:
            return k
    return "Relatively fixed" if recurring else "Discretionary"


def categories(rows: list[Row], level: str = "l1") -> dict:
    s = spend_rows(rows)
    total = sum(r.spend for r in s)
    keys = {"l1": lambda r: (r.cat_l1,), "l2": lambda r: (r.cat_l1, r.cat_l2),
            "l3": lambda r: (r.cat_l1, r.cat_l2, r.cat_l3)}
    table = rollup(s, keys.get(level, keys["l1"]), total)

    months = sorted({month_key(r.date) for r in s})
    by_month: dict[tuple, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in s:
        by_month[(r.cat_l1,)][month_key(r.date)] += r.spend
    trend = [{"key": list(k), "values": [r2(v.get(m, 0.0)) for m in months]}
             for k, v in sorted(by_month.items(), key=lambda kv: -sum(kv[1].values()))]

    fixed: dict[str, list[float]] = defaultdict(list)
    for r in s:
        fixed[_fixedness(r.cat_l2, r.recurring_kind in RECURRING_KINDS)].append(r.spend)
    fixedness = [{"group": k, "net_spend": r2(sum(v)), "count": len(v), "share": (sum(v) / total) if total else 0}
                 for k, v in sorted(fixed.items(), key=lambda kv: -sum(kv[1]))]
    return {"level": level, "total": r2(total), "rows": table, "months": months,
            "l1_by_month": trend, "fixed_vs_discretionary_INFERRED": fixedness}


# ── Monthly ──────────────────────────────────────────────────────────────────

def _top(g: list[Row], key, n: int) -> list[dict]:
    acc: dict[str, float] = defaultdict(float)
    for r in g:
        acc[key(r)] += r.spend
    return [{"name": k, "amount": r2(v)} for k, v in sorted(acc.items(), key=lambda kv: -kv[1])[:n]]


def monthly(rows: list[Row], coverage: dict[int, tuple[date, date]], large: float = 500.0) -> dict:
    s = spend_rows(rows)
    by_month: dict[str, list[Row]] = defaultdict(list)
    for r in s:
        by_month[month_key(r.date)].append(r)
    first_all = min((v[0] for v in coverage.values()), default=None)
    last_all = max((v[1] for v in coverage.values()), default=None)

    out = []
    for m in sorted(by_month):
        g = by_month[m]
        y, mo = int(m[:4]), int(m[5:])
        ms, me = date(y, mo, 1), date(y, mo, monthrange(y, mo)[1])
        covering = [cid for cid, (f, l) in coverage.items() if f <= ms and l >= me]
        gp = [r for r in g if r.txn_type == "purchase"]
        lg = [r for r in gp if r.spend >= large]
        net = sum(r.spend for r in g)
        out.append({
            "month": m,
            "gross": r2(sum(r.spend for r in gp)),
            "refunds_credits": r2(sum(r.spend for r in g if r.txn_type in ("refund", "benefit_credit"))),
            "card_fees": r2(sum(r.spend for r in g if r.txn_type == "card_fee")),
            "net": r2(net), "purchases": len(gp),
            "top_categories": _top(g, lambda r: r.cat_l2, 3),
            "top_merchants": _top(g, lambda r: r.canonical_merchant, 3),
            "largest": [{"name": r.canonical_merchant, "amount": r2(r.spend), "date": r.date.isoformat()}
                        for r in sorted(gp, key=lambda r: -r.spend)[:3]],
            "large_oneoff_total": r2(sum(r.spend for r in lg)),
            "net_ex_large": r2(net - sum(r.spend for r in lg)),
            "travel": r2(sum(r.spend for r in g if r.cat_l1 == "Travel")),
            "home": r2(sum(r.spend for r in g if r.cat_l1 == "Home")),
            "by_card": _by_card(g),
            "cards_fully_covering": len(covering),
            "all_cards_cover": len(covering) == len(coverage),
            "partial_month": bool(first_all and (ms < first_all or me > last_all)),
        })
    for m in out:
        m["comparable"] = not m["partial_month"] and m["all_cards_cover"]
    full = [m for m in out if m["comparable"]]
    # Highest and lowest rank every month the data spans in full, whichever cards
    # it includes; only the medians are limited to months every card covers.
    ranked = [m for m in out if not m["partial_month"]]
    highlights = None
    if ranked:
        hi = max(ranked, key=lambda m: m["net"])
        lo = min(ranked, key=lambda m: m["net"])
        highlights = {"highest": hi["month"], "lowest": lo["month"], "highest_net": hi["net"], "lowest_net": lo["net"],
                      "ranked_months": len(ranked),
                      "median_net": r2(statistics.median([m["net"] for m in full])) if full else None,
                      "median_net_ex_large": r2(statistics.median([m["net_ex_large"] for m in full])) if full else None,
                      "comparable_months": [m["month"] for m in full]}
    return {"months": out, "highlights": highlights, "large_threshold": large}


# ── Merchants ────────────────────────────────────────────────────────────────

def merchants(rows: list[Row], limit: int = 200) -> dict:
    s = spend_rows(rows)
    total = sum(r.spend for r in s)
    groups: dict[str, list[Row]] = defaultdict(list)
    for r in s:
        groups[r.canonical_merchant].append(r)
    table = []
    for name, g in groups.items():
        st = _stats(g)
        table.append({
            "merchant": name,
            "family": Counter(r.merchant_family for r in g).most_common(1)[0][0],
            **st,
            "l1": Counter(r.cat_l1 for r in g).most_common(1)[0][0],
            "l2": Counter(r.cat_l2 for r in g).most_common(1)[0][0],
            "first": min(r.date for r in g).isoformat(), "last": max(r.date for r in g).isoformat(),
            "cards": sorted({r.card for r in g}),
            "raw_variants": len({r.raw_description for r in g}),
            "share_of_net": (st["net_spend"] / total) if total else 0,
            "refund_ratio": (-st["refunds_credits"] / st["gross_purchases"]) if st["gross_purchases"] > 0 else None,
        })
    table.sort(key=lambda d: -d["net_spend"])
    for i, d in enumerate(table, 1):
        d["rank_by_net"] = i
    for i, d in enumerate(sorted(table, key=lambda d: -d["purchases"]), 1):
        d["rank_by_count"] = i

    fam: dict[str, list[float]] = defaultdict(list)
    for r in s:
        fam[r.merchant_family].append(r.spend)
    families = [{"family": k, "net_spend": r2(sum(v)), "count": len(v)}
                for k, v in sorted(fam.items(), key=lambda kv: -sum(kv[1]))]
    concentration = {f"top_{n}_share": (sum(d["net_spend"] for d in table[:n]) / total) if total else 0
                     for n in (5, 10, 20)}
    hf = sorted([d for d in table if d["purchases"] >= 8 and d["avg_purchase"] < 25], key=lambda d: -d["purchases"])
    hr = sorted([d for d in table if d["refund_ratio"] is not None and d["refund_ratio"] >= 0.2],
                key=lambda d: d["refunds_credits"])
    return {"merchants": table[:limit], "merchant_count": len(table), "families": families[:limit],
            "concentration": concentration, "high_frequency_low_ticket": hf, "high_refund": hr}


# ── Large & review ───────────────────────────────────────────────────────────

def refund_matches(rows: list[Row]) -> list[dict]:
    s = spend_rows(rows)
    purchases = [r for r in s if r.txn_type == "purchase"]
    out = []
    for r in s:
        if r.txn_type != "refund" or r.spend > -50:
            continue
        cand = [p for p in purchases if p.card_account_id == r.card_account_id
                and p.canonical_merchant == r.canonical_merchant
                and r.date - timedelta(days=180) <= p.date <= r.date]
        cand.sort(key=lambda p: p.date)
        exact = [p for p in cand if abs(p.spend + r.spend) < 0.005]
        larger = [p for p in cand if p.spend >= -r.spend]
        if exact:
            m, how = exact[-1], "exact amount"
        elif larger:
            m, how = larger[-1], "partial of larger purchase"
        else:
            m, how = None, "no match on same card and merchant (check other cards)"
        out.append({"refund_id": r.id, "refund_date": r.date.isoformat(), "card": r.card,
                    "merchant": r.canonical_merchant, "refund": r2(r.spend),
                    "matched_purchase_id": m.id if m else None,
                    "purchase_date": m.date.isoformat() if m else None,
                    "purchase_amount": r2(m.spend) if m else None, "match": how})
    return out


# Co-brand cards: (merchant family, what is bought, words a co-brand card's name contains: the
# brand or its loyalty program). Not the bare word "American": every American Express card has it.
CO_BRANDS = {
    "Delta": ("airfare", ("delta", "skymiles")),
    "United": ("airfare", ("united", "mileageplus")),
    "American": ("airfare", ("aadvantage", "american airlines")),
    "Southwest": ("airfare", ("southwest", "rapid rewards")),
    "Alaska": ("airfare", ("alaska", "mileage plan", "atmos")),
    "JetBlue": ("airfare", ("jetblue", "trueblue")),
    "Hawaiian": ("airfare", ("hawaiian",)),
    "Spirit": ("airfare", ("spirit airlines", "free spirit")),
    "Frontier": ("airfare", ("frontier airlines",)),
    "Allegiant": ("airfare", ("allegiant",)),
    "Marriott": ("stay", ("marriott", "bonvoy")),
    "Hilton": ("stay", ("hilton", "honors")),
    "Hyatt": ("stay", ("hyatt",)),
    "IHG": ("stay", ("ihg",)),
    "Wyndham": ("stay", ("wyndham",)),
    "Choice Hotels": ("stay", ("choice privileges",)),
    "Best Western": ("stay", ("best western",)),
}
_CO_BRAND_L2 = {"airfare": "Airfare", "stay": "Lodging"}


def co_brands_held(card_names) -> set[str]:
    """The merchant families the user holds a co-brand card for."""
    names = [n.lower() for n in card_names if n]
    return {fam for fam, (_what, words) in CO_BRANDS.items() if any(w in n for n in names for w in words)}


def review(rows: list[Row], large: float = 500.0, reviewed: dict[int, tuple[str, set[str]]] | None = None,
           card_names: list[str] | None = None) -> dict:
    """
    Charges worth a look. ``reviewed``: txn id -> (when, reason codes) the user
    checked off. An item stays checked off until it gains a reason it did not
    have then (a later repeat, say), so it comes back only for something new.
    Charges in a subscription series are left out: the Subscriptions tab covers
    them (price changes, two charges in a month).

    The co-brand check (a flight or stay with an airline or hotel group not charged on its
    co-brand card, CO_BRANDS) runs only for brands the user holds such a card for, judged by
    ``card_names`` (every card's name; default: the cards in ``rows``). Without one it would
    flag every flight and stay.
    """
    held = co_brands_held(card_names if card_names is not None else {r.card for r in rows})
    s = spend_rows(rows)
    purchases = [r for r in s if r.txn_type == "purchase" and r.recurring_kind != "subscription"]
    same: dict[tuple, list[Row]] = defaultdict(list)
    for r in purchases:
        same[(r.card_account_id, r.canonical_merchant, round(r.spend, 2))].append(r)

    items = []
    for r in purchases:
        reasons: list[str] = []
        codes: list[str] = []
        typ, dup = "", ""
        if r.spend >= large:
            reasons.append(f">= ${large:,.0f}")
            codes.append("large")
            typ = "Large purchase"
        group = same[(r.card_account_id, r.canonical_merchant, round(r.spend, 2))]
        if len(group) > 1 and r.spend >= 50:
            near = [x for x in group if abs((x.date - r.date).days) <= 10]
            if len(near) > 1:
                dup = f"same card, merchant and amount x{len(near)} within 10 days: worth a look, not assumed duplicate"
                reasons.append("repeated same-dollar charge")
                codes.append("repeat")
        fam = r.merchant_family
        if fam in held and r.b2 == _CO_BRAND_L2[CO_BRANDS[fam][0]] and not any(
                w in r.card.lower() for w in CO_BRANDS[fam][1]):
            reasons.append(f"{fam} {CO_BRANDS[fam][0]} not on a {fam} card")
            codes.append(fam.lower().replace(" ", "_") + "_card")
        if r.foreign_flag:
            reasons.append("foreign transaction")
            codes.append("foreign")
        if r.cat_l1 == "Uncategorized" and r.spend >= 100:
            reasons.append("uncategorized >= $100")
            codes.append("uncategorized")
        if r.category_source == "ai" and r.ai_confidence is not None and r.ai_confidence < 0.5:
            reasons.append("low-confidence AI category")
            codes.append("low_confidence")
        if not reasons:
            continue
        prio = "High" if (r.spend >= 1000 or dup) else (
            "Medium" if r.spend >= large or "not on" in " ".join(reasons) else "Low")
        items.append(_review_item(r, typ or "Review", "; ".join(reasons), dup, prio, codes, reviewed))
    for r in s:
        if r.txn_type == "refund" and r.spend <= -250:
            items.append(_review_item(r, "Large refund", "refund of $250 or more (see refund matches)", "", "Medium",
                                      ["large_refund"], reviewed))
    order = {"High": 0, "Medium": 1, "Low": 2}
    items.sort(key=lambda d: (order[d["priority"]], -abs(d["amount"])))
    return {"items": items, "refund_matches": refund_matches(rows), "large_threshold": large,
            "reviewed_count": sum(1 for i in items if i["reviewed_at"])}


def _review_item(r: Row, typ: str, why: str, dup: str, prio: str, codes: list[str],
                 reviewed: dict[int, tuple[str, set[str]]] | None) -> dict:
    when, seen = (reviewed or {}).get(r.id, (None, set()))
    return {"txn_id": r.id, "date": r.date.isoformat(), "card": r.card, "merchant": r.canonical_merchant,
            "raw_description": r.raw_description, "amount": r2(r.spend), "cat_l1": r.cat_l1, "cat_l2": r.cat_l2,
            "cat_l3": r.cat_l3, "type": typ, "why_notable": why, "potential_repeat": dup, "priority": prio,
            "codes": codes, "reviewed_at": when if when and set(codes) <= seen else None}


# ── Cards ────────────────────────────────────────────────────────────────────

def cards(rows: list[Row], common_rows: list[Row], card_names: dict[int, str], large: float = 500.0) -> dict:
    s = spend_rows(rows)
    total = sum(r.spend for r in s)
    cs = spend_rows(common_rows)
    common_total = sum(r.spend for r in cs)
    out = []
    l1s = sorted({r.cat_l1 for r in s})
    matrix = []
    for cid, name in sorted(card_names.items(), key=lambda kv: kv[1]):
        g = [r for r in s if r.card_account_id == cid]
        if not g:
            continue
        net = sum(r.spend for r in g)
        cnet = sum(r.spend for r in cs if r.card_account_id == cid)
        out.append({
            "card_account_id": cid, "card": name, **_stats(g),
            "share_of_net": (net / total) if total else 0,
            "common_period_net": r2(cnet), "common_period_share": (cnet / common_total) if common_total else None,
            "top_categories": _top(g, lambda r: r.cat_l2, 5),
            "top_merchants": _top(g, lambda r: r.canonical_merchant, 5),
            "travel_share": (sum(r.spend for r in g if r.cat_l1 == "Travel") / net) if net else 0,
            "foreign_spend": r2(sum(r.spend for r in g if r.foreign_flag)),
            "card_fees": r2(sum(r.spend for r in g if r.txn_type == "card_fee")),
            "recurring_spend": r2(sum(r.spend for r in g if r.recurring_kind in RECURRING_KINDS)),
            "large_purchases": r2(sum(r.spend for r in g if r.txn_type == "purchase" and r.spend >= large)),
        })
        by = defaultdict(float)
        for r in g:
            by[r.cat_l1] += r.spend
        matrix.append({"card": name, "values": [r2(by.get(l1, 0.0)) for l1 in l1s]})
    return {"cards": out, "card_by_l1": {"l1": l1s, "rows": matrix}}


# ── Travel and food ──────────────────────────────────────────────────────────

def travel(rows: list[Row]) -> dict:
    s = spend_rows(rows)
    total = sum(r.spend for r in s)
    tr = [r for r in s if r.cat_l1 == "Travel"]
    ttotal = sum(r.spend for r in tr)
    signals = sorted([r for r in s if (r.cat_l1 == "Travel" and r.b2 in (
        "Lodging", "Ground transportation", "Rental car", "Parking / tolls", "Other travel")) or r.foreign_flag],
        key=lambda r: r.date)
    windows, cur = [], []
    for r in signals:
        if cur and (r.date - cur[-1].date).days > 3:
            windows.append(cur)
            cur = []
        cur.append(r)
    if cur:
        windows.append(cur)
    wrows = []
    for w in windows:
        if len(w) < 3 and not any(x.b2 == "Lodging" for x in w):
            continue
        st, en = w[0].date, w[-1].date
        around = [r for r in s if st - timedelta(days=1) <= r.date <= en + timedelta(days=1)]
        wrows.append({
            "window_start": st.isoformat(), "window_end": en.isoformat(), "signal_txns": len(w),
            "lodging": r2(sum(x.spend for x in w if x.b2 == "Lodging")),
            "ground": r2(sum(x.spend for x in w if x.b2 in ("Ground transportation", "Rental car", "Parking / tolls"))),
            "dining_in_window": r2(sum(x.spend for x in around if x.b2 == "Dining")),
            "foreign": any(x.foreign_flag for x in w),
            "merchants": sorted({x.canonical_merchant for x in w})[:12],
            "label": "likely travel window (inferred from charge clustering)",
        })
    brand = defaultdict(lambda: [0.0, 0])
    for r in tr:
        b = brand[(r.cat_l2, r.merchant_family)]
        b[0] += r.spend
        b[1] += 1
    return {
        "total": r2(ttotal), "share": (ttotal / total) if total else 0,
        "rollup": rollup(tr, lambda r: (r.cat_l2, r.cat_l3), ttotal or 1),
        "by_brand": [{"l2": k[0], "family": k[1], "net_spend": r2(v[0]), "count": v[1]}
                     for k, v in sorted(brand.items(), key=lambda kv: -kv[1][0])],
        "foreign_all_spend": r2(sum(r.spend for r in s if r.foreign_flag)),
        "windows": wrows,
    }


def food(rows: list[Row]) -> dict:
    s = spend_rows(rows)
    fd = [r for r in s if r.cat_l1 == "Food & Beverage"]
    ftotal = sum(r.spend for r in fd)
    dining = sum(r.spend for r in fd if r.b2 == "Dining")
    grocery = sum(r.spend for r in fd if r.b2 == "Groceries")
    delivery = sum(r.spend for r in fd if r.b2 == "Dining" and r.b3 == "Delivery")
    al = [r for r in fd if r.b2 == "Alcohol / Wine"]
    return {
        "total": r2(ftotal), "dining": r2(dining), "groceries": r2(grocery),
        "dining_to_grocery": (dining / grocery) if grocery else None,
        "delivery_share_of_dining": (delivery / dining) if dining else None,
        "alcohol": r2(sum(r.spend for r in al)),
        "winery_direct": r2(sum(r.spend for r in al if r.b3 == "Direct winery / wine club")),
        "wine_liquor_retail": r2(sum(r.spend for r in al if r.b3 == "Wine / liquor retailer")),
        "rollup": rollup(fd, lambda r: (r.cat_l2, r.cat_l3), ftotal or 1),
        "alcohol_merchants": rollup(al, lambda r: (r.cat_l3, r.canonical_merchant), sum(r.spend for r in al) or 1,
                                    with_cards=False),
    }


# ── One category in detail ───────────────────────────────────────────────────

def category_detail(rows: list[Row], l1: str, top: int = 15) -> dict:
    """
    Everything about one L1 category: its subcategories, merchants, months and
    largest purchases, plus the Travel and Food extras for those two.
    Months run over the whole selection, so a month with none of it shows 0.
    """
    s = spend_rows(rows)
    all_total = sum(r.spend for r in s)
    g = [r for r in s if r.cat_l1 == l1]
    total = sum(r.spend for r in g)
    months = sorted({month_key(r.date) for r in s})
    by_month: dict[str, float] = defaultdict(float)
    for r in g:
        by_month[month_key(r.date)] += r.spend
    purchases = sorted((r for r in g if r.txn_type == "purchase"), key=lambda r: -r.spend)
    out = {
        "l1": l1, **_stats(g), "share_of_all": (total / all_total) if all_total else 0,
        "monthly_average": r2(total / len(months)) if months else 0.0,
        "subcategories": rollup(g, lambda r: (r.cat_l2,), total or 1),
        "merchants": rollup(g, lambda r: (r.canonical_merchant,), total or 1, with_cards=False)[:top],
        "merchant_count": len({r.canonical_merchant for r in g}),
        "by_month": [{"month": m, "net": r2(by_month.get(m, 0.0))} for m in months],
        "largest": [{"txn_id": r.id, "date": r.date.isoformat(), "merchant": r.canonical_merchant, "amount": r2(r.spend),
                     "l2": r.cat_l2, "card": r.card} for r in purchases[:10]],
    }
    if l1 == "Travel":
        out["travel"] = travel(rows)
    elif l1 == "Food & Beverage":
        out["food"] = food(rows)
    return out


# ── One merchant in detail ───────────────────────────────────────────────────

def merchant_detail(rows: list[Row], name: str) -> dict:
    """One merchant (by the name shown) within the selection: totals, months, cards, category."""
    s = spend_rows(rows)
    all_total = sum(r.spend for r in s)
    g = [r for r in s if r.canonical_merchant == name]
    total = sum(r.spend for r in g)
    months = sorted({month_key(r.date) for r in s})
    by_month: dict[str, float] = defaultdict(float)
    for r in g:
        by_month[month_key(r.date)] += r.spend
    # The merchant's own category: amount rules and one-off changes don't define it.
    own = [r for r in g if r.category_source not in ("amount_rule", "user")] or g
    cats = Counter((r.cat_l1, r.cat_l2, r.cat_l3) for r in own)
    l1, l2, l3 = cats.most_common(1)[0][0] if cats else ("", "", "")
    return {
        "name": name, **_stats(g), "share_of_all": (total / all_total) if all_total else 0,
        "monthly_average": r2(total / len(months)) if months else 0.0,
        "first": min(r.date for r in g).isoformat() if g else None,
        "last": max(r.date for r in g).isoformat() if g else None,
        "cat_l1": l1, "cat_l2": l2, "cat_l3": l3, "categories": len(cats),
        "by_month": [{"month": m, "net": r2(by_month.get(m, 0.0))} for m in months],
        "by_card": _by_card(g),
    }


# ── Insights payload (the only data the insights call sends) ─────────────────

def card_aliases(card_meta: dict[int, dict]) -> dict[int, str]:
    """card id -> "Card A", "Card B" ... in name order: the aliases every AI payload uses."""
    order = sorted(card_meta, key=lambda cid: card_meta[cid]["name"])
    return {cid: f"Card {chr(65 + i)}" for i, cid in enumerate(order)}


def unalias(text: str, mapping: dict[str, str]) -> str:
    """
    Put real names back in AI text: mapping is {"Card A": "Travel Rewards Card", ...}.
    Handles grouped mentions too: "Cards C and D", "Card A, Card B or Card C",
    "Cards A, B and C" keep their commas and conjunctions with names in place.
    A letter with no name is left as written.
    """
    if not text or not mapping:
        return text
    by_prefix: dict[str, dict[str, str]] = defaultdict(dict)
    for alias, real in mapping.items():
        prefix, _, letter = alias.rpartition(" ")
        by_prefix[prefix][letter] = real
    for prefix, names in by_prefix.items():
        p = re.escape(prefix)
        letter = r"[A-Z]{1,2}"
        sep = r"(?:,\s*(?:and\s+|or\s+|&\s+)?|\s+(?:and|or|&)\s+)"
        rx = re.compile(rf"\b{p}s?\s+{letter}\b(?:{sep}(?:{p}\s+)?{letter}\b)*")

        def repl(m, p=p, names=names):
            body = re.sub(rf"^{p}s?\s+", "", m.group(0))
            body = re.sub(rf"\b{p}\s+", "", body)
            letters = re.findall(rf"\b{letter}\b", body)
            if not letters or any(x not in names for x in letters):
                # Unknown letter: restore only the singular mentions that are known.
                return re.sub(rf"\b{p}\s+({letter})\b", lambda x: names.get(x.group(1), x.group(0)), m.group(0))
            return re.sub(rf"\b{letter}\b", lambda x: names[x.group(0)], body)

        text = rx.sub(repl, text)
    return text


def unalias_all(obj, mapping: dict[str, str]):
    """unalias every string in a JSON-like structure."""
    if isinstance(obj, str):
        return unalias(obj, mapping)
    if isinstance(obj, list):
        return [unalias_all(v, mapping) for v in obj]
    if isinstance(obj, dict):
        return {k: unalias_all(v, mapping) for k, v in obj.items()}
    return obj


def insights_payload(rows: list[Row], coverage: dict[int, tuple[date, date]], card_meta: dict[int, dict],
                     recurring_items: list[dict], recurring_totals: dict, common: bool, large: float = 500.0) -> dict:
    """
    Aggregate-only payload. Cards are aliased "Card A", "Card B"; merchants
    appear by canonical name only; dates are months; lists are capped. No
    transactions, raw descriptions, account names or cardholder names.
    """
    alias = card_aliases(card_meta)
    order = list(alias)
    # Your private subcategories and details are sent as "(private subcategory)" / "(private)".
    aliased = [Row(**{**r.__dict__, "card": alias.get(r.card_account_id, "Card ?"), "raw_description": "",
                      "cat_l2": card_taxonomy.public_l2(r.cat_l1, r.cat_l2),
                      "cat_l3": card_taxonomy.public_l3(r.cat_l1, r.cat_l2, r.cat_l3)})
               for r in rows]
    names = {cid: alias[cid] for cid in order}

    summ = summary(aliased, coverage, names, aliased, large)
    o = summ["overall"]
    cats1 = categories(aliased, "l1")
    cats2 = categories(aliased, "l2")
    mon = monthly(aliased, coverage, large)
    mer = merchants(aliased, limit=25)
    rev = review(aliased, large)
    tr = travel(aliased)
    fd = food(aliased)

    def money(x):
        return None if x is None else round(float(x))

    travel_by_l2: dict[str, float] = defaultdict(float)
    for r in spend_rows(aliased):
        if r.cat_l1 == "Travel":
            travel_by_l2[r.cat_l2] += r.spend

    return {
        "period": {
            "from": min(month_key(r.date) for r in rows) if rows else None,
            "to": max(month_key(r.date) for r in rows) if rows else None,
            "days": o["days"], "common_period": common,
            "cards": [{"alias": alias[cid], "issuer": card_meta[cid].get("issuer") or "",
                       "first_month": month_key(coverage[cid][0]) if cid in coverage else None,
                       "last_month": month_key(coverage[cid][1]) if cid in coverage else None}
                      for cid in order],
        },
        "totals": {
            "net_spend": money(o["net_spend"]), "gross_purchases": money(o["gross_purchases"]),
            "refunds": money(o["refunds"]), "benefit_credits": money(o["benefit_credits"]),
            "card_fees": money(o["card_fees"]), "purchase_count": o["purchase_count"],
            "median_purchase": money(o["median_purchase"]),
            "monthly_equivalent_EXTRAPOLATED": money(o["monthly_equiv_30_4d"]),
            "unclassified_share": round(o["unclassified_share"], 3),
            "interest": money(o["interest"]), "payments_excluded": money(o["payments"]),
        },
        "categories_l1": [{"l1": d["key"][0], "net": money(d["net_spend"]), "share": round(d["share_of_net"], 3),
                           "by_card": {k: money(v) for k, v in d["by_card"].items()}} for d in cats1["rows"]],
        "categories_l2_top": [{"l1": d["key"][0], "l2": d["key"][1], "net": money(d["net_spend"])}
                              for d in cats2["rows"][:20]],
        "monthly": [{"month": m["month"], "net": money(m["net"]), "partial": m["partial_month"],
                     "large_oneoffs": money(m["large_oneoff_total"]),
                     "top_l2": [t["name"] for t in m["top_categories"]]} for m in mon["months"]],
        "merchants_top": [{"name": d["merchant"], "l1": d["l1"], "net": money(d["net_spend"]),
                           "purchases": d["purchases"], "cards": d["cards"]} for d in mer["merchants"][:25]],
        "concentration": {k: round(v, 3) for k, v in mer["concentration"].items()},
        "high_frequency_low_ticket": [{"name": d["merchant"], "purchases": d["purchases"],
                                       "avg": money(d["avg_purchase"])} for d in mer["high_frequency_low_ticket"][:10]],
        "recurring": [{"name": it["merchant"], "kind": it["kind"], "cadence": it["cadence"],
                       "status": it.get("status", "active"),
                       "typical": money(it["typical_amount"]), "monthly_equivalent": money(it["monthly_equivalent"]),
                       "changes": [c for c in it["change_detected"].split("; ") if c],
                       "note": it["notes"], "card": alias.get(it["card_account_id"], "Card ?")}
                      for it in recurring_items[:40]],
        "recurring_totals_monthly": {k: money(v) for k, v in recurring_totals["by_kind"].items()},
        "review": {
            "large_purchases": [{"name": i["merchant"], "amount": money(i["amount"]), "month": i["date"][:7]}
                                for i in rev["items"] if i["type"] == "Large purchase"][:15],
            "same_dollar_repeats": [{"name": i["merchant"], "amount": money(i["amount"]), "month": i["date"][:7]}
                                    for i in rev["items"] if i["potential_repeat"]][:15],
            "uncategorized_over_100": sum(1 for i in rev["items"] if "uncategorized" in i["why_notable"]),
        },
        "travel": {"total": money(tr["total"]), "share": round(tr["share"], 3),
                   "by_l2": {k: money(v) for k, v in sorted(travel_by_l2.items(), key=lambda kv: -kv[1])},
                   "likely_trip_windows": len(tr["windows"]), "foreign_spend": money(tr["foreign_all_spend"])},
        "food": {"dining": money(fd["dining"]), "groceries": money(fd["groceries"]),
                 "dining_to_grocery": round(fd["dining_to_grocery"], 2) if fd["dining_to_grocery"] else None,
                 "delivery_share_of_dining": round(fd["delivery_share_of_dining"], 2)
                 if fd["delivery_share_of_dining"] is not None else None,
                 "alcohol": money(fd["alcohol"])},
        "large_threshold": large,
    }
