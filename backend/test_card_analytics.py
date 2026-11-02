"""
Tests for card spend analytics and the insights payload.

The reconciliation checks: card, L1 and L3 totals all equal net spend, and payments never leak into spend. The
insights payload is checked for anything that could identify a person, card or
transaction. Runnable directly (``python3 test_card_analytics.py``) or via pytest.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date

import card_analytics as A
from card_analytics import Row

_ids = iter(range(1, 100000))


def row(d, amount, merchant, l1, l2, l3=None, txn_type="purchase", card=1, family=None, foreign=False,
        raw=None, source="rule"):
    is_spend = txn_type in ("purchase", "refund", "benefit_credit", "card_fee")
    return Row(id=next(_ids), card_account_id=card, card={1: "Travel Rewards Card", 2: "Everyday Cash Back"}[card],
               date=d, amount=amount, txn_type=txn_type, txn_subtype="", is_spend=is_spend,
               canonical_merchant=merchant, merchant_family=family or merchant, cat_l1=l1, cat_l2=l2,
               cat_l3=l3 or l2, category_source=source, foreign_flag=foreign,
               raw_description=raw or merchant.upper() + " 1234 SPRINGFIELD IL")


ROWS = [
    row(date(2026, 9, 22), -26.85, "DoorDash", "Food & Beverage", "Dining", "Delivery"),
    row(date(2026, 10, 9), -626.80, "United Airlines", "Travel", "Airfare", "United", family="United"),
    row(date(2026, 10, 15), -75.00, "United Airlines", "Travel", "Airfare", "United", family="United"),
    row(date(2026, 10, 15), -75.00, "United Airlines", "Travel", "Airfare", "United", family="United"),
    row(date(2026, 10, 16), 10141.36, "Payment", "Excluded (non-spend)", "Payment", txn_type="payment"),
    row(date(2026, 10, 18), -16.10, "Avis", "Travel", "Rental car", "Avis"),
    row(date(2026, 10, 19), 16.10, "Avis", "Travel", "Rental car", "Avis", txn_type="refund"),
    row(date(2026, 10, 20), -302.14, "Target", "Shopping", "General retail / online", card=2),
    row(date(2026, 10, 21), -249.00, "SNPO", "Uncategorized", "Needs review", card=2, source="ai"),
    row(date(2026, 10, 21), -250.00, "Card fee", "Financial / Investment", "Fees", "Card annual fee",
        txn_type="card_fee"),
]
COV = {1: (date(2026, 9, 22), date(2026, 10, 22)), 2: (date(2026, 10, 17), date(2026, 10, 21))}
NAMES = {1: "Travel Rewards Card", 2: "Everyday Cash Back"}


def net():
    return round(sum(-r.amount for r in ROWS if r.is_spend), 2)


def test_totals_reconcile_and_payments_excluded():
    s = A.summary(ROWS, COV, NAMES, ROWS)
    o = s["overall"]
    assert o["net_spend"] == net()
    assert o["payments"] == -10141.36
    assert o["card_fees"] == 250.00
    assert o["net_spend_ex_fees"] == round(net() - 250, 2)
    for level in ("l1", "l2", "l3"):
        assert round(sum(r["net_spend"] for r in A.categories(ROWS, level)["rows"]), 2) == net()
    cards = A.cards(ROWS, ROWS, NAMES)
    assert round(sum(c["net_spend"] for c in cards["cards"]), 2) == net()
    assert round(sum(m["net"] for m in A.monthly(ROWS, COV)["months"]), 2) == net()
    assert all(r["key"][0] != "Excluded (non-spend)" for r in A.categories(ROWS, "l1")["rows"])


def test_common_period():
    cp = A.common_period(COV)
    assert cp == {"exists": True, "start": "2026-10-17", "end": "2026-10-21", "days": 5}
    assert not A.common_period({1: (date(2026, 1, 1), date(2026, 1, 31)), 2: (date(2026, 3, 1), date(2026, 3, 31))})["exists"]


def test_review_flags_repeats_large_and_uncategorized():
    items = A.review(ROWS)["items"]
    why = {(i["merchant"], i["amount"]): i for i in items}
    assert why[("United Airlines", 626.80)]["priority"] == "Medium"
    repeat = why[("United Airlines", 75.00)]
    assert "not assumed duplicate" in repeat["potential_repeat"] and repeat["priority"] == "High"
    assert "uncategorized" in why[("SNPO", 249.00)]["why_notable"]


def test_reviewed_items_stay_checked_off_until_a_new_reason_appears():
    items = A.review(ROWS)["items"]
    large = next(i for i in items if (i["merchant"], i["amount"]) == ("United Airlines", 626.80))
    repeat = next(i for i in items if (i["merchant"], i["amount"]) == ("United Airlines", 75.00))
    assert large["codes"] == ["large"] and "repeat" in repeat["codes"] and large["reviewed_at"] is None

    out = A.review(ROWS, reviewed={large["txn_id"]: ("2026-10-24T10:00:00", {"large"}),
                                   repeat["txn_id"]: ("2026-10-24T10:00:00", set())})   # ticked before it repeated
    by_id = {i["txn_id"]: i for i in out["items"]}
    assert by_id[large["txn_id"]]["reviewed_at"] == "2026-10-24T10:00:00"
    assert by_id[repeat["txn_id"]]["reviewed_at"] is None
    assert out["reviewed_count"] == 1


def test_co_brand_checks_only_for_someone_holding_that_card():
    """A flight off its airline's card is flagged only when one of the cards is that airline's card."""
    def codes(card_names=None):
        return {(i["merchant"], i["amount"]): i["codes"] for i in A.review(ROWS, card_names=card_names)["items"]}
    assert "united_card" not in codes()[("United Airlines", 626.80)]                    # no United card in ROWS
    assert "united_card" not in codes(["Travel Rewards Card", "Everyday Cash Back"])[("United Airlines", 626.80)]
    held = codes(["Travel Rewards Card", "Everyday Cash Back", "Example MileagePlus Card"])
    assert "united_card" in held[("United Airlines", 626.80)]                           # flown on another card
    on_it = [replace(r, card="Example MileagePlus Card") if r.merchant_family == "United" else r for r in ROWS]
    items = A.review(on_it, card_names=["Example MileagePlus Card", "Everyday Cash Back"])["items"]
    assert not any("united_card" in i["codes"] for i in items)                          # on the United card itself


def test_co_brand_programs_are_recognised_for_every_airline_and_hotel_group():
    assert A.co_brands_held(["Example SkyMiles Card", "Example Rapid Rewards Visa", "Example Honors Card",
                             "World of Hyatt Example", "Example Bonvoy Card"]) == {"Delta", "Southwest", "Hilton", "Hyatt",
                                                                                   "Marriott"}
    assert A.co_brands_held(["Example AAdvantage Card"]) == {"American"}
    assert A.co_brands_held(["American Express Example Card", "Everyday Cash Back"]) == set()   # not American Airlines
    hotel = replace(ROWS[1], id=999, canonical_merchant="Hilton", merchant_family="Hilton", cat_l2="Lodging",
                    cat_l3="Hilton", amount=-420.0)
    items = A.review([hotel], card_names=["Example Honors Card", "Everyday Cash Back"])["items"]
    assert items and "hilton_card" in items[0]["codes"] and "Hilton stay not on a Hilton card" in items[0]["why_notable"]


def test_subscription_charges_are_not_reviewed():
    from dataclasses import replace
    big = [r for r in ROWS if r.canonical_merchant == "United Airlines" and round(-r.amount, 2) == 626.80]
    assert any(i["txn_id"] == big[0].id for i in A.review(ROWS)["items"])
    subs = [replace(r, recurring_kind="subscription") if r in big else r for r in ROWS]
    assert not any(i["txn_id"] == big[0].id for i in A.review(subs)["items"])
    bills = [replace(r, recurring_kind="recurring bill") if r in big else r for r in ROWS]
    assert any(i["txn_id"] == big[0].id for i in A.review(bills)["items"])


def test_refund_matching():
    m = A.refund_matches(ROWS + [row(date(2026, 10, 22), 626.80, "United Airlines", "Travel", "Airfare",
                                     txn_type="refund", family="United")])
    assert m[0]["match"] == "exact amount" and m[0]["purchase_amount"] == 626.80


def test_merchants_and_partial_months():
    m = A.merchants(ROWS)
    assert m["merchants"][0]["merchant"] == "United Airlines"
    months = {x["month"]: x for x in A.monthly(ROWS, COV)["months"]}
    assert months["2026-09"]["partial_month"] and months["2026-10"]["partial_month"]


def test_highs_and_lows_rank_every_complete_month():
    """A month before card 2's data starts still ranks; only the medians need every card."""
    rows = [row(date(2026, 5, 10), -30000.0, "United Airlines", "Travel", "Airfare", family="United"),
            row(date(2026, 6, 10), -900.0, "Target", "Shopping", "General retail / online"),
            row(date(2026, 6, 12), -100.0, "Target", "Shopping", "General retail / online", card=2),
            row(date(2026, 7, 10), -400.0, "Target", "Shopping", "General retail / online", card=2)]
    cov = {1: (date(2026, 5, 1), date(2026, 7, 31)), 2: (date(2026, 5, 20), date(2026, 7, 31))}
    m = A.monthly(rows, cov)
    assert [x["comparable"] for x in m["months"]] == [False, True, True]
    hl = m["highlights"]
    assert (hl["highest"], hl["highest_net"], hl["lowest"], hl["lowest_net"]) == ("2026-05", 30000.0, "2026-07", 400.0)
    assert hl["ranked_months"] == 3
    assert hl["comparable_months"] == ["2026-06", "2026-07"] and hl["median_net"] == 700.0
    partial = A.monthly(rows, {1: (date(2026, 5, 1), date(2026, 7, 20)), 2: (date(2026, 5, 20), date(2026, 7, 20))})["highlights"]
    assert partial["lowest"] == "2026-06" and partial["ranked_months"] == 2     # July is in progress: not ranked


def test_category_detail_reconciles_and_shows_empty_months():
    d = A.category_detail(ROWS, "Travel")
    l1 = next(r for r in A.categories(ROWS, "l1")["rows"] if r["key"] == ["Travel"])
    assert d["net_spend"] == l1["net_spend"] == round(626.80 + 75 + 75 + 16.10 - 16.10, 2)
    assert round(sum(r["net_spend"] for r in d["subcategories"]), 2) == d["net_spend"]
    assert d["by_month"] == [{"month": "2026-09", "net": 0.0}, {"month": "2026-10", "net": d["net_spend"]}]
    assert [x["amount"] for x in d["largest"]] == [626.80, 75.0, 75.0, 16.10]
    assert d["merchants"][0]["key"] == ["United Airlines"] and "travel" in d and "food" not in d
    food = A.category_detail(ROWS, "Food & Beverage")
    assert "food" in food and food["food"]["dining"] == 26.85 and food["purchases"] == 1
    assert A.category_detail(ROWS, "Pets")["net_spend"] == 0 and A.category_detail(ROWS, "Pets")["largest"] == []


def test_unalias_restores_single_and_grouped_card_mentions():
    m = {"Card A": "Premium Travel", "Card B": "Hotel Rewards", "Card C": "Airline Miles", "Card D": "Everyday Cash Back"}
    u = lambda t: A.unalias(t, m)  # noqa: E731
    assert u("Cards C and D only appear from May and April.") == "Airline Miles and Everyday Cash Back only appear from May and April."
    assert u("Card A, Card B or Card C") == "Premium Travel, Hotel Rewards or Airline Miles"
    assert u("Cards A, B, and C") == "Premium Travel, Hotel Rewards, and Airline Miles"
    assert u("Card A's fee and Card D.") == "Premium Travel's fee and Everyday Cash Back."
    assert u("Card Z and Card A") == "Card Z and Premium Travel"              # unknown letter left alone
    assert u("Cards C and Z") == "Cards C and Z"
    assert u("Plan A and a card") == "Plan A and a card"
    assert A.unalias("Accounts A and B", {"Account A": "Checking", "Account B": "Savings"}) == "Checking and Savings"
    rep = A.unalias_all({"headline": "Card A leads", "findings": [{"numbers": ["Card B: $5"]}]}, m)
    assert rep == {"headline": "Premium Travel leads", "findings": [{"numbers": ["Hotel Rewards: $5"]}]}


def test_card_aliases_are_in_name_order():
    assert A.card_aliases({7: {"name": "Zeta"}, 3: {"name": "Alpha"}}) == {3: "Card A", 7: "Card B"}


def test_insights_payload_is_aggregate_only():
    p = A.insights_payload(ROWS, COV, {1: {"name": "Travel Rewards Card", "issuer": "Example Card Co"},
                                       2: {"name": "Everyday Cash Back", "issuer": "Example Bank"}},
                           [{"merchant": "Netflix", "kind": "subscription", "cadence": "monthly", "typical_amount": 15.49,
                             "monthly_equivalent": 15.49, "change_detected": "", "notes": "", "card_account_id": 2}],
                           {"by_kind": {"subscription": 15.49}}, common=False)
    blob = json.dumps(p)
    for leaked in ("Travel Rewards Card", "Everyday Cash Back", "SPRINGFIELD", "1234", "2026-10-15", "2026-10-21"):
        assert leaked not in blob, leaked
    assert {c["alias"] for c in p["period"]["cards"]} == {"Card A", "Card B"}
    assert p["totals"]["net_spend"] == round(net())
    assert p["recurring"][0]["card"] == "Card A"          # aliases go in name order: Everyday Cash Back is A
    assert "transactions" not in p


if __name__ == "__main__":
    import sys
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {exc!r}")
    sys.exit(1 if failed else 0)


def test_csv_export_cells_cannot_become_formulas():
    from routers.cards import spreadsheet_safe
    for bad in ("=HYPERLINK(\"http://x\")", "+1+2", "-2+3", "@SUM(A1)", "\t=1", "\r=1"):
        assert spreadsheet_safe(bad) == "'" + bad
    for ok in ("STARBUCKS", "", "Shake Shack", 12.5, None, True):
        assert spreadsheet_safe(ok) == ok


# ── The year-end summary ──────────────────────────────────────────────────────

def test_year_summary_groups_a_calendar_year_by_category_with_tax_notes():
    from types import SimpleNamespace
    from datetime import date as D
    rows = [
        Row(id=1, card_account_id=1, card="A", date=D(2026, 3, 1), amount=-100.0, txn_type="purchase", txn_subtype="",
            is_spend=True, canonical_merchant="Clinic", merchant_family="", cat_l1="Health & Personal", cat_l2="Medical", cat_l3="Medical"),
        Row(id=2, card_account_id=1, card="A", date=D(2026, 3, 9), amount=20.0, txn_type="refund", txn_subtype="",
            is_spend=True, canonical_merchant="Clinic", merchant_family="", cat_l1="Health & Personal", cat_l2="Medical", cat_l3="Medical"),
        Row(id=3, card_account_id=1, card="A", date=D(2026, 12, 24), amount=-50.0, txn_type="purchase", txn_subtype="",
            is_spend=True, canonical_merchant="Food Bank", merchant_family="", cat_l1="Giving", cat_l2="Charitable giving", cat_l3="Donation"),
        Row(id=4, card_account_id=1, card="A", date=D(2025, 12, 31), amount=-999.0, txn_type="purchase", txn_subtype="",
            is_spend=True, canonical_merchant="Last year", merchant_family="", cat_l1="Giving", cat_l2="Charitable giving", cat_l3="Donation"),
        Row(id=5, card_account_id=1, card="A", date=D(2026, 5, 5), amount=-300.0, txn_type="payment", txn_subtype="",
            is_spend=False, canonical_merchant="Payment", merchant_family="", cat_l1="Transfers / P2P", cat_l2="Card payment", cat_l3=""),
    ]
    cash = [SimpleNamespace(source="cash", group="spend", date=D(2026, 4, 2), amount=40.0, detail="Giving (cash)", sub="Parish"),
            SimpleNamespace(source="cash", group="income", date=D(2026, 4, 2), amount=4000.0, detail="Employer", sub=""),
            SimpleNamespace(source="card", group="spend", date=D(2026, 4, 2), amount=9.0, detail="Shopping", sub="x")]
    out = A.year_summary(rows, cash, 2026)
    assert [(r["source"], r["category"], r["subcategory"], r["tax_note"]) for r in out] == [
        ("card", "Health & Personal", "Medical", "medical"), ("card", "Giving", "Charitable giving", "giving"),
        ("cash", "Giving", "Parish", "giving")]
    med = out[0]
    assert (med["net"], med["gross"], med["refunds"], med["count"]) == (80.0, 100.0, 20.0, 1)
    assert med["months"][2] == 80.0 and sum(med["months"]) == 80.0
    assert out[2]["net"] == 40.0 and out[2]["months"][3] == 40.0
    assert A.tax_note("Financial / Investment", "Insurance") == "insurance" and A.tax_note("Shopping", "Clothing") == ""
