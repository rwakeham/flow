"""
Tests for editing a merchant's name and category: merchant-wide edits keep the
rows the user changed on their own and remember the answer for every
statement spelling; single-row edits mark the row as set individually.
Runnable directly (``python3 test_card_merchant_edit.py``) or via pytest.
"""

from __future__ import annotations

import os
from datetime import date

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")  # models import; never connects
os.environ.setdefault("SECRET_KEY", "test-" * 8)

import card_analytics as A  # noqa: E402
from card_analytics import Row  # noqa: E402
from models import CardTransaction, MerchantCategory  # noqa: E402
from routers.cards import _individual, apply_merchant_edit  # noqa: E402

LODGING = ("Travel", "Lodging", "Other hotels")


class FakeDB:
    def __init__(self):
        self.cache = {}

    def get(self, model, key):
        assert model is MerchantCategory
        return self.cache.get(key)

    def add(self, obj):
        self.cache[obj.merchant_key] = obj


def txn(i, key, name="Stone Creek", source="ai", renamed=False):
    return CardTransaction(id=i, card_account_id=1, date=date(2026, 7, i), raw_description="STONE CREEK EXAMPLETOWN CA",
                           merchant_key=key, merchant_label="Stone Creek", canonical_merchant=name, merchant_family=name,
                           amount=-400.0, txn_type="purchase", is_spend=True, cat_l1="Food & Beverage",
                           cat_l2="Alcohol / Wine", cat_l3="Direct winery / wine club", category_source=source,
                           name_set_by_user=renamed)


def test_merchant_wide_edit_renames_recategorizes_and_remembers_every_spelling():
    db = FakeDB()
    rows = [txn(1, "STONECREEK"), txn(2, "STONECREEK"), txn(3, "STONECREEKINN")]
    out = apply_merchant_edit(db, rows, "Stone Creek Inn", "Stone Creek Inn", LODGING, False)
    assert out == {"updated": 3, "kept_user_overrides": 0}
    assert {(t.canonical_merchant, t.cat_l2, t.category_source) for t in rows} == {("Stone Creek Inn", "Lodging", "merchant")}
    assert sorted(db.cache) == ["STONECREEK", "STONECREEKINN"]
    assert all(c.source == "user" and c.canonical_merchant == "Stone Creek Inn" for c in db.cache.values())


def test_rows_set_individually_are_kept_unless_overwritten():
    db = FakeDB()
    mine = txn(2, "STONECREEK", source="user")
    renamed = txn(3, "STONECREEK", source="user", renamed=True)
    rows = [txn(1, "STONECREEK"), mine, renamed]
    out = apply_merchant_edit(db, rows, "Stone Creek Inn", "", LODGING, False, focus_id=1)
    assert out == {"updated": 1, "kept_user_overrides": 2} and mine.cat_l2 == "Alcohol / Wine"
    out = apply_merchant_edit(db, rows, "Stone Creek Inn", "Stone Creek Inn", LODGING, True)
    assert out["updated"] == 3 and not renamed.name_set_by_user and not _individual(renamed)


def test_the_focus_row_is_always_changed():
    db = FakeDB()
    focus = txn(1, "STONECREEK", source="user")
    apply_merchant_edit(db, [focus], "Stone Creek Inn", "Stone Creek Inn", LODGING, False, focus_id=1)
    assert focus.canonical_merchant == "Stone Creek Inn"


def test_amount_rules_follow_a_rename_and_category_rules_are_left_alone(monkeypatch):
    import routers.cards as C
    from models import CardAmountRule

    mine = CardAmountRule(id=1, merchant="Circle K", amount_max=20, cat_l1="Food & Beverage", cat_l2="Groceries", cat_l3="")
    other = CardAmountRule(id=2, merchant="Shell", amount_max=20, cat_l1="Food & Beverage", cat_l2="Groceries", cat_l3="")
    fuel = CardAmountRule(id=3, merchant=None, scope_l1="Transportation", scope_l2="Fuel", amount_max=20,
                          cat_l1="Food & Beverage", cat_l2="Groceries", cat_l3="")

    class RulesDB:
        def query(self, model):
            assert model is CardAmountRule
            return self

        def all(self):
            return [mine, other, fuel]

        def flush(self):
            pass

    applied = []
    monkeypatch.setattr(C.card_amount_rules, "apply", lambda db, merchants: applied.append(merchants))
    C._rules_follow(RulesDB(), {"CIRCLE K"}, "Circle K Store")
    assert (mine.merchant, other.merchant, fuel.merchant) == ("Circle K Store", "Shell", None)
    assert applied == [{"CIRCLE K", "Circle K Store"}]


def _row(i, merchant, amount, d, card=1, l2="Lodging"):
    return Row(id=i, card_account_id=card, card=f"Card {card}", date=d, amount=amount, txn_type="purchase",
               txn_subtype="", is_spend=True, canonical_merchant=merchant, merchant_family=merchant,
               cat_l1="Travel", cat_l2=l2, cat_l3=l2)


def test_merchant_detail():
    rows = [_row(1, "Stone Creek Inn", -400.0, date(2026, 7, 3)), _row(2, "Stone Creek Inn", -250.0, date(2026, 9, 1), card=2),
            _row(3, "United", -600.0, date(2026, 8, 9), l2="Airfare")]
    d = A.merchant_detail(rows, "Stone Creek Inn")
    assert d["net_spend"] == 650.0 and round(d["share_of_all"], 3) == round(650 / 1250, 3)
    assert d["by_month"] == [{"month": "2026-07", "net": 400.0}, {"month": "2026-08", "net": 0.0}, {"month": "2026-09", "net": 250.0}]
    assert (d["first"], d["last"], d["cat_l2"]) == ("2026-07-03", "2026-09-01", "Lodging")
    assert d["by_card"] == {"Card 1": 400.0, "Card 2": 250.0}
    assert A.merchant_detail(rows, "Nobody")["purchases"] == 0
    # Charges set by amount rules don't decide the merchant's own category, even when they are most of them.
    snacks = [Row(id=10 + i, card_account_id=1, card="Card 1", date=date(2026, 9, 1 + i), amount=-4.0, txn_type="purchase",
                  txn_subtype="", is_spend=True, canonical_merchant="Circle K", merchant_family="Circle K",
                  cat_l1="Food & Beverage", cat_l2="Groceries", cat_l3="Convenience store", category_source="amount_rule")
              for i in range(3)]
    fuel = [Row(id=20, card_account_id=1, card="Card 1", date=date(2026, 9, 9), amount=-40.0, txn_type="purchase",
                txn_subtype="", is_spend=True, canonical_merchant="Circle K", merchant_family="Circle K",
                cat_l1="Transportation", cat_l2="Fuel", cat_l3="Gas station", category_source="rule")]
    assert A.merchant_detail(snacks + fuel, "Circle K")["cat_l2"] == "Fuel"


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
