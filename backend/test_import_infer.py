"""
Drafting formats from files Flow has never seen (import_infer).

The first four are the acceptance tests for the public-release review: each of
these layouts used to be accepted and silently misread (signs swapped, dates
misread) or refused. All files are synthetic.
"""

from __future__ import annotations

import io
from datetime import date

import openpyxl
import pytest

import import_infer as inf
import import_profile as ip


def read(content: bytes, filename: str = "f.csv", kind: str | None = None):
    d = inf.infer(content, filename, kind)
    return d, ip.parse(content, filename, d.spec)


def flow_amounts(parsed) -> dict[str, float]:
    return {r.description: r.amount for r in parsed.rows}


def test_generic_debit_credit_reads_money_out_as_negative():
    d, p = read(b"Date,Description,Debit,Credit\n01/05/2026,GROCERY STORE,50.00,\n01/06/2026,PAYROLL,,2000.00\n")
    assert d.spec.amount_mode == "debit_credit"
    assert flow_amounts(p) == {"GROCERY STORE": -50.0, "PAYROLL": 2000.0}


def test_summary_block_above_the_header_is_skipped():
    content = (b'Description,,Summary Amt.\nBeginning balance as of 01/01/2026,,"1,000.00"\n'
               b'Total credits,,"2,000.00"\n\nDate,Description,Amount,Running Bal.\n'
               b'01/01/2026,Beginning balance as of 01/01/2026,,"1,000.00"\n'
               b'01/05/2026,"GROCERY STORE","-50.00","950.00"\n01/06/2026,"PAYROLL","2,000.00","2,950.00"\n')
    d, p = read(content)
    assert d.header_row == 3 and "running_balance" in d.spec.columns
    assert flow_amounts(p) == {"GROCERY STORE": -50.0, "PAYROLL": 2000.0}   # no fake "Beginning balance" row
    assert ip.verify_sign(p, d.spec).status == "confirmed" and "sign" not in d.needs_confirmation


def test_day_first_dates_are_detected():
    d, p = read(b"Date,Description,Amount\n13/01/2026,TESCO,-12.50\n03/04/2026,RENT,-800.00\n")
    assert d.spec.date_format == "DMY"
    assert [r.date for r in p.rows] == [date(2026, 1, 13), date(2026, 4, 3)] and not p.errors


def test_details_column_with_signed_amounts_and_running_balance():
    content = (b"Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #\n"
               b"DEBIT,01/05/2026,GROCERY,-50.00,ACH_DEBIT,950.00,,\n"
               b"CREDIT,01/06/2026,PAYROLL,2000.00,ACH_CREDIT,2950.00,,\n"
               b"CHECK,01/07/2026,CHECK 1001,-100.00,CHECK_PAID,2850.00,1001,\n")
    d, p = read(content)
    assert d.spec.columns["description"][0].header == "Description"     # not "Details"
    assert d.spec.columns["check_number"][0].header == "Check or Slip #"
    assert flow_amounts(p) == {"GROCERY": -50.0, "PAYROLL": 2000.0, "CHECK 1001": -100.0}
    assert d.confidence["sign"] == "high"


def test_positive_amounts_with_a_type_column():
    content = (b"Account Number,Transaction Description,Transaction Date,Transaction Type,Transaction Amount,Balance\n"
               b"1111,GROCERY,01/05/26,Debit,50.00,950.00\n1111,PAYROLL,01/06/26,Credit,2000.00,2950.00\n")
    d, p = read(content)
    assert d.spec.amount_mode == "amount_plus_direction"
    assert d.spec.direction_out_values == ["debit"] and d.spec.direction_in_values == ["credit"]
    assert flow_amounts(p) == {"GROCERY": -50.0, "PAYROLL": 2000.0}


def test_decimal_comma_semicolon_file():
    d, p = read("Datum;Omschrijving;Bedrag\n13-01-2026;SUPERMARKT;-12,50\n14-01-2026;SALARIS;2.500,00\n".encode())
    assert (d.spec.delimiter, d.spec.decimal, d.spec.date_format) == (";", ",", "DMY")
    assert [r.amount for r in p.rows] == [-12.5, 2500.0]


def test_card_xlsx_with_preamble_rows():
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in (["Activity", "Example Card / Sep 22, 2026 to Oct 22, 2026"], ["Prepared for"], ["JANE EXAMPLE"],
              ["Date", "Description", "Card Member", "Amount"],
              ["10/21/2026", "LUNCH PLACE", "JANE EXAMPLE", 32.01],
              ["10/16/2026", "AUTOPAY PAYMENT - THANK YOU", "JANE EXAMPLE", -500.0]):
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    d, p = read(buf.getvalue(), "activity.xlsx")
    assert d.kind == "card" and d.spec.file_type == "xlsx" and d.header_row == 3
    assert d.spec.sign == "positive_is_out"                    # the payment row settles it
    assert flow_amounts(p) == {"LUNCH PLACE": -32.01, "AUTOPAY PAYMENT - THANK YOU": 500.0}
    assert p.preamble["start"] == date(2026, 9, 22)


def test_headerless_file_reads_by_position():
    d, p = read(b"10/01/2026\t2478.30\t*\t\tPAYROLL EXAMPLE CORP\n10/02/2026\t-120.00\t*\t1001\tCHECK 1001\n",
                "f.txt")
    assert not d.spec.has_header
    assert flow_amounts(p) == {"PAYROLL EXAMPLE CORP": 2478.3, "CHECK 1001": -120.0}


def test_footer_total_row_is_skipped():
    d, p = read(b"Date,Description,Amount\n01/02/2026,A,-1.00\n01/03/2026,B,-2.00\nTotal,,-3.00\n")
    assert d.spec.skip_footer_rows == 1 and not p.errors and len(p.rows) == 2


def test_pending_status_is_skipped():
    d, p = read(b"Date,Description,Amount,Status\n01/02/2026,A,-1.00,Posted\n01/03/2026,B,-2.00,Pending\n")
    assert d.spec.skip_status_values == ["pending"] and len(p.rows) == 1


def test_unsettled_sign_and_ambiguous_dates_need_confirmation():
    d, _p = read(b"Date,Description,Amount\n01/02/2026,A,-1.00\n01/03/2026,B,2.00\n")
    assert "sign" in d.needs_confirmation and "date_format" in d.needs_confirmation


def test_brokerage_holdings_become_a_balance_format():
    content = (b"Account,Symbol,Description,Quantity,Price,Market Value\n"
               b"Brokerage X1111,VTI,Total Stock,10,250.00,2500.00\nBrokerage X1111,BND,Bond,5,70.00,350.00\n"
               b"IRA X2222,VXUS,Intl,3,60.00,180.00\n")
    d, p = read(content)
    assert d.kind == "balance" and d.spec.aggregate == "sum_by_account"
    items = ip.to_balance_items(p, d.spec)
    assert [(i.label, i.amount) for i in items] == [("Brokerage X1111", 2850.0), ("IRA X2222", 180.0)]


def test_kind_hint_wins():
    d, _ = read(b"Date,Description,Amount\n01/02/2026,A,-1.00\n", kind="card")
    assert d.kind == "card" and d.spec.kind == "card"


def test_drafts_round_trip_through_json():
    d, _ = read(b"Date,Description,Debit,Credit\n01/05/2026,GROCERY STORE,50.00,\n")
    assert ip.Spec.from_json(d.spec.to_json()).to_json() == d.spec.to_json()


@pytest.mark.parametrize("content", [b"", b"just,some,text\nwith,no,dates\n", b"Date,Description\n01/02/2026,A\n"])
def test_files_that_are_not_statements_are_refused(content):
    with pytest.raises(inf.InferError):
        inf.infer(content, "f.csv")


def test_malformed_date_at_the_end_is_an_error_not_a_footer():
    d, p = read(b"Date,Description,Amount\n01/02/2026,A,-1.00\n01/03/2026,B,-2.00\n01/04/2026,C,-3.00\n"
                b"13/45/2026,D,-4.00\n")
    assert d.spec.skip_footer_rows == 0 and len(p.errors) == 1 and p.errors[0]["line"] == 5


# ── Headers with a currency note; ambiguous date order ───────────────────────

def test_currency_notes_in_headers_are_ignored_when_matching():
    assert inf.vocab_key("Deposits ($)") == "deposits"
    assert inf.vocab_key("Amount (CAD)") == "amount"
    assert inf.vocab_key("Withdrawals [GBP]") == "withdrawals"
    assert inf.vocab_key("$ Balance") == "balance" and inf.vocab_key("Balance $") == "balance"
    assert inf.vocab_key("Amount ($) (USD)") == "amount"


def test_a_deposits_first_layout_with_currency_notes_keeps_its_signs():
    # Read by position (the header missed), the first money column was taken as money out:
    # payroll came out -2000 and rent +1000. The header is found now, and the roles with it.
    d, p = read(b"Date,Description,Deposits ($),Withdrawals ($),Balance ($)\n"
                b"10/01/2026,PAYROLL ACME,2000.00,,3000.00\n10/03/2026,RENT OCT,,1000.00,2000.00\n")
    assert d.spec.has_header and d.spec.amount_mode == "debit_credit"
    assert flow_amounts(p) == {"PAYROLL ACME": 2000.0, "RENT OCT": -1000.0}
    assert "running_balance" in d.spec.columns


def test_amount_with_a_currency_code_is_the_amount_column():
    d, p = read(b"Date,Description,Amount (CAD)\n2026-10-01,PAYROLL,2000.00\n2026-10-03,RENT,-1000.00\n")
    assert d.spec.has_header and d.spec.amount_mode == "single"
    assert flow_amounts(p) == {"PAYROLL": 2000.0, "RENT": -1000.0}


def test_date_order_is_ambiguous_only_when_both_orders_read_and_differ():
    ambiguous = b"Date,Description,Amount\n03/04/2026,A,-1.00\n05/06/2026,B,-2.00\n"
    settled = b"Date,Description,Amount\n03/04/2026,A,-1.00\n25/06/2026,B,-2.00\n"   # 25 can only be a day
    same = b"Date,Description,Amount\n03/03/2026,A,-1.00\n05/05/2026,B,-2.00\n"      # reads the same either way
    for content, expected in ((ambiguous, True), (settled, False), (same, False)):
        d = inf.infer(content, "f.csv", "cash")
        assert ip.date_order_ambiguous(content, "f.csv", d.spec, ip.parse(content, "f.csv", d.spec)) is expected
    iso = b"Date,Description,Amount\n2026-03-04,A,-1.00\n"
    d = inf.infer(iso, "f.csv", "cash")
    assert ip.date_order_ambiguous(iso, "f.csv", d.spec, ip.parse(iso, "f.csv", d.spec)) is False


# ── Public-release review, second pass ───────────────────────────────────────

def test_a_card_file_with_one_payment_named_merchant_asks_about_the_sign():
    """Charges positive, no running balance, no issuer payment row, one merchant whose name matches
    the payment pattern. Used to draft "negative_is_out" as confirmed (every charge a credit) with
    nothing for the user to answer."""
    f = (b"Date,Description,Card Member,Amount\n01/05/2026,ACME GROCERY,JANE EXAMPLE,54.21\n"
         b"01/06/2026,EXAMPLE AIRLINES,JANE EXAMPLE,312.40\n01/07/2026,ONLINE PAYMENT SOLUTIONS LLC,JANE EXAMPLE,29.00\n")
    d = inf.infer(f, "f.csv")
    assert d.kind == "card" and "sign" in d.needs_confirmation and d.confidence["sign"] == "low"


def test_a_bank_layout_can_be_drafted_for_a_card_account():
    """A checking export (with a check-number column) imported into a card account: the column is
    left unassigned rather than the draft failing with "not a column role for card files"."""
    f = (b"Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #\n"
         b"DEBIT,01/05/2026,GROCERY STORE,-50.00,DEBIT_CARD,950.00,\n"
         b"CREDIT,01/06/2026,PAYROLL,2000.00,ACH_CREDIT,2950.00,\n")
    d = inf.infer(f, "f.csv", "card")
    assert d.kind == "card" and "check_number" not in d.spec.columns
    assert flow_amounts(ip.parse(f, "f.csv", d.spec)) == {"GROCERY STORE": -50.0, "PAYROLL": 2000.0}


def test_whole_number_amounts_are_money_when_the_header_says_so():
    """Some banks (and any JPY or HUF export) write 2,500 and -950 with no decimals. The header
    names the column, so it is the amount; a whole number in an unnamed column is still not."""
    d, p = read(b"Date,Description,Amount\n01/05/2026,RENT,\"-1,250\"\n01/06/2026,PAYROLL,\"2,500\"\n01/07/2026,COFFEE,-4\n")
    assert flow_amounts(p) == {"RENT": -1250.0, "PAYROLL": 2500.0, "COFFEE": -4.0}
    with pytest.raises(inf.InferError, match="'Amount' doesn't hold numbers written like money"):
        inf.infer(b"Date,Description,Amount\n01/05/2026,RENT,-1.250\n01/06/2026,PAYROLL,2.500\n", "f.csv")
    with pytest.raises(inf.InferError, match="^No amount column found.$"):
        inf.infer(b"Date,Description,Ref\n01/05/2026,RENT,1250\n01/06/2026,PAYROLL,2500\n", "f.csv")


def test_ofx_qfx_and_qif_are_named_in_the_rejection():
    for name, body in (("stmt.qfx", b"<OFX><BANKMSGSRSV1/></OFX>"), ("stmt.csv", b"OFXHEADER:100\nDATA:OFXSGML\n"),
                       ("stmt.qif", b"!Type:Bank\nD01/05/2026\nT-50.00\nPGROCERY\n^\n")):
        with pytest.raises(inf.InferError, match="not OFX, QFX or QIF"):
            inf.infer(body, name)


def test_a_two_valued_text_column_is_pointed_at_as_the_direction():
    """All amounts positive and a direction column in another language: the sign question's one
    answer would flip deposits too, so the draft points at the amount-plus-direction layout."""
    f = ("Date;Libellé;Montant;Sens\n05/01/2026;LOYER;1250,00;Débit\n06/01/2026;SALAIRE;2500,00;Crédit\n"
         "07/01/2026;CAFE;4,50;Débit\n").encode("utf-8")
    d = inf.infer(f, "f.csv", "cash")
    assert d.spec.amount_mode == "single" and "sign" in d.needs_confirmation
    assert any("has two values (crédit, débit)" in n and "saying in or out" in n for n in d.notes)
