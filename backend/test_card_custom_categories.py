"""
Tests for your own subcategories and details: they join the taxonomy, carry
their behaviour (fixedness, recurring role, rewards bucket) and private ones are
never shown to the AI. Runnable directly (``python3 test_card_custom_categories.py``) or via pytest.
"""

from __future__ import annotations

from datetime import date, timedelta

import card_analytics
import card_rules
import card_taxonomy as T
from card_recurring import Charge, detect

TEAM = T.Custom(1, "Entertainment", "Team support", fixedness="discretionary", role="subscription", rewards="general")
GARDEN = T.Custom(2, "Home", "Garden plot", fixedness="essential", role="bill", rewards="utilities", ai_visible=False)
PASS = T.Custom(3, "Entertainment", "Team support", "Season pass")


def setup_function(_fn):
    T.set_custom([TEAM, GARDEN, PASS])


def teardown_function(_fn):
    T.set_custom([])


def test_custom_subcategories_and_details_join_the_tree():
    assert T.is_valid("Entertainment", "Team support") and T.is_valid("Home", "Garden plot")
    assert not T.is_valid("Travel", "Team support")
    assert T.tree()["Entertainment"]["Team support"] == ["Season pass"]
    assert T.is_builtin("Entertainment", "Sports tickets") and not T.is_builtin("Entertainment", "Team support")
    entry = next(x for x in next(t for t in T.as_list() if t["l1"] == "Entertainment")["l2"] if x["name"] == "Team support")
    assert entry["custom"] and entry["custom_l3"] == ["Season pass"]


def test_private_names_never_reach_the_ai():
    ai = {t["l1"]: [x["name"] for x in t["l2"]] for t in T.as_list(for_ai=True)}
    assert "Garden plot" not in ai["Home"] and "Team support" in ai["Entertainment"]
    assert T.public_l2("Home", "Garden plot") == T.PRIVATE_L2 and T.public_l2("Entertainment", "Team support") == "Team support"
    assert T.public_l3("Home", "Garden plot", "Raised beds") == T.PRIVATE_L3


def test_behaviour_follows_the_attributes():
    assert card_analytics._fixedness("Garden plot", False) == "Essential, variable"
    assert card_analytics._fixedness("Team support", False) == "Discretionary"
    assert card_rules.reward_key("Home", "Garden plot", "", "") == "utilities"
    start = date(2026, 8, 10)
    passes = [Charge(id=i, card_account_id=1, card="Card 1", date=start + timedelta(days=30 * i), spend=50.0,
                  canonical_merchant="Riverside Arena", cat_l1="Entertainment", cat_l2="Team support", cat_l3="Season pass")
           for i in range(2)]
    items, _ = detect(passes, {1: (date(2026, 1, 1), date(2026, 10, 20))})
    assert items and items[0]["kind"] == "subscription"           # role "subscription": two charges are enough


def test_insights_payload_masks_private_subcategories():
    row = card_analytics.Row(id=1, card_account_id=1, card="Lake card", date=date(2026, 9, 1), amount=-120.0,
                             txn_type="purchase", txn_subtype="", is_spend=True, canonical_merchant="Garden Co",
                             merchant_family="Garden Co", cat_l1="Home", cat_l2="Garden plot", cat_l3="Raised beds")
    payload = card_analytics.insights_payload([row], {1: (date(2026, 9, 1), date(2026, 9, 30))},
                                              {1: {"name": "Lake card", "issuer": ""}}, [], {"by_kind": {}}, False)
    text = str(payload)
    assert "Garden plot" not in text and "Raised beds" not in text and T.PRIVATE_L2 in text


if __name__ == "__main__":
    import sys
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            setup_function(fn)
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"FAIL {name}: {exc!r}")
            finally:
                teardown_function(fn)
    sys.exit(1 if failed else 0)
