"""
Tests for renaming built-in subcategories and details, and for the Assistant's
rename / move / new-detail proposals and category lookups.

A renamed built-in is stored and shown under its new name, the rules' answers
follow it, and behaviour keyed on built-in names (rewards, analytics, recurring
detection) keeps working through the original name. No database or network.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")  # models import; never connects
os.environ.setdefault("SECRET_KEY", "test-" * 8)

import ask_data  # noqa: E402
import ask_proposals as P  # noqa: E402
import card_analytics  # noqa: E402
import card_classifier  # noqa: E402
import card_rules  # noqa: E402
import card_taxonomy as T  # noqa: E402
from test_ask import TODAY, fixture, row  # noqa: E402

F = "Food & Beverage"


def renamed(items):
    """Run a test body with these renames, then clear them."""
    class _Ctx:
        def __enter__(self):
            T.set_renames(items)

        def __exit__(self, *exc):
            T.set_renames({})
    return _Ctx()


def classify(desc: str):
    key = "".join(ch for ch in desc.upper() if ch.isalnum())
    return card_classifier.classify_transaction(desc, -25.0, "", "", "Card", key, desc, {})


# ── The rename layer ─────────────────────────────────────────────────────────

def test_detail_rename_follows_rules_and_maps_back():
    before = classify("RIVERSIDE BREWING CO SPRINGFIELD IL")
    assert (before.cat_l2, before.cat_l3) == ("Alcohol / Wine", "Brewery / distillery / winery")
    with renamed({(F, "Alcohol / Wine", "Brewery / distillery / winery"): "Craft drinks"}):
        after = classify("RIVERSIDE BREWING CO SPRINGFIELD IL")
        assert (after.cat_l1, after.cat_l2, after.cat_l3) == (F, "Alcohol / Wine", "Craft drinks")
        assert T.base(F, "Alcohol / Wine", "Craft drinks") == (F, "Alcohol / Wine", "Brewery / distillery / winery")
        assert "Craft drinks" in T.tree()[F]["Alcohol / Wine"]
        assert "Brewery / distillery / winery" not in T.tree()[F]["Alcohol / Wine"]
        assert T.is_builtin(F, "Alcohol / Wine", "Craft drinks")
    assert T.tree()[F]["Alcohol / Wine"].count("Brewery / distillery / winery") == 1


def test_subcategory_rename_keeps_behaviour():
    with renamed({(F, "Dining", ""): "Eating out"}):
        r = classify("CHIPOTLE 1234 OAK GROVE IL")
        assert r.cat_l2 == "Eating out"
        assert T.is_valid(F, "Eating out") and not T.is_valid(F, "Dining")
        assert T.is_builtin(F, "Eating out")
        # A detail that mirrors its subcategory follows it, both ways.
        assert T.current(F, "Dining", "Dining") == (F, "Eating out", "Eating out")
        assert T.base(F, "Eating out", "Eating out") == (F, "Dining", "Dining")
        assert T.current(F, "Dining", "Delivery") == (F, "Eating out", "Delivery")
        # Rewards, analytics and the trim list still see Dining.
        assert card_rules.reward_key(F, "Eating out", "Delivery", "DoorDash") == "dining_delivery"
        assert card_rules.reward_key(F, "Eating out", "Eating out", "Chipotle") == "dining"
        rows = [row(TODAY - timedelta(days=3), -40.0, "Chipotle", F, "Eating out"),
                row(TODAY - timedelta(days=4), -20.0, "DoorDash", F, "Eating out", "Delivery"),
                row(TODAY - timedelta(days=5), -60.0, "Kroger", F, "Groceries", "Traditional grocery")]
        food = card_analytics.food(rows)
        assert food["dining"] == 60.0 and food["groceries"] == 60.0 and food["delivery_share_of_dining"] == 20.0 / 60.0
        assert card_analytics._fixedness("Eating out", False) == card_analytics._fixedness("Dining", False)


def test_renames_are_scoped_to_their_category():
    with renamed({("Travel", "Parking / tolls", ""): "Tolls"}):
        assert T.current("Transportation", "Parking / tolls") == ("Transportation", "Parking / tolls", "")
        assert T.base("Transportation", "Tolls") == ("Transportation", "Tolls", "")


# ── Proposals ────────────────────────────────────────────────────────────────

def test_rename_proposal_for_a_built_in():
    data, state = fixture(), P.State()
    _, card = P.run("propose_rename_category", {"category": "Digital & Media", "subcategory": "Streaming", "detail": "",
                                                 "new_name": "TV & streaming", "reason": "x"}, data, state)
    assert card["apply"] == {"method": "POST", "url": "/api/cards/categories/rename",
                             "body": {"l1": "Digital & Media", "l2": "Streaming", "l3": "", "new_name": "TV & streaming"}}
    text = " ".join(card["lines"])
    assert "6 charges" in text and "Built-in" in text
    # The new name is usable later in the same answer.
    _, m = P.run("propose_merchant_category", {"merchant": "Netflix", "new_name": "", "category": "Digital & Media",
                                               "subcategory": "TV & streaming", "detail": "", "reason": "x"}, data, state)
    assert m["apply"]["body"]["cat_l2"] == "TV & streaming"


def test_rename_proposal_refusals():
    data = fixture()
    bad = [
        {"category": "Uncategorized", "subcategory": "Needs review", "detail": "", "new_name": "Later"},
        {"category": F, "subcategory": "Nope", "detail": "", "new_name": "X"},
        {"category": F, "subcategory": "Dining", "detail": "", "new_name": "Groceries"},          # clash
        {"category": F, "subcategory": "Dining", "detail": "", "new_name": "Dining"},             # same
        {"category": F, "subcategory": "Dining", "detail": "Nope", "new_name": "X"},
        {"category": F, "subcategory": "Dining", "detail": "", "new_name": "a|b"},
    ]
    for a in bad:
        try:
            P.run("propose_rename_category", {**a, "reason": ""}, data, P.State())
            assert False, a
        except P.ProposalError:
            pass


def test_move_proposal_is_one_card():
    data, state = fixture(), P.State()
    _, card = P.run("propose_move_category", {"from_category": F, "from_subcategory": "Dining", "from_detail": "Delivery",
                                              "to_category": F, "to_subcategory": "Groceries", "to_detail": "",
                                              "reason": "x"}, data, state)
    assert card["apply"]["url"] == "/api/cards/categories/move"
    assert card["apply"]["body"] == {"from_l1": F, "from_l2": "Dining", "from_l3": "Delivery",
                                     "to_l1": F, "to_l2": "Groceries", "to_l3": ""}
    assert "Moves 6 charges ($231.00) at 1 merchant" in " ".join(card["lines"])
    for a in ({"from_subcategory": "Lodging", "from_category": "Travel"},            # nothing there
              {"to_subcategory": "Nope"},
              {"to_detail": "Made-up detail"}):
        args = {"from_category": F, "from_subcategory": "Dining", "from_detail": "", "to_category": F,
                "to_subcategory": "Groceries", "to_detail": "", "reason": "", **a}
        try:
            P.run("propose_move_category", args, data, P.State())
            assert False, a
        except P.ProposalError:
            pass


def test_details_must_exist_or_be_proposed():
    data, state = fixture(), P.State()
    args = {"merchant": "Kroger", "new_name": "", "category": F, "subcategory": "Groceries",
            "detail": "Bulk club", "reason": ""}
    try:
        P.run("propose_merchant_category", args, data, state)
        assert False
    except P.ProposalError as exc:
        assert "propose_new_detail" in str(exc)
    _, d = P.run("propose_new_detail", {"category": F, "subcategory": "Groceries", "name": "Bulk club", "reason": ""},
                 data, state)
    assert d["apply"]["body"] == {"l1": F, "l2": "Groceries", "l3": "Bulk club"}
    _, m = P.run("propose_merchant_category", args, data, state)
    assert m["apply"]["body"]["cat_l3"] == "Bulk club"
    # Existing details, and details already on charges, are fine.
    P.run("propose_merchant_category", {**args, "detail": "Traditional grocery"}, data, P.State())
    try:
        P.run("propose_new_detail", {"category": F, "subcategory": "Groceries", "name": "Traditional grocery",
                                     "reason": ""}, data, P.State())
        assert False
    except P.ProposalError:
        pass


# ── Lookups and index ────────────────────────────────────────────────────────

def test_categories_lookup_and_detail_grouping():
    data = fixture()
    text, _ = ask_data.run("categories", {"category": ""}, data)
    top = json.loads(text)
    assert len(top["categories"]) == len(T.CHOOSABLE_L1) == 16
    text, _ = ask_data.run("categories", {"category": "food & beverage"}, data)
    food = json.loads(text)
    dining = next(s for s in food["subcategories"] if s["subcategory"] == "Dining")
    assert dining["built_in"] and dining["charges"] == 6
    assert next(d for d in dining["details"] if d["detail"] == "Delivery")["charges"] == 6
    with renamed({(F, "Dining", ""): "Eating out"}):
        text, _ = ask_data.run("categories", {"category": F}, data)
        eating = next(s for s in json.loads(text)["subcategories"] if s["subcategory"] == "Eating out")
        assert eating["original_name"] == "Dining"
    text, _ = ask_data.run("spending_breakdown", {"group_by": "detail", "months": 0, "category": "Dining",
                                                  "merchant": "", "card": ""}, data)
    groups = {g["group"] for g in json.loads(text)["rows"]}
    assert groups == {"Food & Beverage > Dining > Delivery"}
    idx = ask_data.build_index(data, None)
    assert idx["category_list"]["top_level_categories"] == 16


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
