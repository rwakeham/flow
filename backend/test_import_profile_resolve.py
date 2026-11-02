"""
Choosing the import format for a file (import_profile_resolve.choose) and format
links as bulk-import evidence. Pure: no DB.
"""

from __future__ import annotations

from datetime import date

import import_batch
import import_profile as ip
import import_profile_resolve as resolve
from testdata.sample_files import FORMATS, by_name, spec


def cand(i: int, key: str, accounts=()) -> resolve.Candidate:
    return resolve.Candidate(id=i, name=key, kind=FORMATS[key]["kind"], spec=spec(key), accounts=list(accounts))


CASH = [cand(1, "bank_csv_status"), cand(2, "bank_tsv_headerless"), cand(3, "ledger_by_position")]


def pick(content: bytes, filename: str, cands=CASH, kind="cash") -> resolve.Resolution:
    return resolve.choose(content, filename, cands, kind)


def test_each_layout_picks_its_own_format():
    for fixture, expected in (("bank_csv_status", 1), ("bank_tsv_status", 1), ("bank_tsv_headerless", 2),
                              ("ledger_headerless", 3), ("ledger_with_header", 3)):
        fx = by_name(fixture)
        r = pick(fx.content, fx.filename)
        assert (r.status, r.candidate.id) == ("match", expected), fixture


def test_renamed_header_is_drift_not_a_positional_read():
    """The ledger format would read this by position; it's the bank export with a renamed column."""
    f = b"Date,Payee,Amount,Check #,Status\n10/01/2026,PAYROLL,2478.30,,Posted\n"
    r = pick(f, "x.csv")
    assert r.status == "drift" and r.candidate.id == 1
    assert r.diff["added"] == ["Payee"] and r.diff["removed"] == ["Description"]
    assert r.draft["columns"]["description"][0]["header"] == "Payee"         # re-pointed from the inferred draft
    assert r.draft["header_cells"] == ["Date", "Payee", "Amount", "Check #", "Status"]
    assert ip.Spec.from_json(r.draft).fingerprint == ip.fingerprint(r.draft["header_cells"])


def test_added_column_is_drift_and_the_redraft_keeps_the_roles():
    f = b"Date,Description,Amount,Check #,Status,Balance\n10/01/2026,PAYROLL,2478.30,,Posted,100.00\n"
    r = pick(f, "x.csv")
    assert r.status == "drift" and r.diff["added"] == ["Balance"]
    assert set(r.draft["columns"]) == set(FORMATS["bank_csv_status"]["columns"])


def test_a_format_needing_only_common_columns_doesnt_claim_unrelated_files():
    """A wide format whose required columns are Date/Description/Amount doesn't claim a 4-column bank file."""
    f = b"Date,Description,Amount,Running Bal.\n01/05/2026,GROCERY,-50.00,950.00\n"
    r = pick(f, "x.csv", [cand(9, "csv_preamble")], kind="card")
    assert r.status == "none" and r.draft["kind"] == "card"


def test_no_format_gives_a_draft_in_the_accounts_kind():
    f = b"Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n10/02/2026,10/03/2026,STARBUCKS,Food,Sale,-6.25,\n"
    assert pick(f, "x.csv").draft["kind"] == "cash"
    r = pick(f, "x.csv", [], kind="card")
    assert r.status == "none" and r.draft["kind"] == "card"


def test_text_and_dates_never_read_as_amounts():
    """A headerless format must not 'read' a headered file through its text columns."""
    fx = by_name("card_csv_type_column")
    assert pick(fx.content, fx.filename).status == "none"
    assert ip.parse_amount("10/03/2026") is None and ip.parse_amount("CHECK # 1001") is None
    assert ip.parse_amount("USD 12.50") == 12.5


def test_xlsx_formats_only_read_xlsx_files():
    fx = by_name("bank_csv_status")
    assert pick(fx.content, fx.filename, [cand(5, "xlsx_preamble")], "card").status == "none"


# ── Bulk import ──────────────────────────────────────────────────────────────

D = date(2026, 10, 1)
ACCTS = [import_batch.BatchAccount("r:1", "Checking"), import_batch.BatchAccount("r:2", "Savings"),
         import_batch.BatchAccount("c:5", "Card")]


def test_format_linked_to_one_account_is_enough():
    f = import_batch.BatchFile(index=0, kind="bank", cash_rows=[(D, -5.0, "SHOP")], card_rows=[(D, -5.0, "SHOP")],
                               format_accounts=["r:1"])
    s = import_batch.plan([f], ACCTS, {}, {})[0]
    assert s.sure and s.account == "r:1" and "format" in s.reasons[0]


def test_format_shared_by_two_accounts_is_no_evidence():
    f = import_batch.BatchFile(index=0, kind="bank", cash_rows=[(D, -5.0, "SHOP")], format_accounts=["r:1", "r:2"])
    assert not import_batch.plan([f], ACCTS, {}, {})[0].sure


def test_ledger_file_with_its_own_format_goes_to_that_account():
    f = import_batch.BatchFile(index=0, kind="ledger", format_accounts=["r:2"])
    s = import_batch.plan([f], ACCTS, {}, {})[0]
    assert s.sure and s.account == "r:2"


def test_overlap_with_another_account_beats_the_format_link():
    rows = [(D, -float(i), f"SHOP {i}") for i in range(1, 5)]
    f = import_batch.BatchFile(index=0, kind="bank", cash_rows=rows, format_accounts=["r:1"])
    s = import_batch.plan([f], ACCTS, {"r:2": rows}, {"r:2": (D, D)})[0]
    assert s.sure and s.account == "r:2"


def test_unconfirmed_card_sign_needs_a_pick_even_with_a_format_link():
    f = import_batch.BatchFile(index=0, kind="card", card_rows=[(D, -5.0, "SHOP")], sign_guessed=True,
                               format_accounts=["c:5"])
    assert not import_batch.plan([f], ACCTS, {}, {})[0].sure
