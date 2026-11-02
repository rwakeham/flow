"""
Tests for amount rules: matching by size, non-overlapping ranges, and the
category changes they call for (rows set individually are never touched;
rows a rule no longer covers go back to the merchant's category).
Runnable directly (``python3 test_card_amount_rules.py``) or via pytest.
"""

from __future__ import annotations

import types

from card_amount_rules import SOURCE, AmountRule, in_range, overlaps, plan, rule_for

STORE = ("Food & Beverage", "Groceries", "Convenience store")
FUEL = ("Transportation", "Fuel", "Gas station")
WASH = ("Transportation", "Auto purchase / repair / maintenance", "Car wash")
RULES = [AmountRule(1, "Circle K", None, 20.0, *STORE), AmountRule(2, "Circle K", 90.0, None, *WASH)]


def row(amount, source="rule", cat=FUEL, merchant="Circle K"):
    return types.SimpleNamespace(canonical_merchant=merchant, amount=-amount, category_source=source,
                                 cat_l1=cat[0], cat_l2=cat[1], cat_l3=cat[2])


def test_ranges_include_the_lower_bound_and_exclude_the_upper():
    assert in_range(19.99, None, 20.0) and not in_range(20.0, None, 20.0)
    assert in_range(90.0, 90.0, None) and not in_range(89.99, 90.0, None)


def test_overlap():
    assert not overlaps(None, 20, 20, 90) and not overlaps(20, 90, 90, None)
    assert overlaps(None, 20, 19, 30) and overlaps(None, None, 5, 6) and overlaps(10, 50, 20, 30)


def test_rule_for_picks_by_merchant_and_size():
    assert rule_for(RULES, "circle k", 4.49).id == 1
    assert rule_for(RULES, "Circle K", 38.80) is None
    assert rule_for(RULES, "Circle K", 112.99).id == 2
    assert rule_for(RULES, "Shell", 4.49) is None
    assert rule_for(RULES, "Circle K", -4.49).id == 1          # refunds match by size


def test_plan_moves_small_and_large_charges_and_keeps_yours():
    small, fill, big, mine = row(2.99), row(38.80), row(112.99), row(6.99, source="user", cat=FUEL)
    changes = {id(t): (cat, src) for t, cat, src in plan([small, fill, big, mine], RULES, lambda t: (*FUEL, "rule"))}
    assert changes == {id(small): (STORE, SOURCE), id(big): (WASH, SOURCE)}


def test_a_charge_a_rule_no_longer_covers_goes_back_to_the_merchant_category():
    was = row(25.0, source=SOURCE, cat=STORE)
    (t, cat, src), = plan([was], RULES, lambda t: (*FUEL, "merchant"))
    assert cat == FUEL and src == "merchant"
    assert plan([row(4.0, source=SOURCE, cat=STORE)], RULES, lambda t: (*FUEL, "rule")) == []   # already right


FUEL_RULE = AmountRule(3, "", None, 20.0, *STORE, scope_l1="Transportation", scope_l2="Fuel")


def test_a_category_rule_covers_every_merchant_in_it():
    shell, exxon, groceries = row(5.49, merchant="Shell"), row(42.0, merchant="Exxon"), row(8.0, merchant="Kroger", cat=STORE)
    changes = {id(t): cat for t, cat, _src in plan([shell, exxon, groceries], [FUEL_RULE], lambda t: (*FUEL, "rule"))}
    assert changes == {id(shell): STORE}


def test_a_merchant_rule_wins_over_a_category_rule():
    rules = [FUEL_RULE, AmountRule(4, "Circle K", None, 10.0, *WASH)]
    assert rule_for(rules, "Circle K", 4.0, ("Transportation", "Fuel")).id == 4
    assert rule_for(rules, "Circle K", 15.0, ("Transportation", "Fuel")).id == 3   # outside its own: the category's
    assert rule_for(rules, "Shell", 4.0, ("Transportation", "Fuel")).id == 3


def test_category_rules_match_the_category_before_any_rule():
    """A snack charge already moved to Convenience store still counts as fuel, so it stays put."""
    moved = row(4.0, source=SOURCE, cat=STORE, merchant="Shell")
    assert plan([moved], [FUEL_RULE], lambda t: (*FUEL, "rule")) == []
    # ... and goes back to fuel when the rule goes away.
    (t, cat, src), = plan([moved], [], lambda t: (*FUEL, "rule"))
    assert cat == FUEL


def test_whole_l1_scope_and_same_scope():
    any_transport = AmountRule(5, "", 100.0, None, *WASH, scope_l1="Transportation")
    assert any_transport.covers_category("Transportation", "Parking / tolls")
    assert not FUEL_RULE.covers_category("Transportation", "Parking / tolls")
    assert FUEL_RULE.same_scope(AmountRule(6, "", 50.0, None, *WASH, scope_l1="Transportation", scope_l2="Fuel"))
    assert not FUEL_RULE.same_scope(any_transport) and not FUEL_RULE.same_scope(RULES[0])


def test_the_pre_rule_category_is_read_back():
    from card_amount_rules import stored_base
    t = types.SimpleNamespace(pre_rule_category="Transportation|Fuel|Gas station|rule")
    assert stored_base(t) == ("Transportation", "Fuel", "Gas station", "rule")
    assert stored_base(types.SimpleNamespace(pre_rule_category=None)) is None


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
