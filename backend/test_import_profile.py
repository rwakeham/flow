"""
Tests for the import-format engine (import_profile): reading files from a Spec.

All files are synthetic. Runnable via pytest.
"""

from __future__ import annotations

import io
from datetime import date

import openpyxl
import pytest

import import_profile as ip


def spec(**kw) -> ip.Spec:
    base = {"kind": "cash", "has_header": True,
            "columns": {"date": [{"header": "Date"}], "description": [{"header": "Description"}],
                        "amount": [{"header": "Amount"}]}}
    cols = kw.pop("columns", None)
    if cols is not None:
        base["columns"] = cols
    base.update(kw)
    return ip.Spec.from_json(base)


def H(name, **kw):
    return [{"header": name, **kw}]


def amounts(content: bytes, s: ip.Spec, filename: str = "f.csv") -> list[float]:
    return [r.amount for r in ip.parse(content, filename, s).rows]


# ── Values ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,dec,thou,expected", [
    ("1,234.56", ".", ",", 1234.56), ("-1,234.56", ".", ",", -1234.56), ("(1,234.56)", ".", ",", -1234.56),
    ("$(12.00)", ".", ",", -12.0), ("12.34-", ".", ",", -12.34), ("-$1,045.12", ".", ",", -1045.12),
    ("1.234,56", ",", ".", 1234.56), ("-12,50", ",", ".", -12.5), ("1 234,56", ",", " ", 1234.56),
    ("1'234.50", ".", "'", 1234.5), ("€ 7,00", ",", ".", 7.0), ("+5.00", ".", ",", 5.0),
    ("N/A", ".", ",", None), ("", ".", ",", None), ("-", ".", ",", None),
])
def test_parse_amount(text, dec, thou, expected):
    assert ip.parse_amount(text, dec, thou) == expected


def test_parens_can_be_positive_when_the_format_says_so():
    assert ip.parse_amount("(5.00)", parens_negative=False) == 5.0
    assert ip.parse_amount("-(5.00)", parens_negative=False) == -5.0


@pytest.mark.parametrize("text,fmt,expected", [
    ("03/04/2026", "MDY", date(2026, 3, 4)), ("03/04/2026", "DMY", date(2026, 4, 3)),
    ("13/01/2026", "DMY", date(2026, 1, 13)), ("13/01/2026", "MDY", None),
    ("2026-01-13", "MDY", date(2026, 1, 13)), ("2026/01/13", "DMY", date(2026, 1, 13)),
    ("1/5/26", "MDY", date(2026, 1, 5)), ("1/5/69", "MDY", date(1969, 1, 5)),
    ("13.01.2026", "DMY", date(2026, 1, 13)), ("Oct 2, 2026", "DMY", date(2026, 10, 2)),
    ("2 Oct 2026", "MDY", date(2026, 10, 2)), ("10/02/2026 14:30:00", "MDY", date(2026, 10, 2)),
    ("01/13/2026", "YMD", None), ("hello", "MDY", None),
])
def test_parse_date(text, fmt, expected):
    assert ip.parse_date(text, fmt) == expected


# ── Spec validation and JSON ─────────────────────────────────────────────────

@pytest.mark.parametrize("bad,message", [
    ({"kind": "loan"}, "kind"),
    ({"columns": {"date": H("Date"), "description": H("Description")}}, "amount column"),
    ({"amount_mode": "debit_credit"}, "money-out"),
    ({"sign": None}, "sign"),
    ({"columns": {"date": H("Date"), "description": H("D"), "amount": H("A"), "cardholder": H("C")}}, "not a column role"),
    ({"decimal": ",", "thousands": ","}, "differ"),
    ({"has_header": False}, "position"),
    ({"surprise": 1}, "Unknown"),
    ({"skip_rows_containing": [{"text": "  "}]}, "skip_rows_containing"),
    ({"row_source": "manual", "kind": "card"}, "manual"),
    ({"version": 2}, "version"),
])
def test_invalid_specs_are_refused(bad, message):
    with pytest.raises(ip.SpecError, match=message):
        spec(**bad)


def test_json_round_trip():
    s = spec(skip_status_values=["pending"], header_cells=["Date", "Description", "Amount"])
    assert ip.Spec.from_json(s.to_json()).to_json() == s.to_json()


# ── Reading ──────────────────────────────────────────────────────────────────

def test_single_amount_both_signs():
    f = b"Date,Description,Amount\n01/05/2026,SHOP,50.00\n01/06/2026,REFUND,-5.00\n"
    assert amounts(f, spec(sign="negative_is_out")) == [50.0, -5.0]
    assert amounts(f, spec(sign="positive_is_out")) == [-50.0, 5.0]


def test_debit_credit_columns():
    f = b"Date,Description,Debit,Credit\n01/05/2026,SHOP,50.00,\n01/06/2026,PAY,,2000.00\n"
    s = spec(amount_mode="debit_credit", sign=None,
             columns={"date": H("Date"), "description": H("Description"), "debit": H("Debit"), "credit": H("Credit")})
    rows = ip.parse(f, "f.csv", s).rows
    assert [r.amount for r in rows] == [-50.0, 2000.0]
    assert [r.source_amount for r in rows] == [50.0, -2000.0]


def test_amount_plus_direction_and_unknown_direction_is_an_error():
    f = b"Date,Description,Amount,Type\n01/05/2026,SHOP,50.00,Debit\n01/06/2026,PAY,2000.00,CREDIT\n01/07/2026,X,1.00,???\n"
    s = spec(amount_mode="amount_plus_direction", sign=None, direction_out_values=["debit"],
             direction_in_values=["credit"],
             columns={"date": H("Date"), "description": H("Description"), "amount": H("Amount"),
                      "direction": H("Type")})
    p = ip.parse(f, "f.csv", s)
    assert [r.amount for r in p.rows] == [-50.0, 2000.0]
    assert len(p.errors) == 1 and p.errors[0]["line"] == 4


def test_bad_date_is_an_error_unless_the_format_skips_it():
    f = b"Date,Description,Amount\n13/01/2026,TESCO,-12.50\n03/04/2026,RENT,-800.00\n"
    p = ip.parse(f, "f.csv", spec())
    assert len(p.rows) == 1 and p.errors[0]["line"] == 2 and "13/01/2026" in p.errors[0]["message"]
    p = ip.parse(f, "f.csv", spec(on_bad_date="skip"))
    assert len(p.rows) == 1 and p.skipped["bad_date"] == 1 and not p.errors
    assert [r.date for r in ip.parse(f, "f.csv", spec(date_format="DMY")).rows] == [date(2026, 1, 13), date(2026, 4, 3)]


def test_rows_without_a_date_or_an_amount_are_skipped_not_errors():
    f = b"Date,Description,Amount\n,Note,\n01/01/2026,Opening balance,\n01/02/2026,SHOP,-5.00\n"
    p = ip.parse(f, "f.csv", spec())
    assert len(p.rows) == 1 and p.skipped == {"no_date": 1, "no_amount": 1} and not p.errors


def test_unreadable_amount_is_an_error():
    p = ip.parse(b"Date,Description,Amount\n01/02/2026,SHOP,abc\n", "f.csv", spec())
    assert not p.rows and "abc" in p.errors[0]["message"]


def test_header_found_below_a_summary_block_and_footer_skipped():
    f = (b"Description,,Summary Amt.\nBeginning balance,,100.00\n\n"
         b"Date,Description,Amount\n01/02/2026,SHOP,-5.00\n01/03/2026,PAY,10.00\nTotal,,5.00\n")
    p = ip.parse(f, "f.csv", spec(skip_footer_rows=1))
    assert p.header_row == 2 and [r.amount for r in p.rows] == [-5.0, 10.0] and not p.errors


def test_skip_rows_containing_any_column_or_one_column():
    f = b"Date,Description,Amount\n01/02/2026,Interest charge,-1.00\n01/03/2026,SHOP,-5.00\n"
    assert amounts(f, spec(skip_rows_containing=[{"text": "interest"}])) == [-5.0]
    assert amounts(f, spec(skip_rows_containing=[{"text": "interest", "column": "amount"}])) == [-1.0, -5.0]


def test_status_values_skipped():
    f = b"Date,Description,Amount,Status\n01/02/2026,A,-1.00,Posted\n01/03/2026,B,-2.00,PENDING\n"
    s = spec(skip_status_values=["pending"], columns={"date": H("Date"), "description": H("Description"),
                                                      "amount": H("Amount"), "status": H("Status")})
    p = ip.parse(f, "f.csv", s)
    assert [r.amount for r in p.rows] == [-1.0] and p.skipped["status"] == 1


def test_multi_column_description_and_whitespace():
    f = b"Date,Payee,Memo,Amount\n01/02/2026,ACME   CORP,  invoice 7 ,-1.00\n"
    s = spec(columns={"date": H("Date"), "description": H("Payee") + H("Memo"), "amount": H("Amount")})
    assert ip.parse(f, "f.csv", s).rows[0].description == "ACME CORP invoice 7"
    s2 = spec(collapse_whitespace=False,
              columns={"date": H("Date"), "description": H("Payee"), "amount": H("Amount")})
    assert ip.parse(f, "f.csv", s2).rows[0].description == "ACME   CORP"


def test_headerless_by_position_skips_leading_rows():
    f = b"some title\n10/01/2026\t-12.50\tSHOP\n10/02/2026\t3.00\tREFUND\n"
    s = ip.Spec.from_json({"kind": "cash", "has_header": False,
                           "columns": {"date": [{"index": 0}], "amount": [{"index": 1}],
                                       "description": [{"index": 2}]}})
    p = ip.parse(f, "f.txt", s)
    assert [r.description for r in p.rows] == ["SHOP", "REFUND"] and not p.errors


def test_semicolon_and_decimal_comma():
    f = "Datum;Omschrijving;Bedrag\n13-01-2026;SUPERMARKT;-12,50\n14-01-2026;SALARIS;2.500,00\n".encode()
    s = spec(delimiter=";", decimal=",", thousands=".", date_format="DMY",
             columns={"date": H("Datum"), "description": H("Omschrijving"), "amount": H("Bedrag")})
    assert amounts(f, s) == [-12.5, 2500.0]


def test_latin1_and_bom_are_decoded():
    f = "﻿Date,Description,Amount\n01/02/2026,CAFÉ,-3.00\n".encode("utf-8")
    assert ip.parse(f, "f.csv", spec()).rows[0].description == "CAFÉ"
    f = "Date,Description,Amount\n01/02/2026,CAFÉ,-3.00\n".encode("latin-1")
    assert ip.parse(f, "f.csv", spec()).rows[0].description == "CAFÉ"


def _xlsx(sheets: dict[str, list[list]]) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_xlsx_sheet_choice_and_typed_cells():
    content = _xlsx({"Summary": [["Total", 5.0]],
                     "Activity": [["Date", "Description", "Amount"], [date(2026, 1, 2), "SHOP", -5.0]]})
    p = ip.parse(content, "f.xlsx", spec(file_type="xlsx"))
    assert p.sheet == "Activity" and p.rows[0].date == date(2026, 1, 2) and p.rows[0].amount == -5.0
    with pytest.raises(ip.FormatMismatch):
        ip.parse(content, "f.xlsx", spec(file_type="xlsx", sheet="Summary"))
    with pytest.raises(ip.FormatMismatch, match="no sheet"):
        ip.parse(content, "f.xlsx", spec(file_type="xlsx", sheet="Nope"))


def test_xlsx_file_against_a_text_format_is_a_mismatch():
    content = _xlsx({"S": [["Date", "Description", "Amount"]]})
    with pytest.raises(ip.FormatMismatch, match="spreadsheet"):
        ip.parse(content, "f.xlsx", spec())


# ── Drift ────────────────────────────────────────────────────────────────────

def test_fingerprint_and_drift():
    hdr = ["Date", "Description", "Amount"]
    s = spec(header_cells=hdr)
    p = ip.parse(b"date, description ,AMOUNT\n01/02/2026,A,-1.00\n", "f.csv", s)
    assert p.fingerprint_ok and p.diff is None                       # case and spacing don't matter
    p = ip.parse(b"Date,Description,Amount,Balance\n01/02/2026,A,-1.00,9.00\n", "f.csv", s)
    assert p.fingerprint_ok is False and p.diff["added"] == ["Balance"] and len(p.rows) == 1
    with pytest.raises(ip.FormatMismatch) as exc:
        ip.parse(b"Date,Payee,Amount\n01/02/2026,A,-1.00\n", "f.csv", s)
    assert exc.value.diff["missing"] == ["Description"]
    assert exc.value.diff["added"] == ["Payee"] and exc.value.diff["removed"] == ["Description"]


def test_optional_column_may_be_missing():
    s = spec(columns={"date": H("Date"), "description": H("Description"), "amount": H("Amount"),
                      "status": H("Status", optional=True)})
    assert amounts(b"Date,Description,Amount\n01/02/2026,A,-1.00\n", s) == [-1.0]


def test_header_diff_moves():
    d = ip.header_diff(["A", "B", "C"], ["B", "A", "C", "D"])
    assert d["added"] == ["D"] and not d["removed"] and {m["header"] for m in d["moved"]} == {"A", "B"}


def test_headerless_fingerprints_differ_by_layout_and_fits_picks_the_right_one():
    a = ip.Spec.from_json({"kind": "cash", "has_header": False,
                           "columns": {"date": [{"index": 0}], "amount": [{"index": 1}], "description": [{"index": 2}]}})
    b = ip.Spec.from_json({"kind": "cash", "has_header": False,
                           "columns": {"date": [{"index": 0}], "description": [{"index": 1}], "amount": [{"index": 2}]}})
    assert a.fingerprint != b.fingerprint
    f = b"10/01/2026,-12.50,SHOP\n10/02/2026,3.00,REFUND\n"
    assert ip.fits(f, "f.csv", a) == 1.0 and ip.fits(f, "f.csv", b) == 0.0


# ── Sign check ───────────────────────────────────────────────────────────────

BAL = (b"Date,Description,Amount,Balance\n01/05/2026,SHOP,-50.00,950.00\n01/06/2026,PAY,2000.00,2950.00\n"
       b"01/07/2026,COFFEE,-4.50,2945.50\n")


def _bal_spec(sign):
    return spec(sign=sign, columns={"date": H("Date"), "description": H("Description"), "amount": H("Amount"),
                                    "running_balance": H("Balance")})


def test_running_balance_confirms_or_reverses():
    s = _bal_spec("negative_is_out")
    assert ip.verify_sign(ip.parse(BAL, "f.csv", s), s).status == "confirmed"
    s = _bal_spec("positive_is_out")
    assert ip.verify_sign(ip.parse(BAL, "f.csv", s), s).status == "reversed"


def test_running_balance_newest_first():
    lines = BAL.decode().splitlines()
    newest_first = "\n".join([lines[0]] + lines[1:][::-1]).encode()
    s = _bal_spec("negative_is_out")
    assert ip.verify_sign(ip.parse(newest_first, "f.csv", s), s).status == "confirmed"


def test_history_check():
    f = b"Date,Description,Amount\n01/05/2026,SHOP,-50.00\n01/06/2026,PAY,2000.00\n"
    s = spec()
    p = ip.parse(f, "f.csv", s)
    same = [(date(2026, 1, 5), -50.0, "SHOP"), (date(2026, 1, 6), 2000.0, "PAY")]
    flipped = [(d, -a, t) for d, a, t in same]
    assert ip.verify_sign(p, s, same).status == "confirmed"
    assert ip.verify_sign(p, s, flipped).status == "reversed"
    assert ip.verify_sign(p, s, [same[0], flipped[1]]).status == "conflict"
    assert ip.verify_sign(p, s, []).status == "unverified"


def test_card_payment_rows():
    f = b"Date,Description,Amount\n01/05/2026,SHOP,50.00\n01/06/2026,AUTOPAY PAYMENT - THANK YOU,-500.00\n"
    s = spec(kind="card", sign="positive_is_out")
    assert ip.verify_sign(ip.parse(f, "f.csv", s), s).status == "confirmed"
    s = spec(kind="card", sign="negative_is_out")
    assert ip.verify_sign(ip.parse(f, "f.csv", s), s).status == "reversed"


def test_a_merchant_named_like_a_payment_is_no_evidence_of_the_sign():
    """Charges positive, no payment row, one merchant whose name matches the payment pattern: it runs
    the same way as the charges, so it is a merchant, not a payment. Used to confirm the wrong sign
    for the whole file (every charge stored as a credit), with no question asked."""
    f = (b"Date,Description,Amount\n01/05/2026,ACME GROCERY,54.21\n01/06/2026,EXAMPLE AIRLINES,312.40\n"
         b"01/07/2026,ONLINE PAYMENT SOLUTIONS LLC,29.00\n01/08/2026,STREAMING SERVICE,15.99\n")
    for sign in ("negative_is_out", "positive_is_out"):
        s = spec(kind="card", sign=sign)
        check = ip.verify_sign(ip.parse(f, "f.csv", s), s)
        assert check.status == "unverified" and "named like a payment" in check.reason
    # The issuer's own payment line runs the other way: that still settles it.
    g = f + b"01/09/2026,ONLINE PAYMENT - THANK YOU,-400.00\n"
    s = spec(kind="card", sign="positive_is_out")
    assert ip.verify_sign(ip.parse(g, "f.csv", s), s).status == "confirmed"


# ── Adapters ─────────────────────────────────────────────────────────────────

def test_ledger_adapter_drops_placeholders_and_reads_reconciled():
    f = b"Date,Description,In,Out,R\n01/01/2026,Pay,100.00,,X\n01/02/2026,Placeholder,0,,\n01/03/2026,,,5.00,\n"
    s = spec(row_source="manual", amount_mode="debit_credit", sign=None,
             columns={"date": H("Date"), "description": H("Description"), "credit": H("In"), "debit": H("Out"),
                      "reconciled": H("R")})
    rows = ip.to_ledger_rows(ip.parse(f, "f.csv", s))
    assert [(r.description, r.amount, r.is_reconciled) for r in rows] == [("Pay", 100.0, True)]


def test_card_adapter_counts_rows_without_description_and_errors():
    f = b"Date,Description,Amount\n01/02/2026,,5.00\nbad,X,1.00\n01/03/2026,SHOP,7.00\n"
    s = spec(kind="card", sign="positive_is_out")
    r = ip.to_card_result(ip.parse(f, "f.csv", s), s, "My card format")
    assert [x.amount for x in r.rows] == [-7.0] and r.rows_skipped == 2
    assert r.issuer_format == "My card format" and r.sign_convention == "charges_positive"


def test_balance_adapter_sums_by_account():
    f = b"Account,Symbol,Market Value\nBrokerage X1111,VTI,2500.00\nBrokerage X1111,BND,350.00\nIRA,VX,180.00\n"
    s = ip.Spec.from_json({"kind": "balance", "has_header": True, "aggregate": "sum_by_account", "sign": None,
                           "columns": {"account_ref": H("Account"), "value": H("Market Value")}})
    items = ip.to_balance_items(ip.parse(f, "f.csv", s), s)
    assert [(i.label, i.last4, i.amount) for i in items] == [("Brokerage X1111", "1111", 2850.0), ("IRA", "", 180.0)]


# ── Row cap ──────────────────────────────────────────────────────────────────

def _csv(n, blank_every=0):
    lines = ["Date,Description,Amount"]
    for i in range(n):
        lines.append("10/01/2026,SHOP,-1.00")
        if blank_every and i % blank_every == 0:
            lines.append(",,")
    return ("\n".join(lines) + "\n").encode()


def test_files_over_the_row_cap_are_refused(monkeypatch):
    import pytest
    monkeypatch.setattr(ip, "MAX_ROWS", 100)
    assert len(ip.read_sheets(_csv(99), "a.csv")[0][1]) == 100          # header + 99 rows: at the cap
    with pytest.raises(ip.TooManyRows, match="more than 100 rows"):
        ip.read_sheets(_csv(100), "a.csv")
    assert len(ip.read_sheets(_csv(99, blank_every=1), "a.csv")[0][1]) == 100   # blank rows don't count
    assert not issubclass(ip.TooManyRows, ValueError)                   # no layout check swallows it


def test_spreadsheets_are_capped_too(monkeypatch):
    import io

    import openpyxl
    import pytest
    monkeypatch.setattr(ip, "MAX_ROWS", 50)
    monkeypatch.setattr(ip, "MAX_SCANNED_ROWS", 500)

    def xlsx(rows, blank_tail=0):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Date", "Description", "Amount"])
        for _ in range(rows):
            ws.append(["10/01/2026", "SHOP", -1.0])
        if blank_tail:
            ws.cell(row=rows + 1 + blank_tail, column=1, value=" ")      # a used range far below the data
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    assert len(ip.read_sheets(xlsx(49), "a.xlsx")[0][1]) == 50
    with pytest.raises(ip.TooManyRows):
        ip.read_sheets(xlsx(50), "a.xlsx")
    with pytest.raises(ip.TooManyRows):
        ip.read_sheets(xlsx(10, blank_tail=1000), "a.xlsx")               # stops scanning, not just counting



# ── A workbook that unpacks to far more than a statement ─────────────────────

def _xlsx_bytes(rows):
    import openpyxl as _ox
    wb = _ox.Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_an_ordinary_workbook_passes_the_unpacked_size_check():
    content = _xlsx_bytes([["Date", "Description", "Amount"], ["2026-10-01", "COFFEE", -4.5]])
    ip.check_xlsx_limits(content)
    assert ip.read_sheets(content, "f.xlsx")[0][1][1][1] == "COFFEE"


def test_a_workbook_whose_shared_strings_unpack_too_large_is_refused(monkeypatch):
    # Excel writes text to xl/sharedStrings.xml (openpyxl here writes it inline), which openpyxl
    # reads whole before any row: a crafted one compresses tiny and unpacks huge.
    import zipfile as _zf
    base = _xlsx_bytes([["Date", "Description", "Amount"]])
    buf = io.BytesIO()
    with _zf.ZipFile(io.BytesIO(base)) as src, _zf.ZipFile(buf, "w", _zf.ZIP_DEFLATED) as out:
        for item in src.infolist():
            out.writestr(item, src.read(item))
        out.writestr("xl/sharedStrings.xml", b"<sst>" + b"<si><t>x</t></si>" * 5_000 + b"</sst>")
    monkeypatch.setattr(ip, "MAX_XLSX_SHARED_STRINGS", 10_000)               # the part is ~85 KB here
    with pytest.raises(ip.FormatMismatch, match="too large once unpacked"):
        ip.read_sheets(buf.getvalue(), "f.xlsx")


def test_a_workbook_too_large_in_total_or_with_too_many_parts_is_refused(monkeypatch):
    content = _xlsx_bytes([["a"]])
    monkeypatch.setattr(ip, "MAX_XLSX_UNPACKED", 1_000)
    with pytest.raises(ip.FormatMismatch):
        ip.check_xlsx_limits(content)
    monkeypatch.setattr(ip, "MAX_XLSX_UNPACKED", 10**9)
    monkeypatch.setattr(ip, "MAX_XLSX_MEMBERS", 3)
    with pytest.raises(ip.FormatMismatch):
        ip.check_xlsx_limits(content)
