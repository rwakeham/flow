"""
The one-off refresh of stored charges after the merchant rules changed (card_rules_refresh).
Pure: plan() only; no database.
"""

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")  # models import; never connects

import card_rules_refresh as R  # noqa: E402

AIR = ("Travel", "Airfare", "Other airlines")


def row(i, desc, cat, source="rule", key=None, name=None, pre=None, txn_type="purchase"):
    return R.StoredRow(i, desc, None, key if key is not None else desc.split()[0], name or desc.title(),
                       name or desc.title(), name or desc.title(), cat, source, pre, txn_type)


def test_a_flight_moves_to_its_airlines_detail():
    p = R.plan([row(1, "UNITED AIRLINES 0162345678901", AIR, name="United Airlines")], set())
    assert p.updates == {1: ("Travel", "Airfare", "United")} and p.merchants == {"United Airlines"}
    assert not p.remember


def test_a_merchant_no_rule_knows_any_more_keeps_its_category():
    cat = ("Government & Taxes", "Government fees", "Registration / licensing")
    p = R.plan([row(1, "EXAMPLETOWN DVS OFFICE 123", cat, key="EXAMPLETOWNDVS", name="Exampletown Dvs"),
                row(2, "EXAMPLETOWN DVS OFFICE 456", cat, key="EXAMPLETOWNDVS", name="Exampletown Dvs")], set())
    assert p.updates == {} and p.remember == {"EXAMPLETOWNDVS": ("Exampletown Dvs", "Exampletown Dvs", *cat)}


def test_a_key_the_user_already_set_is_not_overwritten():
    cat = ("Government & Taxes", "Government fees", "Registration / licensing")
    p = R.plan([row(1, "EXAMPLETOWN DVS OFFICE 123", cat, key="EXAMPLETOWNDVS")], {"EXAMPLETOWNDVS"})
    assert not p.remember


def test_unchanged_answers_and_other_sources_are_left_alone():
    same = row(1, "STARBUCKS STORE 1182", ("Food & Beverage", "Dining", "Coffee / cafe"), name="Starbucks")
    ai = row(2, "UNITED AIRLINES 0162345678901", AIR, source="ai")
    mine = row(3, "UNITED AIRLINES 0162345678901", AIR, source="user")
    credit = row(4, "UNITED AIRLINES 0162345678901", AIR, txn_type="benefit_credit")
    p = R.plan([same, ai, mine, credit], set())
    assert p.updates == {} and p.pre_rule_updates == {} and not p.remember


def test_an_amount_rule_row_gets_its_recorded_category_updated():
    store = ("Food & Beverage", "Groceries", "Convenience store")
    r = row(1, "UNITED AIRLINES 0162345678901", store, source="amount_rule", pre="Travel|Airfare|Other airlines|rule")
    p = R.plan([r], set())
    assert p.updates == {} and p.pre_rule_updates == {1: "Travel|Airfare|United|rule"}
