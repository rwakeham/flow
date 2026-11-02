"""
The file outline the Assistant may see (import_infer.outline) carries no data.

A file packed with a name, card and account numbers, an address, a phone number
and merchant descriptions, in its preamble, its header and its rows, must
produce an outline with none of them: only cleaned header cells and an
enumerated shape per column.
"""

from __future__ import annotations

import io
import json

import openpyxl

import import_infer as inf

SECRETS = ["JANE", "EXAMPLE", "4111", "1111", "99999", "555-0100", "123 ELM", "SPRINGFIELD", "GREEN BOWL",
           "PAYROLL", "2478.30", "32.01", "jane@example.com", "Sep 22", "1002"]


def _check(outline: dict) -> None:
    text = json.dumps(outline)
    for s in SECRETS:
        assert s.lower() not in text.lower(), s
    for c in outline["columns"]:
        assert set(c) == {"index", "header", "shape"}
        assert c["shape"] in inf.SHAPES
    assert set(outline) == {"file_type", "delimiter", "header_row", "rows_above_header", "data_rows", "columns"}


def test_csv_outline_has_no_values():
    content = (b"Statement for JANE EXAMPLE,account 4111 1111 1111 1111\n"
               b"Date,Description,Amount,Balance,Card 1002 JANE,Jane's Notes\n"
               b"10/21/2026,GREEN BOWL SPRINGFIELD 555-0100,-32.01,100.00,4111111111111111,123 ELM ST\n"
               b"10/22/2026,PAYROLL EXAMPLE CORP,2478.30,2578.30,4111111111111111,jane@example.com\n")
    out = inf.outline(content, "f.csv")
    _check(out)
    shapes = {c["header"]: c["shape"] for c in out["columns"]}
    assert shapes["Date"] == "date:MDY" and shapes["Amount"] == "money:mixed" and shapes["Description"] == "text"
    assert out["rows_above_header"] == 1


def test_xlsx_preamble_never_reaches_the_outline():
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in (["Activity", "Example Card / Sep 22, 2026 to Oct 22, 2026"], ["Prepared for"], ["JANE EXAMPLE"],
              ["Account Number"], ["XXXX-XXXXXX-99999"],
              ["Date", "Description", "Card Member", "Amount"],
              ["10/21/2026", "GREEN BOWL", "JANE EXAMPLE", 32.01]):
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    _check(inf.outline(buf.getvalue(), "f.xlsx"))


def test_safe_header():
    assert inf.safe_header("Transaction Date") == "Transaction Date"
    assert inf.safe_header("Card 1002") == "~"
    assert inf.safe_header("JANE EXAMPLE'S ACCOUNT FOR THE MONTH OF OCTOBER") == "~"
    assert inf.safe_header("someone@example.com") == "~"
