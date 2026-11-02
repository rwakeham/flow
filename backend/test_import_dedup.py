"""
Tests for import de-duplication and one-to-one match assignment.

The cases here all revolve around *repeated identical transactions* — a statement
that legitimately lists the same date/amount/description more than once, e.g. two
$4.99 ATM fee reimbursements on the same day. Existence-based de-dup collapses
those to a single row; the planners de-dup by count instead.

Runnable directly (``python3 test_import_dedup.py`` from the backend/ dir) or via
pytest.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date

from import_dedup import (
    coverage_key,
    line_key,
    manual_key,
    normalize_description,
    plan_bank_import,
    plan_ledger_import,
)
from reconciler import TxnInfo, assign_matches, match_annotations, suggest_matches

D = date(2026, 8, 3)


@dataclass
class Bank:
    date: date
    amount: float
    bank_description: str
    check_number: str | None = None


@dataclass
class Ledger:
    date: date
    description: str
    amount: float
    is_reconciled: bool = False


def on_file(*rows) -> Counter:
    """Bank rows already imported — coverage keyed on the statement line itself."""
    return Counter(line_key(r.date, r.amount, r.bank_description) for r in rows)


def hand_reconciled(*rows) -> Counter:
    """Reconciled ledger entries with no bank row — generic (date, amount) coverage."""
    return Counter(coverage_key(r.date, r.amount) for r in rows)


def manual_counts(*rows) -> Counter:
    return Counter(manual_key(r.date, r.amount, r.description) for r in rows)


# ── Bank import de-dup ────────────────────────────────────────────────────────

def test_identical_rows_in_same_file_both_import():
    """The reported bug: two $4.99 credits in the export, only one landed in Flow."""
    fee = Bank(D, 4.99, "DOMESTIC ATM ACCESS FEE REIMBURSEMENT")
    rows = [fee, fee]

    to_import, skipped = plan_bank_import(rows, Counter(), Counter())

    assert len(to_import) == 2, f"expected both fee credits, got {len(to_import)}"
    assert skipped == 0


def test_reimport_of_identical_pair_skips_both():
    """Re-importing the same statement must not add a third or fourth copy."""
    fee = Bank(D, 4.99, "DOMESTIC ATM ACCESS FEE REIMBURSEMENT")
    rows = [fee, fee]

    to_import, skipped = plan_bank_import(rows, on_file(fee, fee), Counter())

    assert to_import == []
    assert skipped == 2


def test_partial_overlap_imports_only_the_surplus():
    """Ledger already has one of the two; the second is genuinely new."""
    fee = Bank(D, 4.99, "DOMESTIC ATM ACCESS FEE REIMBURSEMENT")
    rows = [fee, fee, fee]

    to_import, skipped = plan_bank_import(rows, on_file(fee), Counter())

    assert len(to_import) == 2
    assert skipped == 1


def test_same_amount_different_description_are_distinct():
    withdrawal_a = Bank(D, -204.99, "NON-NETWORK ATM WITHDRAWAL ... CTR00005 CARD 1111")
    withdrawal_b = Bank(D, -204.99, "NON-NETWORK ATM WITHDRAWAL ... CTR00027 CARD 2222")

    to_import, skipped = plan_bank_import(
        [withdrawal_a, withdrawal_b], on_file(withdrawal_a), Counter()
    )

    assert [r.bank_description for r in to_import] == [withdrawal_b.bank_description]
    assert skipped == 1


def test_full_statement_keeps_same_amount_postings_distinct():
    """
    The ledger holds the $50 allowance transfer; a full statement lists it AND a
    different $50 debit. The allowance line consumes the stored row exactly, so
    the Venmo line is a genuine surplus and imports.
    """
    allowance = Bank(D, -50.00, "RECURRING TRANSFER TO EXAMPLE S REF #OP0A1B2C3D")
    venmo = Bank(D, -50.00, "VENMO PAYMENT 260809 1000000000001 JANE EXAMPLE")

    to_import, skipped = plan_bank_import([allowance, venmo], on_file(allowance), Counter())

    assert [r.bank_description for r in to_import] == [venmo.bank_description]
    assert skipped == 1


def test_counts_cover_a_rewritten_line_in_a_partial_statement():
    """
    Tier 3's deliberate trade-off, chosen because a wrong skip on a full statement
    was never the observed failure while re-imported duplicates were: when text
    cannot decide, counts do. A statement carrying only one of two same-date
    same-amount postings consumes the ledger's slot even if it is "the other one".
    """
    allowance = Bank(D, -50.00, "RECURRING TRANSFER TO EXAMPLE S REF #OP0A1B2C3D")
    venmo = Bank(D, -50.00, "VENMO PAYMENT 260809 1000000000001 JANE EXAMPLE")

    to_import, skipped = plan_bank_import([venmo], on_file(allowance), Counter())

    assert to_import == []
    assert skipped == 1


def test_column_padding_does_not_defeat_line_matching():
    """
    A bank's two export layouts can store its column padding differently, so the
    same posting could be written twice under different strings.
    """
    padded = Bank(D, -200.00, "VENMO            PAYMENT    260809 1000000000001   JANE EXAMPLE")
    collapsed = Bank(D, -200.00, "VENMO PAYMENT 260809 1000000000001 JANE EXAMPLE")

    assert normalize_description(padded.bank_description) == normalize_description(collapsed.bank_description)

    to_import, skipped = plan_bank_import([collapsed], on_file(padded), Counter())
    assert to_import == []
    assert skipped == 1


def test_reconciled_manual_covers_one_copy_only():
    """
    A hand-reconciled manual entry stands in for one bank row (so deleting a bank
    row stays deleted) — but only one. The second copy still imports.
    """
    fee = Bank(D, 4.99, "DOMESTIC ATM ACCESS FEE REIMBURSEMENT")

    to_import, skipped = plan_bank_import([fee, fee], Counter(), Counter({coverage_key(D, 4.99): 1}))

    assert len(to_import) == 1
    assert skipped == 1


def test_amount_key_tolerates_float_vs_decimal_representation():
    """DB Numeric round-trips and float parsing must land on the same key."""
    from decimal import Decimal

    assert coverage_key(D, 4.99) == coverage_key(D, Decimal("4.99"))
    assert coverage_key(D, -2345.67) == coverage_key(D, Decimal("-2345.67"))


def test_hand_reconciled_entry_covers_a_row_it_cannot_be_compared_to():
    """
    A "mark reconciled" entry has a friendly name, so there is no line to compare —
    it can only assert that *a* posting at this date and amount already happened.
    """
    row = Bank(D, -1234.56, "ONLINE TRANSFER REF #IB0A1B2C3D TO MORTGAGE XXXXXX1111")

    to_import, skipped = plan_bank_import([row], Counter(), hand_reconciled(row))

    assert to_import == []
    assert skipped == 1


def test_truncated_line_matches_its_full_version():
    """Real case: the pending export cut the Venmo line short; the posted one is full."""
    stored = Bank(D, -200.00, "VENMO            PAYMENT    260809 100000000000")
    posted = Bank(D, -200.00, "VENMO PAYMENT 260809 1000000000001 JANE EXAMPLE")

    to_import, skipped = plan_bank_import([posted], on_file(stored), Counter())

    assert to_import == []
    assert skipped == 1


def test_rewritten_pending_line_is_covered_by_counts():
    """
    Real case: the pending export renders an ATM deposit as placeholder text that
    shares nothing with the posted version. No text tier can bridge that, so the
    stored row degrades to (date, amount) coverage and still absorbs the posting.
    """
    stored = Bank(D, 1500.00, "ATM 1234A 100 Main St Springfield    IL CARD1111")
    posted = Bank(D, 1500.00, "ATM CASH DEPOSIT ON 08/09 100 Main St Springfield IL 0001234 ATM ID 1234A CARD 1111")

    to_import, skipped = plan_bank_import([posted], on_file(stored), Counter())

    assert to_import == []
    assert skipped == 1


def test_short_generic_strings_never_prefix_match():
    """"CHECK" is under the prefix floor: only an exact hit or counts may claim it."""
    stored = Bank(D, -3900.00, "CHECK")
    incoming = Bank(D, -3900.00, "CHECK RETURNED ITEM FEE WAIVED")

    # Exact tier misses, prefix tier refuses (floor) — but tier 3 counts still
    # cover the (date, amount) slot, which is the correct call for a full export.
    to_import, skipped = plan_bank_import([incoming], on_file(stored), Counter())
    assert skipped == 1

    # With the slot already consumed by the exact line, the second row imports.
    to_import, skipped = plan_bank_import([stored, incoming], on_file(stored), Counter())
    assert [r.bank_description for r in to_import] == [incoming.bank_description]


# ── Scenario: the 8/10 statement that produced duplicates ─────────────────────

def test_reimport_does_not_duplicate_already_reconciled_transactions():
    """
    Every row of this statement was already imported and reconciled, so the ledger
    holds a linked bank+manual pair for each. Re-importing must add nothing — even
    though the friendly ledger names ("ATM Deposit") look nothing like the bank text.
    """
    E = date(2026, 8, 10)
    statement = [
        Bank(E, 1500.00, "ATM CASH DEPOSIT ON 08/09 100 Main St Springfield IL"),
        Bank(E, 1200.00, "ZELLE FROM EXAMPLE JOHN ON 08/10 REF # ZL00A0A0A0AA"),
        Bank(E, -840.00, "NON-NETWORK ATM WITHDRAWAL AUTHORIZED ON 08/07 EXAMPLETOWN IL"),
        Bank(E, -25000.00, "ONLINE TRANSFER TO EXAMPLE J HIGH YIELD SAVINGS XXXXX2222"),
        Bank(E, -1234.56, "ONLINE TRANSFER REF #IB0A1B2C3D TO MORTGAGE XXXXXX1111"),
    ]
    # Coverage comes from the bank row of each reconciled pair.
    to_import, skipped = plan_bank_import(statement, on_file(*statement), Counter())

    assert to_import == [], f"re-import duplicated {len(to_import)} reconciled rows"
    assert skipped == 5


def test_planned_entries_do_not_suppress_the_import():
    """
    An unreconciled manual entry is a *plan*, not a posting. The statement row still
    imports (and auto-matching pairs the two) rather than being skipped.
    """
    E = date(2026, 8, 10)
    row = Bank(E, -1234.56, "ONLINE TRANSFER REF #IB0A1B2C3D TO MORTGAGE XXXXXX1111")

    # Planned entries contribute no coverage.
    to_import, skipped = plan_bank_import([row], Counter(), Counter())

    assert len(to_import) == 1
    assert skipped == 0

    bank = [TxnInfo(1, E, -1234.56, row.bank_description)]
    manual = [TxnInfo(10, E, -1234.56, "Northwind Bank - Mortgage")]
    assert len(assign_matches(bank, manual)) == 1, "the pair should auto-match"


def test_new_transaction_sharing_a_date_and_amount_still_imports():
    """Coverage is consumed one-for-one, so a genuine second posting gets through."""
    E = date(2026, 8, 10)
    covered = Bank(E, -200.00, "VENMO PAYMENT 260809 100000000000")
    also_200 = Bank(E, -200.00, "VENMO PAYMENT 260810 998877665544")

    to_import, skipped = plan_bank_import([covered, also_200], on_file(covered), Counter())

    assert len(to_import) == 1
    assert skipped == 1


# ── Ledger import de-dup ──────────────────────────────────────────────────────

def test_ledger_identical_rows_both_import():
    fee = Ledger(D, "ATM Fee Reimbursement", 4.99)

    to_import, skipped = plan_ledger_import([fee, fee], Counter())

    assert len(to_import) == 2
    assert skipped == 0


def test_ledger_reimport_skips_existing_copies():
    fee = Ledger(D, "ATM Fee Reimbursement", 4.99)

    to_import, skipped = plan_ledger_import([fee, fee], manual_counts(fee, fee))

    assert to_import == []
    assert skipped == 2


def test_ledger_partial_overlap_imports_surplus():
    fee = Ledger(D, "ATM Fee Reimbursement", 4.99)

    to_import, skipped = plan_ledger_import([fee, fee], manual_counts(fee))

    assert len(to_import) == 1
    assert skipped == 1


# ── One-to-one match assignment ───────────────────────────────────────────────

def test_suggest_matches_alone_double_books_a_manual_entry():
    """Documents why assign_matches exists — both bank rows pick the same partner."""
    bank = [TxnInfo(1, D, 4.99, "DOMESTIC ATM ACCESS FEE REIMBURSEMENT"),
            TxnInfo(2, D, 4.99, "DOMESTIC ATM ACCESS FEE REIMBURSEMENT")]
    manual = [TxnInfo(10, D, 4.99, "ATM Fee Reimbursement"),
              TxnInfo(11, D, 4.99, "ATM Fee Reimbursement")]

    suggestions = suggest_matches(bank, manual)

    assert len({s.manual_id for s in suggestions}) == 1


def test_assign_matches_pairs_identical_transactions_one_to_one():
    bank = [TxnInfo(1, D, 4.99, "DOMESTIC ATM ACCESS FEE REIMBURSEMENT"),
            TxnInfo(2, D, 4.99, "DOMESTIC ATM ACCESS FEE REIMBURSEMENT")]
    manual = [TxnInfo(10, D, 4.99, "ATM Fee Reimbursement"),
              TxnInfo(11, D, 4.99, "ATM Fee Reimbursement")]

    assigned = assign_matches(bank, manual)

    assert len(assigned) == 2
    assert {s.bank_id for s in assigned} == {1, 2}
    assert {s.manual_id for s in assigned} == {10, 11}


def test_assign_matches_leaves_the_extra_bank_row_unmatched():
    bank = [TxnInfo(1, D, 4.99, "ATM FEE"), TxnInfo(2, D, 4.99, "ATM FEE")]
    manual = [TxnInfo(10, D, 4.99, "ATM Fee Reimbursement")]

    assigned = assign_matches(bank, manual)

    assert len(assigned) == 1
    assert assigned[0].manual_id == 10


def test_assign_matches_prefers_the_better_scoring_pair():
    """
    Two bank rows compete for one well-matching manual entry; the loser falls back
    to the remaining, weaker candidate rather than going unmatched.
    """
    bank = [TxnInfo(1, D, -2345.67, "NWB HOME MTG AUTO PAY 080126 JANE A EXAMPLE"),
            TxnInfo(2, D, -2345.67, "UNRELATED DEBIT")]
    manual = [TxnInfo(10, D, -2345.67, "Northwind Bank Mortgage Auto Pay"),
              TxnInfo(11, D, -2345.67, "Something Else Entirely")]

    assigned = assign_matches(bank, manual)
    pairs = {s.bank_id: s.manual_id for s in assigned}

    assert pairs[1] == 10, "mortgage description should win the mortgage entry"
    assert pairs[2] == 11
    assert len(assigned) == 2


def test_assign_matches_is_empty_when_nothing_is_close():
    bank = [TxnInfo(1, D, -987.65, "AMERICAN EXPRESS ACH PMT")]
    manual = [TxnInfo(10, date(2025, 1, 1), 12.00, "Coffee")]

    assert assign_matches(bank, manual) == []


def test_assign_matches_handles_no_candidates():
    assert assign_matches([], []) == []
    assert assign_matches([TxnInfo(1, D, 4.99, "X")], []) == []


def test_amount_collision_is_flagged_as_weak():
    """
    Bank and ledger wording always differ, so a suggestion is still offered — but a
    pair sharing only a date and an amount must not read as confident: it tops out
    at 50%, and description_score lets the UI say "amount & date only".
    """
    bank = [TxnInfo(1, D, -50.00, "VENMO PAYMENT 260809 1000000000001 JANE EXAMPLE")]
    manual = [TxnInfo(10, D, -50.00, "Transfer to Sam - Allowance")]

    [suggestion] = assign_matches(bank, manual)

    assert suggestion.score == 0.50, "still suggested, so the user can review it"
    assert suggestion.description_score == 0.0, "nothing in common but date and amount"


def test_score_orders_collision_below_genuine_below_identical():
    """The score should separate the three regimes the user actually sees."""
    def score(bank_desc, manual_desc):
        [s] = assign_matches([TxnInfo(1, D, -100.0, bank_desc)],
                             [TxnInfo(10, D, -100.0, manual_desc)])
        return s.score

    collision = score("VENMO PAYMENT 260809 100000000000", "Metro Power Co")
    genuine = score("NWB HOME MTG AUTO PAY 080126 JANE A EXAMPLE", "Northwind Bank Mortgage auto pay")
    identical = score("DOMESTIC ATM ACCESS FEE REIMBURSEMENT", "Domestic ATM access fee reimbursement")

    assert collision == 0.50
    assert 0.65 < genuine < 0.85, f"genuine pairing should read confident-ish, got {genuine}"
    assert identical == 1.0


def test_genuine_match_has_word_overlap():
    """Different vocabularies, but a real pair still shares something."""
    bank = [TxnInfo(1, D, -2345.67, "NWB HOME MTG AUTO PAY 080126 JANE A EXAMPLE")]
    manual = [TxnInfo(10, D, -2345.67, "Northwind Bank Mortgage auto pay")]

    [suggestion] = assign_matches(bank, manual)

    assert suggestion.description_score > 0.0
    assert suggestion.score > 0.70


# ── Ledger match annotations ──────────────────────────────────────────────────

@dataclass
class Row:
    """A ledger record as match_annotations sees it."""
    id: int
    date: date
    amount: float
    description: str
    source: str
    bank_description: str = None
    matched_to_id: int = None
    is_reconciled: bool = False


def test_pending_pair_is_annotated_on_both_halves():
    """
    A matched-but-unconfirmed pair is a proposal. The ledger needs its score so it
    can say how good it is instead of showing a checkmark it has not earned.
    """
    bank = Row(1, D, -200.0, "x", "bank",
               bank_description="VENMO PAYMENT 260809 100000000000", matched_to_id=10)
    manual = Row(10, D, -200.0, "Metro Power Co", "manual", matched_to_id=1)

    ann = match_annotations([bank, manual], D)

    assert ann[1]["match_score"] == 0.50, "amount collision, so capped at 50%"
    assert ann[1]["match_description_score"] == 0.0
    assert ann[10]["match_score"] == 0.50, "the manual half carries it too"


def test_confirmed_pair_is_not_annotated():
    """Once confirmed it is settled — no confidence to report."""
    bank = Row(1, D, -200.0, "x", "bank", bank_description="X",
               matched_to_id=10, is_reconciled=True)
    manual = Row(10, D, -200.0, "Metro Power Co", "manual", matched_to_id=1, is_reconciled=True)

    assert match_annotations([bank, manual], D) == {}


def test_unmatched_bank_rows_get_one_to_one_suggestions():
    strong = Row(1, D, -2345.67, "x", "bank",
                 bank_description="NWB HOME MTG AUTO PAY 080126 JANE A EXAMPLE")
    weak = Row(2, D, -50.0, "x", "bank",
               bank_description="VENMO PAYMENT 260809 999 JANE EXAMPLE")
    mortgage = Row(10, D, -2345.67, "Northwind Bank Mortgage auto pay", "manual")
    allowance = Row(11, D, -50.0, "Transfer to Sam - Allowance", "manual")

    ann = match_annotations([strong, weak, mortgage, allowance], D)

    assert ann[1]["suggestion"]["manual_id"] == 10
    assert ann[1]["suggestion"]["score"] >= 0.70, "reads as confident, button goes blue"
    assert ann[2]["suggestion"]["manual_id"] == 11
    assert ann[2]["suggestion"]["score"] == 0.50, "collision stays below the bar"
    assert 10 not in ann and 11 not in ann, "manual rows carry no suggestion"


def test_future_planned_entries_are_not_suggested():
    """A posted transaction should not be paired with something only planned."""
    posted = Row(1, D, -50.0, "x", "bank", bank_description="VENMO PAYMENT 260809 999")
    planned = Row(12, date(2026, 9, 1), -50.0, "Planned thing", "manual")

    assert match_annotations([posted, planned], D) == {}


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
    print()
    print("all passed" if not failures else f"{failures} failure(s)")
    raise SystemExit(1 if failures else 0)


# ── Wrong-account check (cards and cash accounts) ────────────────────────────

def test_a_file_mostly_in_another_account_points_there():
    from import_dedup import best_other_account, other_account_message
    d = date(2026, 9, 1)
    file_rows = [(date(2026, 9, i), -10.0 * i, f"STORE {i} SPRINGFIELD") for i in range(1, 6)]
    existing = {2: [(r[0], r[1], r[2][:14]) for r in file_rows[:4]]}           # truncated differently
    coverage = {1: (d, date(2026, 9, 30)), 2: (d, date(2026, 9, 30))}
    m = best_other_account(file_rows, 1, {1: "Checking", 2: "Savings"}, existing, coverage)
    assert m == {"account_id": 2, "account": "Savings", "matched": 4, "compared": 5, "matched_on_chosen": 0}
    assert other_account_message(m, "Checking").startswith("This file looks like it belongs to Savings: 4 of its 5")


def test_transfers_between_own_accounts_do_not_count():
    from import_dedup import best_other_account
    d = date(2026, 9, 1)
    file_rows = [(date(2026, 9, i), -500.0, "ONLINE TRANSFER TO SAVINGS") for i in range(1, 5)]
    existing = {2: [(r[0], 500.0, r[2]) for r in file_rows]}                  # the other side of each transfer
    assert best_other_account(file_rows, 1, {1: "Checking", 2: "Savings"}, existing, {2: (d, date(2026, 9, 30))}) is None


def test_no_warning_when_the_chosen_account_has_them_too_or_dates_are_not_covered():
    from import_dedup import best_other_account
    d = date(2026, 9, 1)
    file_rows = [(date(2026, 9, i), -10.0 * i, f"STORE {i}") for i in range(1, 6)]
    both = {1: file_rows, 2: file_rows}
    cov = {1: (d, date(2026, 9, 30)), 2: (d, date(2026, 9, 30))}
    assert best_other_account(file_rows, 1, {1: "A", 2: "B"}, both, cov) is None
    assert best_other_account(file_rows, 1, {1: "A", 2: "B"}, {2: file_rows}, {2: (date(2026, 10, 1), date(2026, 10, 31))}) is None
