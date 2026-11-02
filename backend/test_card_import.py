"""
Tests for card import de-duplication and foreign-fee inference.

Card rows go through the register's import_dedup planner unchanged, so the
same rules hold: counts, not existence; repeats within a file survive; a
re-imported or overlapping download adds only what is new.
Runnable directly (``python3 test_card_import.py``) or via pytest.
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import date

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")  # models import; never connects

import card_classifier  # noqa: E402
from card_import import PlannedRow, _infer_foreign_from_fees, is_likely_match, overlap  # noqa: E402
from import_profile import CardRow  # noqa: E402
from import_dedup import line_key, plan_bank_import  # noqa: E402


def r(d, amount, desc):
    return CardRow(date=d, raw_description=desc, amount=amount, source_amount=-amount)


def on_file(*rows):
    return Counter(line_key(x.date, x.amount, x.raw_description) for x in rows)


def test_reimport_skips_everything():
    rows = [r(date(2026, 10, 15), -75.0, "UNITED AIRLINES"), r(date(2026, 10, 15), -75.0, "UNITED AIRLINES")]
    new, skipped = plan_bank_import(rows, on_file(*rows), Counter())
    assert new == [] and skipped == 2


def test_repeats_within_a_file_survive():
    """Two identical $75 airline charges on one day are two charges."""
    rows = [r(date(2026, 10, 15), -75.0, "UNITED AIRLINES"), r(date(2026, 10, 15), -75.0, "UNITED AIRLINES")]
    new, skipped = plan_bank_import(rows, Counter(), Counter())
    assert len(new) == 2 and skipped == 0


def test_overlapping_download_adds_only_new_rows():
    first = [r(date(2026, 10, 17), -20.10, "BP#1000336JOE & ANN'S SALEM OR"),
             r(date(2026, 10, 17), -4.20, "BP#1000336JOE & ANN'S SALEM OR")]
    second = first + [r(date(2026, 10, 21), -38.80, "CIRCLEK#1000043 OAK GROVE IL")]
    new, skipped = plan_bank_import(second, on_file(*first), Counter())
    assert [x.raw_description for x in new] == ["CIRCLEK#1000043 OAK GROVE IL"] and skipped == 2


FILE = [r(date(2026, 8, 1), -5.25, "STARBUCKS STORE 1234 SPRINGFIELD IL"),
        r(date(2026, 9, 15), -4.20, "BP#1000336JOE & ANN'S SALEM OR"),
        r(date(2026, 9, 15), -4.20, "BP#1000336JOE & ANN'S SALEM OR"),
        r(date(2026, 10, 20), -302.14, "TARGET T-1234 SPRINGFIELD IL"),
        r(date(2026, 12, 10), -38.80, "CIRCLEK#1000043 OAK GROVE IL")]


def _existing(rows):
    return [(x.date, x.amount, x.raw_description) for x in rows]


def test_overlap_counts_one_to_one_within_the_cards_dates():
    """Only dates the other card covers are compared; repeats are matched one-to-one."""
    window = (date(2026, 8, 1), date(2026, 10, 23))
    existing = _existing(FILE[:3])                       # one BP charge-pair, Starbucks
    assert overlap(FILE, existing, window) == (3, 4)     # Target compared but absent; December outside
    assert overlap(FILE, _existing(FILE[:2]), window) == (2, 4)   # second BP not double-counted
    assert overlap(FILE, existing, None) == (0, 0)


def test_overlap_tolerates_truncated_descriptions():
    window = (date(2026, 8, 1), date(2026, 8, 31))
    assert overlap(FILE[:1], [(date(2026, 8, 1), -5.25, "STARBUCKS STORE 1234")], window) == (1, 1)
    assert overlap(FILE[:1], [(date(2026, 8, 1), -5.25, "CORNER COFFEE 22")], window) == (0, 1)
    assert overlap(FILE[:1], [(date(2026, 8, 2), -5.25, "STARBUCKS STORE 1234")], window) == (0, 1)


def test_likely_match_thresholds():
    assert is_likely_match(4, 4) and is_likely_match(3, 5)       # 60%+ and at least 3
    assert not is_likely_match(2, 5)                             # too small a share
    assert not is_likely_match(2, 10)
    assert is_likely_match(1, 1) and is_likely_match(2, 2)       # tiny files: every row must match
    assert not is_likely_match(1, 2)
    assert not is_likely_match(0, 0)


def _planned(row, ttype, sub=""):
    cls = card_classifier.TxnClass(ttype, sub, True, "", "", "", "", "", "rule")
    return PlannedRow(row, "", "", "", cls, False, False)


def test_foreign_fee_marks_its_purchase():
    purchase = _planned(r(date(2026, 6, 3), -200.0, "TRATTORIA ROMA ITA"), "purchase")
    other = _planned(r(date(2026, 6, 3), -50.0, "LOCAL SHOP"), "purchase")
    fee = _planned(r(date(2026, 6, 4), -6.0, "FOREIGN TRANSACTION FEE"), "card_fee", "foreign transaction fee")
    _infer_foreign_from_fees([purchase, other, fee])
    assert purchase.foreign and not other.foreign


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


def test_card_numbers_and_remembering_new_ones():
    from card_import import card_numbers, remember_numbers
    from models import CardAccount
    assert card_numbers("1234, 5678") == ["1234", "5678"]
    assert card_numbers("XXXX-1234 5678\n1234") == ["1234", "5678"]
    assert card_numbers(None) == []
    acct = CardAccount(name="Hotel Rewards", last4="1234")
    assert remember_numbers(acct, ["1234", "5678"]) == ["5678"]
    assert acct.last4 == "1234, 5678"
    assert remember_numbers(acct, ["15678"]) == []          # same last 4
    assert acct.last4 == "1234, 5678"


def test_every_card_number_on_the_account_is_redacted():
    from datetime import date
    from card_import import _sanitize_row
    from import_profile import CardRow
    from models import CardAccount
    acct = CardAccount(name="Hotel Rewards", last4="1234, 5678")
    row = CardRow(date=date(2026, 10, 1), raw_description="ACME STORE REF 5678 X1234", amount=-10.0, source_amount=10.0)
    label = _sanitize_row(row, acct, []).label
    assert "1234" not in label and "5678" not in label
