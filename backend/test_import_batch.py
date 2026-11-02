"""
Tests for bulk-import account suggestions (import_batch.plan).

A file is pre-assigned only on evidence that points one way; everything else is
left for the user. Runnable directly (``python3 test_import_batch.py``) or via pytest.
"""

from __future__ import annotations

import threading
from datetime import date, timedelta

import card_ai
from import_batch import BatchAccount, BatchFile, plan

D0 = date(2026, 9, 1)


def rows(start: int, n: int, prefix: str = "SHOP", sign: float = -1) -> list[tuple]:
    """n rows, one a day from ``start`` days after D0; a day's row is the same in every file."""
    return [(D0 + timedelta(days=d), sign * (10 + d), f"{prefix} {d:03d}") for d in range(start, start + n)]


def cov(rs: list[tuple]) -> tuple:
    return (min(r[0] for r in rs), max(r[0] for r in rs))


CHECKING = BatchAccount("r:1", "Checking")
SAVINGS = BatchAccount("r:2", "Savings")
AMEX = BatchAccount("c:1", "Amex", numbers=["1004"])
CHASE = BatchAccount("c:2", "Chase")
ACCOUNTS = [CHECKING, SAVINGS, AMEX, CHASE]


def test_overlap_with_one_account_is_sure():
    held = rows(0, 20)
    f = BatchFile(0, "bank", cash_rows=rows(10, 20))
    s = plan([f], ACCOUNTS, {"r:1": held}, {"r:1": cov(held)})[0]
    assert s.sure and s.account == "r:1"
    assert "10 of its 10" in s.reasons[0]


def test_bank_file_can_belong_to_a_card():
    held = rows(0, 20)
    f = BatchFile(0, "bank", cash_rows=rows(10, 20), card_rows=rows(10, 20))
    s = plan([f], ACCOUNTS, {"c:2": held}, {"c:2": cov(held)})[0]
    assert s.sure and s.account == "c:2"


def test_overlap_with_two_accounts_needs_a_pick():
    held = rows(0, 20)
    f = BatchFile(0, "bank", cash_rows=rows(10, 20))
    s = plan([f], ACCOUNTS, {"r:1": held, "r:2": held}, {"r:1": cov(held), "r:2": cov(held)})[0]
    assert not s.sure and s.account is None and s.hint in ("r:1", "r:2")


def test_no_overlap_needs_a_pick():
    held = rows(0, 20)
    f = BatchFile(0, "bank", cash_rows=rows(40, 10, prefix="NEW"))
    s = plan([f], ACCOUNTS, {"r:1": held}, {"r:1": cov(held)})[0]
    assert not s.sure and s.account is None and s.hint is None


def test_weak_overlap_is_only_a_hint():
    held = rows(0, 20)
    file_rows = rows(0, 2) + rows(2, 8, prefix="OTHER")   # 2 of 10 already there
    s = plan([BatchFile(0, "bank", cash_rows=file_rows)], ACCOUNTS, {"r:1": held}, {"r:1": cov(held)})[0]
    assert not s.sure and s.hint == "r:1"


def test_one_matching_row_is_not_enough():
    held = rows(0, 20)
    file_rows = rows(19, 1) + rows(30, 9, prefix="NEW")   # one row inside the window, matched
    s = plan([BatchFile(0, "bank", cash_rows=file_rows)], ACCOUNTS, {"r:1": held}, {"r:1": cov(held)})[0]
    assert not s.sure


def test_transfers_do_not_count():
    held = rows(0, 20, sign=1)          # +amounts in savings
    f = BatchFile(0, "bank", cash_rows=rows(0, 20))   # same lines, -amounts
    s = plan([f], ACCOUNTS, {"r:2": held}, {"r:2": cov(held)})[0]
    assert not s.sure


def test_card_number_alone_is_sure_with_gap_warning():
    held = rows(0, 10)
    f = BatchFile(0, "card", card_rows=rows(30, 10, prefix="NEW"), numbers=["XXXX-1004"])
    s = plan([f], ACCOUNTS, {"c:1": held}, {"c:1": cov(held)})[0]
    assert s.sure and s.account == "c:1"
    assert s.warnings and "missing" in s.warnings[0]


def test_card_number_with_contiguous_dates_has_no_warning():
    held = rows(0, 10)
    f = BatchFile(0, "card", card_rows=rows(10, 10, prefix="NEW"), numbers=["1004"])
    s = plan([f], ACCOUNTS, {"c:1": held}, {"c:1": cov(held)})[0]
    assert s.sure and not s.warnings


def test_number_listed_on_another_card_contradicts_overlap():
    held = rows(0, 20)
    f = BatchFile(0, "card", card_rows=rows(5, 10), numbers=["1004"])
    s = plan([f], ACCOUNTS, {"c:2": held}, {"c:2": cov(held)})[0]
    assert not s.sure and s.hint == "c:2" and "listed on Amex" in s.reasons[0]


def test_new_number_on_the_matched_card_is_no_objection():
    """A second card on the account (authorized user) or a reissued number: the import adds it."""
    held = rows(0, 20)
    f = BatchFile(0, "card", card_rows=rows(5, 10), numbers=["5678"])
    s = plan([f], ACCOUNTS, {"c:1": held}, {"c:1": cov(held)})[0]
    assert s.sure and s.account == "c:1"
    assert "5678" in s.reasons[1] and "added to Amex" in s.reasons[1]


def test_card_with_several_numbers_matches_any_of_them():
    multi = BatchAccount("c:4", "Hotel Rewards", numbers=["1234", "5678"])
    f = BatchFile(0, "card", card_rows=rows(0, 10), numbers=["5678"])
    s = plan([f], ACCOUNTS + [multi], {}, {})[0]
    assert s.sure and s.account == "c:4"
    assert len(s.reasons) == 1                     # nothing new to add


def test_numbers_on_two_cards_need_a_pick():
    other = BatchAccount("c:4", "Hotel Rewards", numbers=["1234"])
    f = BatchFile(0, "card", card_rows=rows(0, 10), numbers=["1004", "1234"])
    s = plan([f], ACCOUNTS + [other], {}, {})[0]
    assert not s.sure and "more than one card" in s.reasons[0]


def test_unknown_number_without_overlap_needs_a_pick():
    f = BatchFile(0, "card", card_rows=rows(0, 10), numbers=["7777"])
    s = plan([f], ACCOUNTS, {}, {})[0]
    assert not s.sure and "reissued" in s.reasons[0]


def test_card_number_without_overlap_inside_known_dates_needs_a_pick():
    held = rows(0, 20)
    f = BatchFile(0, "card", card_rows=rows(5, 10, prefix="ELSE"), numbers=["1004"])
    s = plan([f], ACCOUNTS, {"c:1": held}, {"c:1": cov(held)})[0]
    assert not s.sure and s.hint == "c:1"


def test_card_number_with_one_unmatched_row_in_known_dates_needs_a_pick():
    held = rows(0, 20)
    f = BatchFile(0, "card", card_rows=rows(19, 1, prefix="ELSE") + rows(20, 10), numbers=["1004"])
    s = plan([f], ACCOUNTS, {"c:1": held}, {"c:1": cov(held)})[0]
    assert not s.sure


def test_guessed_sign_needs_overlap():
    f = BatchFile(0, "card", card_rows=rows(0, 10), numbers=["1004"], sign_guessed=True)
    s = plan([f], ACCOUNTS, {}, {})[0]
    assert not s.sure and "guessed" in s.reasons[0]


def test_archived_account_is_only_a_hint():
    held = rows(0, 20)
    old = BatchAccount("c:3", "Old card", archived=True)
    f = BatchFile(0, "card", card_rows=rows(5, 10))
    s = plan([f], ACCOUNTS + [old], {"c:3": held}, {"c:3": cov(held)})[0]
    assert not s.sure and s.hint == "c:3"


def test_files_chain_through_the_batch():
    held = rows(0, 20)
    a = BatchFile(0, "bank", cash_rows=rows(10, 20))          # overlaps the database
    b = BatchFile(1, "bank", cash_rows=rows(25, 20))          # overlaps only file a
    c = BatchFile(2, "bank", cash_rows=rows(40, 20))          # overlaps only file b
    out = plan([c, b, a], ACCOUNTS, {"r:1": held}, {"r:1": cov(held)})
    assert all(out[i].sure and out[i].account == "r:1" for i in (0, 1, 2))
    assert "already there" in out[0].reasons[0]
    assert "other files here" in out[1].reasons[0] and "other files here" in out[2].reasons[0]


def test_pinned_file_counts_as_history():
    a = BatchFile(0, "bank", cash_rows=rows(0, 20))
    b = BatchFile(1, "bank", cash_rows=rows(10, 20))
    out = plan([a, b], ACCOUNTS, {}, {})
    assert not out[0].sure and not out[1].sure
    out = plan([a, b], ACCOUNTS, {}, {}, pinned={0: "r:2"})
    assert out[0].sure and out[1].sure and out[1].account == "r:2"


def test_ledger_file_waits_for_a_pick():
    out = plan([BatchFile(0, "ledger")], ACCOUNTS, {}, {})
    assert not out[0].sure and out[0].account is None


def test_card_file_never_goes_to_a_cash_account():
    held = rows(0, 20)
    f = BatchFile(0, "card", card_rows=rows(5, 10))
    s = plan([f], ACCOUNTS, {"r:1": held}, {"r:1": cov(held)})[0]
    assert not s.sure and s.hint is None


def test_categorization_requested_mid_run_runs_again(monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []

    def fake(db, force=False):
        calls.append(force)
        if len(calls) == 1:
            started.set()
            release.wait(5)
        return {"sent": 0, "classified": 0, "transactions_updated": 0}

    class FakeDb:
        def close(self): pass
        def rollback(self): pass

    monkeypatch.setattr(card_ai, "categorize_pending", fake)
    t = threading.Thread(target=card_ai.run_categorization, args=(FakeDb,))
    t.start()
    assert started.wait(5)
    assert card_ai.run_categorization(FakeDb) is None    # busy: asks for another pass
    release.set()
    t.join(5)
    assert calls == [False, False]
    card_ai.run_categorization(FakeDb)                    # nothing queued: one pass only
    assert calls == [False, False, False]


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
