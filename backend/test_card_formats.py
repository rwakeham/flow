"""
Card statements read through import formats: common layouts, signs, pending rows.

Each sample in testdata/sample_files.py is read with its format, the way a card import
reads it once that format is linked to the card. Runnable via pytest.
"""

from __future__ import annotations

from datetime import date

import pytest

import import_profile as ip
from testdata.sample_files import FORMATS, by_name, spec as format_spec


def read(name: str) -> ip.CardParseResult:
    sample = by_name(name)
    spec = format_spec(sample.format)
    return ip.to_card_result(ip.parse(sample.content, sample.filename, spec), spec, sample.format,
                             institution="Example Card Co")


def test_xlsx_preamble_period_and_sign():
    """The header sits below a preamble; the payment row confirms charges are positive in the file."""
    r = read("card_xlsx_preamble")
    assert r.institution == "Example Card Co"
    assert r.sign_convention == "charges_positive" and r.sign_status == "confirmed"
    assert "payment" in r.sign_reason
    assert r.statement_start == date(2026, 9, 22) and r.statement_end == date(2026, 10, 22)
    assert [x.amount for x in r.rows] == [-32.01, 500.00, -275.34]       # Flow convention
    assert r.rows[0].city == "SPRINGFIELD"
    assert r.rows[0].issuer_category == "Restaurant-Restaurant"
    assert r.rows[2].subscription_hint                                     # "MONTHLY DUES"
    assert r.suggested_last4 == "9999"
    assert r.card_numbers == ["9999"]
    assert r.suggested_cardholders == ["JANE EXAMPLE"]                     # de-duplicated


def test_card_numbers_from_every_card_on_the_account():
    """Authorized users' charges carry their own card number; all of them are collected."""
    r = read("card_xlsx_preamble_two_cards")
    assert r.suggested_last4 == "9999"
    assert r.card_numbers == ["9999", "1002"]


def test_status_column_drops_pending_and_leaves_sign_unverified():
    """No payment rows: the format's sign stands, and the import says nothing confirmed it."""
    r = read("card_csv_status")
    assert r.sign_convention == "charges_negative" and r.sign_needs_review
    assert r.pending_dropped == 1
    assert [x.amount for x in r.rows] == [-38.80, -302.14]
    assert r.statement_start is None


def test_type_column_payment():
    r = read("card_csv_type_column")
    assert r.sign_convention == "charges_negative" and r.sign_status == "confirmed"
    assert r.rows[0].amount == -6.25 and r.rows[0].post_date == date(2026, 10, 3)
    assert r.rows[1].issuer_type == "Payment"


def test_debit_credit_columns():
    r = read("card_csv_debit_credit")
    assert r.sign_convention == "debit_credit_columns"
    assert [x.amount for x in r.rows] == [-41.20, 19.99]
    assert r.card_numbers == ["1234"]


def test_status_and_member_columns_drop_pending_rows():
    r = read("card_csv_status_member")
    assert r.pending_dropped == 1 and len(r.rows) == 1
    assert r.rows[0].cardholder == "JANE EXAMPLE"


def test_positive_charges():
    r = read("card_csv_positive_charges")
    assert r.sign_convention == "charges_positive"
    assert r.rows[0].amount == -54.10


def test_headerless_layout():
    r = read("card_tsv_headerless")
    assert [x.raw_description for x in r.rows] == ["CHIPOTLE 1234 SPRINGFIELD IL", "CORNER COFFEE"]


def test_accounting_amounts_and_long_dates():
    r = read("card_csv_accounting")
    assert r.rows[0].date == date(2026, 10, 2) and r.rows[0].amount == -1234.50


def test_a_file_that_isnt_the_formats_layout_is_a_mismatch():
    with pytest.raises(ip.FormatMismatch):
        ip.parse(b"just,some,text\nwith,no,dates\n", "x.csv", format_spec("csv_type_column"))


def test_reversed_sign_is_reported():
    """The same workbook read as if charges were negative: its payment row gives it away."""
    sample = by_name("card_xlsx_preamble")
    spec = ip.Spec.from_json({**FORMATS["xlsx_preamble"], "sign": "negative_is_out"})
    r = ip.to_card_result(ip.parse(sample.content, sample.filename, spec), spec, "x")
    assert r.sign_status == "reversed" and r.sign_needs_review
