"""
Tests for recurring-charge and subscription detection on card purchases.

Runnable directly (``python3 test_card_recurring.py``) or via pytest.
"""

from __future__ import annotations

from datetime import date, timedelta

from card_recurring import Charge, ManualSeries, detect, totals

START = date(2026, 1, 5)
END = date(2026, 10, 20)
COV = {1: (START, END), 2: (START, END)}
_ids = iter(range(1, 100000))


def charges(merchant, amounts, step_days, l1="Digital & Media", l2="Streaming", card=1, start=START,
            hint=False, installment=False):
    return [Charge(id=next(_ids), card_account_id=card, card=f"Card {card}", date=start + timedelta(days=i * step_days),
                   spend=a, canonical_merchant=merchant, cat_l1=l1, cat_l2=l2, cat_l3=l2,
                   subscription_hint=hint, installment_flag=installment)
            for i, a in enumerate(amounts)]


def kinds(items):
    return {(it["merchant"], it["card_account_id"]): it for it in items}


def test_subscription_bill_service_habitual():
    data = (charges("Netflix", [15.49] * 9, 30, start=END - timedelta(days=8 * 30))
            + charges("Metro Power Co", [120, 95, 140, 160, 110, 90], 30, l1="Bills & Utilities", l2="Utilities",
                      start=END - timedelta(days=5 * 30))
            + charges("Cleaner", [100, 120, 90, 110], 14, l1="Home", l2="Contractors", start=END - timedelta(days=42))
            + charges("Starbucks", [5.5, 6.1, 4.9, 7.2, 5.5, 6.0, 5.1], 3, l1="Food & Beverage", l2="Dining",
                      start=END - timedelta(days=20)))
    items, ids = detect(data, COV)
    k = kinds(items)
    assert k[("Netflix", 1)]["kind"] == "subscription"
    assert k[("Netflix", 1)]["cadence"] == "monthly"
    assert k[("Metro Power Co", 1)]["kind"] == "recurring bill"
    assert k[("Cleaner", 1)]["kind"] == "recurring bill"           # contractors are a bill category
    assert k[("Starbucks", 1)]["kind"] == "habitual"
    assert k[("Starbucks", 1)]["annualized_run_rate"] is None
    assert all(ids[c.id] == "subscription" for c in data if c.canonical_merchant == "Netflix")
    t = totals(items)
    assert t["core_monthly_recurring"] >= 15.49 + 100


def test_price_change_and_ended():
    data = charges("Spotify", [10.99, 10.99, 10.99, 11.99], 30, start=date(2026, 3, 1))
    items, _ = detect(data, COV)
    it = kinds(items)[("Spotify", 1)]
    assert "price change 10.99 -> 11.99" in it["change_detected"]
    assert it["status"] == "ended" and it["ended_by"] == "auto"   # last charge ~June, coverage to October


def _monthly(last_charge, merchant="Mathnasium", card=1):
    return charges(merchant, [350.0] * 7, 30, l1="Education / Family", l2="Tutoring", card=card,
                   start=last_charge - timedelta(days=6 * 30))


def test_overdue_still_counts_and_ended_does_not():
    on_time = _monthly(END - timedelta(days=20))
    overdue = _monthly(END - timedelta(days=52))                    # 1.6 to 2.5 periods: about 48 to 75 days
    gone = _monthly(END - timedelta(days=80))
    s = {name: detect(d, COV)[0][0] for name, d in [("on_time", on_time), ("overdue", overdue), ("gone", gone)]}
    assert [s[k]["status"] for k in ("on_time", "overdue", "gone")] == ["active", "overdue", "ended"]
    assert "overdue: no charge in 52d" in s["overdue"]["change_detected"]
    assert totals([s["overdue"]])["core_monthly_recurring"] == 350.0
    assert totals([s["gone"]])["core_monthly_recurring"] == 0.0


def test_gaps_are_measured_to_the_cards_data_not_today():
    data = _monthly(date(2026, 3, 1))
    assert detect(data, {1: (START, date(2026, 3, 20))})[0][0]["status"] == "active"


def test_marked_ended_then_restarted():
    data = _monthly(END - timedelta(days=10))
    last = max(c.date for c in data)
    item = detect(data, COV, {("Mathnasium", 1): "confirmed"}, {("Mathnasium", 1): last})[0][0]
    assert item["status"] == "ended" and item["ended_by"] == "you" and item["verdict"] == "confirmed"
    assert totals([item])["core_monthly_recurring"] == 0.0
    again = data + charges("Mathnasium", [350.0], 30, l1="Education / Family", l2="Tutoring", start=END - timedelta(days=2))
    item = detect(again, COV, {}, {("Mathnasium", 1): last})[0][0]
    assert item["status"] == "active" and "restarted" in item["change_detected"]


def test_an_ended_series_is_not_a_duplicate_of_a_running_one():
    data = _monthly(END - timedelta(days=5), card=1) + _monthly(END - timedelta(days=100), card=2)
    items, _ = detect(data, COV)
    assert all(it["notes"] == "" for it in items)
    assert [it["status"] for it in items] == ["active", "ended"]       # ended ones sort last


def test_duplicate_subscription_across_cards():
    data = (charges("Hulu", [17.99] * 4, 30, start=END - timedelta(days=90))
            + charges("Hulu", [17.99] * 4, 30, card=2, start=END - timedelta(days=95)))
    items, _ = detect(data, COV)
    assert all("duplicate subscription" in it["notes"] for it in items if it["merchant"] == "Hulu")


def test_hint_catches_a_new_subscription_after_two_charges():
    shop = dict(l1="Shopping", l2="Specialty retail", start=END - timedelta(days=30))
    plain, _ = detect(charges("New App", [9.99, 9.99], 30, **shop), COV)
    hinted, _ = detect(charges("New App", [9.99, 9.99], 30, hint=True, **shop), COV)
    assert not plain
    assert hinted and hinted[0]["kind"] == "subscription"
    # Memberships, streaming, software and the like are subscriptions by nature: no hint needed.
    prime, _ = detect(charges("Amazon Prime", [16.27, 16.27], 30, l1="Shopping", l2="Memberships",
                              start=END - timedelta(days=30)), COV)
    assert prime and prime[0]["kind"] == "subscription"


def test_a_move_after_one_charge_on_the_old_card():
    """Prime: one charge on Airline Miles (Aug 20), then Sep 20 and Oct 20 on Everyday Visa: one series of three."""
    cov = {1: (START, date(2026, 10, 22)), 2: (START, date(2026, 10, 22))}
    data = (charges("Amazon Prime", [16.27], 30, l1="Shopping", l2="Memberships", card=1, start=date(2026, 8, 20))
            + charges("Amazon Prime", [16.27] * 2, 30, l1="Shopping", l2="Memberships", card=2, start=date(2026, 9, 20)))
    items, _ = detect(data, cov)
    assert [(it["card_account_id"], it["occurrences"], it["kind"], it["status"]) for it in items] == [(2, 3, "subscription", "active")]


def test_a_manual_series_matches_the_parent_brand():
    prime = [Charge(id=next(_ids), card_account_id=1, card="Card 1", date=date(2026, 8, 20) + timedelta(days=30 * i),
                    spend=16.27, canonical_merchant="Amazon Prime", cat_l1="Shopping", cat_l2="Memberships",
                    cat_l3="Amazon Prime", merchant_family="Amazon") for i in range(3)]
    m = ManualSeries(id=9, name="Prime", merchant="Amazon", cadence="monthly", kind="subscription",
                     amount_min=16.0, amount_max=17.0)
    it = next(x for x in detect(prime, COV, manual=[m])[0] if x["manual_id"] == 9)
    assert it["occurrences"] == 3 and it["status"] != "expected"


def test_installments_and_verdicts():
    inst, _ = detect(charges("Affirm Peloton", [49.0, 49.0], 30, l1="Shopping", l2="Specialty retail",
                             installment=True), COV)
    assert inst[0]["kind"] == "installment" and inst[0]["annualized_run_rate"] is None
    data = charges("Netflix", [15.49] * 4, 30, start=END - timedelta(days=90))
    assert not detect(data, COV, {("Netflix", 0): "dismissed"})[0]
    assert not detect(data, COV, {("Netflix", 1): "dismissed"})[0]
    assert detect(data, COV, {("Netflix", 2): "dismissed"})[0]               # another card's verdict


def test_one_charge_is_never_recurring_even_if_confirmed():
    """A single $1,115.77 ticket purchase is not a subscription, and is not spread across months."""
    tickets = charges("Ticket Hub", [1115.77], 30, l1="Entertainment", l2="Sports tickets", card=2,
                      start=date(2026, 5, 2))
    assert not detect(tickets, COV)[0]
    assert not detect(tickets, COV, {("Ticket Hub", 0): "confirmed"})[0]
    assert not detect(tickets, COV, {("Ticket Hub", 2): "confirmed"})[0]


def test_same_merchant_on_two_cards_is_judged_and_marked_separately():
    monthly = charges("Ticket Hub", [700.0] * 5, 30, l1="Entertainment", l2="Sports tickets", card=1,
                      start=date(2026, 5, 30))
    one_off = charges("Ticket Hub", [1115.77], 30, l1="Entertainment", l2="Sports tickets", card=2,
                      start=date(2026, 5, 2))
    items, _ = detect(monthly + one_off, COV, {("Ticket Hub", 0): "confirmed"})
    assert [(it["card_account_id"], it["kind"], it["monthly_equivalent"]) for it in items] == [(1, "subscription", 700.0)]
    assert items[0]["notes"] == ""                                         # no false "duplicate on two cards"
    # Two repeating series: dismissing one card leaves the other, confirmed.
    both = monthly + charges("Ticket Hub", [700.0] * 5, 30, l1="Entertainment", l2="Sports tickets", card=2,
                             start=date(2026, 5, 30))
    items, _ = detect(both, COV, {("Ticket Hub", 0): "confirmed", ("Ticket Hub", 2): "dismissed"})
    assert [(it["card_account_id"], it["verdict"]) for it in items] == [(1, "confirmed")]


def test_confirmed_irregular_series_uses_its_gap_not_coverage():
    data = charges("Club Dues", [120.0, 120.0], 45, l1="Services", l2="Memberships", start=date(2026, 3, 1))
    assert not detect(data, COV)[0]                                         # 45 days: no known cadence
    items, _ = detect(data, COV, {("Club Dues", 1): "confirmed"})
    assert items[0]["kind"] == "subscription" and items[0]["monthly_equivalent"] == round(120 * 30.4 / 45, 2)


def test_a_subscription_that_moves_cards_is_one_series():
    """Prime on Airline Miles through Aug 20, then on Everyday Visa from Sep 20: one series, on Everyday Visa."""
    cov = {1: (START, date(2026, 10, 22)), 2: (START, date(2026, 10, 22))}
    miles = charges("Amazon Prime", [16.27] * 6, 30, l1="Shopping", l2="Memberships", card=1, start=date(2026, 8, 20) - timedelta(days=150))
    everyday = charges("Amazon Prime", [16.27] * 2, 30, l1="Shopping", l2="Memberships", card=2, start=date(2026, 9, 20))
    items, kinds_by_id = detect(miles + everyday, cov, card_names={1: "Airline Miles", 2: "Everyday Visa"})
    assert len(items) == 1
    it = items[0]
    assert (it["card_account_id"], it["card"], it["status"], it["occurrences"]) == (2, "Everyday Visa", "active", 8)
    assert it["moved_from"] == ["Airline Miles"] and "moved from Airline Miles on 2026-09-20" in it["change_detected"]
    assert it["notes"] == "" and totals(items)["core_monthly_recurring"] == 16.27
    assert all(kinds_by_id[c.id] == "subscription" for c in miles + everyday)


def test_moves_need_matching_amount_and_timing():
    cov = {1: (START, END), 2: (START, END)}
    a = charges("Gym", [50.0] * 4, 30, l1="Health & Personal", l2="Fitness", card=1, start=date(2026, 3, 1))
    pricier = charges("Gym", [80.0] * 3, 30, l1="Health & Personal", l2="Fitness", card=2, start=date(2026, 6, 29))
    later = charges("Gym", [50.0] * 3, 30, l1="Health & Personal", l2="Fitness", card=2, start=date(2026, 8, 20))
    assert len(detect(a + pricier, cov)[0]) == 2
    assert len(detect(a + later, cov)[0]) == 2
    assert not any(it["moved_from"] for it in detect(a + later, cov)[0])


def _amazon_mix():
    shopping = [Charge(id=next(_ids), card_account_id=1, card="Card 1", date=START + timedelta(days=d), spend=amt,
                       canonical_merchant="Amazon", cat_l1="Shopping", cat_l2="General retail / online",
                       cat_l3="Amazon") for d, amt in [(3, 37.95), (9, 116.89), (16, 38.30), (24, 88.0), (31, 66.19),
                                                         (40, 17.04), (47, 103.09), (55, 91.28), (61, 43.39)]]
    kindle = charges("Amazon", [11.99] * 6, 30, l1="Shopping", l2="General retail / online", start=date(2026, 5, 10))
    return shopping, kindle


def test_a_manual_series_splits_a_subscription_out_of_shopping():
    shopping, kindle = _amazon_mix()
    m = ManualSeries(id=1, name="Kindle Unlimited", merchant="amazon", cadence="monthly", kind="subscription",
                     amount_min=11.0, amount_max=13.0)
    items, ids = detect(shopping + kindle, COV, manual=[m])
    k = next(it for it in items if it["manual_id"] == 1)
    assert (k["label"], k["occurrences"], k["monthly_equivalent"], k["verdict"]) == ("Kindle Unlimited", 6, 11.99, "confirmed")
    assert all(ids[c.id] == "subscription" for c in kindle)
    assert all(ids.get(c.id) != "subscription" for c in shopping)
    shopping_items = [it for it in items if it["manual_id"] is None]
    assert all(it["occurrences"] == len(shopping) for it in shopping_items)    # Kindle charges left the Amazon series


def test_a_manual_series_with_no_charges_is_expected_and_not_counted():
    m = ManualSeries(id=2, name="Annual domain", merchant="Namecheap", cadence="annual", kind="subscription",
                     amount_min=10.0, amount_max=14.0)
    it = detect([], COV, manual=[m])[0][0]
    assert it["status"] == "expected" and it["occurrences"] == 0 and it["typical_amount"] == 12.0
    assert totals([it])["core_monthly_recurring"] == 0.0


def test_a_manual_series_can_be_ended():
    _, kindle = _amazon_mix()
    last = max(c.date for c in kindle)
    m = ManualSeries(id=3, name="Kindle Unlimited", merchant="Amazon", cadence="monthly", kind="subscription",
                     amount_min=11.0, amount_max=13.0, ended_through=last)
    it = detect(kindle, COV, manual=[m])[0][0]
    assert it["status"] == "ended" and it["ended_by"] == "you"


def test_same_trip_repeats_are_not_cadence():
    """Charges two days apart (one trip) do not make a weekly pattern."""
    data = charges("Hotel X", [200, 200, 200], 1, l1="Travel", l2="Lodging")
    assert not detect(data, COV)[0]


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
