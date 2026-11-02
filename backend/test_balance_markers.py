"""
Paste markers: teaching Flow where an account's balance sits on a pasted summary.

Pure (balance_sheet); synthetic pages. The account-summary page is the one in
test_balance_sheet.py; the others are layouts the anchored reader can't read.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-" * 8)

import pytest  # noqa: E402

import balance_sheet as bs  # noqa: E402
from test_balance_sheet import PAGE  # noqa: E402

BROKER = """Your accounts
Brokerage Account
Value
$25,000.00
Day change +$120.00
Retirement (IRA) X4321
Today's change -$50.00
Total value $80,000.00
Total of all accounts $105,000.00"""

CARD = """Rewards Card ending in 1111
Statement balance $500.00
Current balance $650.00
Minimum payment due $35.00"""


def _lines(text):
    return [l["text"] for l in bs.paste_lines(text)]


def _pos(text, line_contains, amount):
    """(line index, amount start) for an amount on the first line containing a phrase."""
    for i, l in enumerate(bs.paste_lines(text)):
        if line_contains in l["text"]:
            for a in l["amounts"]:
                if abs(a["value"] - amount) < 0.005:
                    return i, a["start"]
    raise AssertionError((line_contains, amount))


def _idx(text, contains):
    return next(i for i, l in enumerate(_lines(text)) if contains in l)


def test_layout_without_account_numbers_is_taught_by_example():
    local = bs.extract(BROKER).items                            # the anchored reader misses the brokerage
    assert [(i.last4, i.amount) for i in local] == [("4321", -50.0)]   # and misreads the IRA as its day change
    al, ast = _pos(BROKER, "$25,000.00", 25000)
    m = bs.marker_from_example(BROKER, 7, _idx(BROKER, "Brokerage Account"), al, ast)
    assert (m.label_text, m.last4, m.amount_rule) == ("Brokerage Account", None, "first_after")
    items = bs.read_with_markers(BROKER, [m])
    assert [(i.account_id, i.amount, i.source) for i in items] == [(7, 25000.0, "marker")]


def test_second_amount_needs_a_phrase_rule():
    al, ast = _pos(BROKER, "Total value", 80000)
    m = bs.marker_from_example(BROKER, 8, _idx(BROKER, "Retirement (IRA)"), al, ast)
    assert m.amount_rule == "on_line_with" and m.amount_label == "Total value" and m.last4 == "4321"
    assert m.label_text == "Retirement (IRA)"
    assert bs.read_with_markers(BROKER, [m])[0].amount == 80000.0
    combined = bs.combine(bs.read_with_markers(BROKER, [m]), bs.extract(BROKER).items)
    assert [(i.last4, i.amount, i.source) for i in combined] == [("4321", 80000.0, "marker")]   # replaces the misread
    # next month's page: new amounts, same layout
    later = BROKER.replace("$80,000.00", "$81,234.56").replace("-$50.00", "+$12.00")
    assert bs.read_with_markers(later, [m])[0].amount == 81234.56


def test_marker_beats_the_anchored_reading():
    local = bs.extract(CARD).items
    assert local[0].amount == 500.0                               # the anchored reader takes the first amount
    al, ast = _pos(CARD, "Current balance", 650)
    m = bs.marker_from_example(CARD, 3, 0, al, ast)
    combined = bs.combine(bs.read_with_markers(CARD, [m]), local)
    assert [(i.last4, i.amount, i.source, i.account_id) for i in combined] == [("1111", 650.0, "marker", 3)]


def test_last4_must_agree_with_the_label():
    m = bs.Marker(account_id=1, label_text="Rewards Card", last4="2222", amount_rule="first_after")
    assert bs.read_with_markers(CARD, [m]) == []
    m.last4 = "1111"
    assert bs.read_with_markers(CARD, [m])[0].amount == 500.0


def test_archived_accounts_markers_are_ignored():
    m = bs.Marker(account_id=1, label_text="Rewards Card", last4="1111", archived=True)
    assert bs.read_with_markers(CARD, [m]) == []


def test_marker_on_the_sample_page_matches_the_anchored_reading():
    al, ast = _pos(PAGE, "(as of previous close)", 1234567.89)
    li = _idx(PAGE, "Jordan Brokerage")
    m = bs.marker_from_example(PAGE, 5, li, al, ast)
    assert m.last4 == "3333"
    assert bs.read_with_markers(PAGE, [m])[0].amount == 1234567.89


@pytest.mark.parametrize("label_line_of,amount,error", [
    ("$25,000.00", 25000, "account's name"),       # a line with no words
    ("Your accounts", 105000, "within"),            # amount too far below
])
def test_unusable_examples_are_refused(label_line_of, amount, error):
    al, ast = _pos(BROKER, "", amount)
    with pytest.raises(bs.MarkerError, match=error):
        bs.marker_from_example(BROKER, 1, _idx(BROKER, label_line_of), al, ast)


def test_ai_reading_becomes_a_marker():
    m = bs.marker_for_item(CARD, 3, "1111", 650.0)
    assert m is not None and bs.read_with_markers(CARD, [m])[0].amount == 650.0
    assert bs.marker_for_item(CARD, 3, "9999", 650.0) is None   # number not on the page
    assert bs.marker_for_item(CARD, 3, "1111", 1.23) is None    # amount not near it


def test_markers_without_numbers_do_not_collide_when_merged():
    a = bs.Item(last4="", label="Brokerage Account", amount=1.0, kind="balance", source="marker", account_id=1)
    b = bs.Item(last4="", label="College fund", amount=2.0, kind="balance", source="marker", account_id=2)
    ai = [bs.Item(last4="1111", label="x", amount=3.0, kind="balance", source="ai")]
    assert [i.amount for i in bs.merge_items(bs.combine([a, b], []), ai)] == [1.0, 2.0, 3.0]


def test_ai_does_not_replace_a_marker_reading():
    mk = bs.Item(last4="1111", label="Rewards Card", amount=650.0, kind="balance", source="marker", account_id=3)
    ai = [bs.Item(last4="1111", label="Rewards Card", amount=500.0, kind="balance", source="ai")]
    assert bs.merge_items([mk], ai)[0].amount == 650.0


def test_paste_lines_lists_amount_positions():
    lines = bs.paste_lines(CARD)
    first = lines[1]["amounts"][0]
    assert lines[1]["text"][first["start"]:first["end"]] == "$500.00" and first["value"] == 500.0


def test_a_spot_belongs_to_the_newest_marker():
    old = bs.Marker(account_id=1, label_text="Rewards Card", last4="1111")
    new = bs.Marker(account_id=2, label_text="Rewards Card", last4="1111")
    items = bs.read_with_markers(CARD, [old, new])
    assert [(i.account_id, i.amount) for i in items] == [(2, 500.0)]
    assert bs.same_spot(CARD, old, new)
    other = bs.Marker(account_id=3, label_text="Rewards Card", last4="1111", amount_rule="on_line_with",
                      amount_label="Current balance")
    assert not bs.same_spot(CARD, old, other)
