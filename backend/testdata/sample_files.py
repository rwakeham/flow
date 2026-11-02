"""
Synthetic statement files and the import formats that read them, named by layout shape.

Each sample is a common export layout (a workbook with a preamble above the header, a CSV
with a status, type or member column, separate debit and credit columns, a headerless TSV,
a ledger kept by hand), read with an ordinary import format (``FORMATS``). The rows each
should give are in ``expected_rows/<name>.json`` (test_sample_files.py).

Everything here is made up: names (JANE EXAMPLE), places (SPRINGFIELD IL), card and
account numbers, references and amounts. Never paste a real export in.
"""

from __future__ import annotations

import dataclasses
import io
from dataclasses import dataclass, field
from datetime import date

import openpyxl

import import_profile as ip


def _h(header: str, optional: bool = False) -> list[dict]:
    return [{"header": header, **({"optional": True} if optional else {})}]


def _i(index: int) -> list[dict]:
    return [{"index": index}]


STATUS_HEADER = ["Date", "Description", "Amount", "Check #", "Status"]
PREAMBLE_HEADER = ["Date", "Description", "Card Member", "Account #", "Amount", "Extended Details",
                   "Appears On Your Statement As", "Address", "City/State", "Zip Code", "Country", "Reference", "Category"]
PREAMBLE_COLUMNS = {
    "date": _h("Date"), "description": _h("Description"), "cardholder": _h("Card Member", True),
    "card_number": _h("Account #", True), "amount": _h("Amount"), "extended": _h("Extended Details", True),
    "statement_as": _h("Appears On Your Statement As", True), "city_state": _h("City/State", True),
    "country": _h("Country", True), "reference": _h("Reference", True), "issuer_category": _h("Category", True),
}
PREAMBLE = {"period": True, "cardholder_label": "Prepared for", "account_label": "Account Number"}

# Format definitions (Spec JSON), keyed by layout shape.
FORMATS: dict[str, dict] = {
    "bank_csv_status": {"kind": "cash", "has_header": True, "header_cells": STATUS_HEADER, "header_row": 0,
                        "columns": {"date": _h("Date"), "description": _h("Description"), "amount": _h("Amount"),
                                    "check_number": _h("Check #", True), "status": _h("Status", True)},
                        "sign": "negative_is_out", "skip_status_values": ["pending"], "on_bad_date": "skip"},
    "bank_tsv_headerless": {"kind": "cash", "has_header": False,
                            "columns": {"date": _i(0), "amount": _i(1), "check_number": _i(3), "description": _i(4)},
                            "sign": "negative_is_out", "on_bad_date": "skip"},
    "ledger_by_position": {"kind": "cash", "has_header": False, "row_source": "manual",
                           "columns": {"date": _i(0), "description": _i(1), "credit": _i(2), "debit": _i(3),
                                       "reconciled": _i(4)},
                           "amount_mode": "debit_credit", "sign": None, "reconciled_values": ["x"],
                           "collapse_whitespace": False, "on_bad_date": "skip"},
    "xlsx_preamble": {"kind": "card", "file_type": "xlsx", "has_header": True, "header_cells": PREAMBLE_HEADER,
                      "columns": PREAMBLE_COLUMNS, "sign": "positive_is_out", "preamble": PREAMBLE,
                      "collapse_whitespace": False, "on_bad_date": "skip"},
    "csv_preamble": {"kind": "card", "has_header": True, "header_cells": PREAMBLE_HEADER, "quote_char": '"',
                     "columns": PREAMBLE_COLUMNS, "sign": "positive_is_out", "preamble": PREAMBLE,
                     "collapse_whitespace": False, "on_bad_date": "skip"},
    "csv_type_column": {"kind": "card", "has_header": True, "quote_char": '"',
                        "header_cells": ["Transaction Date", "Post Date", "Description", "Category", "Type", "Amount",
                                         "Memo"],
                        "columns": {"date": _h("Transaction Date"), "post_date": _h("Post Date", True),
                                    "description": _h("Description"), "issuer_category": _h("Category", True),
                                    "issuer_type": _h("Type", True), "amount": _h("Amount"),
                                    "extended": _h("Memo", True)},
                        "sign": "negative_is_out", "collapse_whitespace": False, "on_bad_date": "skip"},
    "csv_debit_credit": {"kind": "card", "has_header": True, "quote_char": '"',
                         "header_cells": ["Transaction Date", "Posted Date", "Card No.", "Description", "Category",
                                          "Debit", "Credit"],
                         "columns": {"date": _h("Transaction Date"), "post_date": _h("Posted Date", True),
                                     "card_number": _h("Card No.", True), "description": _h("Description"),
                                     "issuer_category": _h("Category", True), "debit": _h("Debit"),
                                     "credit": _h("Credit")},
                         "amount_mode": "debit_credit", "sign": None, "collapse_whitespace": False,
                         "on_bad_date": "skip"},
    "csv_status_member": {"kind": "card", "has_header": True, "quote_char": '"',
                          "header_cells": ["Status", "Date", "Description", "Debit", "Credit", "Member Name"],
                          "columns": {"status": _h("Status", True), "date": _h("Date"), "description": _h("Description"),
                                      "debit": _h("Debit"), "credit": _h("Credit"),
                                      "cardholder": _h("Member Name", True)},
                          "amount_mode": "debit_credit", "sign": None, "skip_status_values": ["pending"],
                          "collapse_whitespace": False, "on_bad_date": "skip"},
    "csv_positive_charges": {"kind": "card", "has_header": True, "quote_char": '"',
                             "header_cells": ["Trans. Date", "Post Date", "Description", "Amount", "Category"],
                             "columns": {"date": _h("Trans. Date"), "post_date": _h("Post Date", True),
                                         "description": _h("Description"), "amount": _h("Amount"),
                                         "issuer_category": _h("Category", True)},
                             "sign": "positive_is_out", "collapse_whitespace": False, "on_bad_date": "skip"},
    "card_csv_status": {"kind": "card", "has_header": True, "header_cells": STATUS_HEADER, "quote_char": '"',
                        "columns": {"date": _h("Date"), "description": _h("Description"), "amount": _h("Amount"),
                                    "status": _h("Status", True)},
                        "sign": "negative_is_out", "skip_status_values": ["pending"], "collapse_whitespace": False,
                        "on_bad_date": "skip"},
    "card_tsv_headerless": {"kind": "card", "has_header": False, "quote_char": '"',
                            "columns": {"date": _i(0), "amount": _i(1), "description": _i(4)},
                            "sign": "negative_is_out", "collapse_whitespace": False, "on_bad_date": "skip"},
    "csv_minimal": {"kind": "card", "has_header": True, "sign": "positive_is_out",
                    "columns": {"date": _h("Date"), "description": _h("Description"), "amount": _h("Amount")}},
    "csv_accounting": {"kind": "card", "has_header": True, "sign": "negative_is_out",
                       "columns": {"date": _h("Transaction Date"), "description": _h("Description"),
                                   "amount": _h("Amount")}},
}


def spec(key: str) -> ip.Spec:
    return ip.Spec.from_json(FORMATS[key])


def preamble_xlsx(rows: list[list]) -> bytes:
    """A workbook: six preamble rows, the header, rows, and a summary sheet."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Transaction Details"
    ws.append(["Transaction Details", "Example Rewards Card / Sep 22, 2026 to Oct 22, 2026"])
    ws.append(["Prepared for", ""])
    ws.append(["JANE EXAMPLE", ""])
    ws.append(["Account Number", ""])
    ws.append(["XXXX-XXXXXX-99999", ""])
    ws.append(["", ""])
    ws.append(PREAMBLE_HEADER)
    for r in rows:
        ws.append(r)
    summary = wb.create_sheet("Transaction Summary")
    summary.append(["Previous Balance", 100.0])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def preamble_row(d, desc, amount, city="SPRINGFIELD\nIL", category="Restaurant-Restaurant", ref="100000000000000001",
                 extended=""):
    return [d, desc, "JANE EXAMPLE", "-99999", amount, extended, desc, "1 MAIN ST", city, "00000",
            "UNITED STATES", ref, category]


@dataclass
class Sample:
    name: str
    filename: str
    content: bytes
    reads_as: str                     # "card" | "bank" | "ledger"
    format: str                       # key in FORMATS
    extra: dict = field(default_factory=dict)


def samples() -> list[Sample]:
    return [
        # ── card statements ──────────────────────────────────────────────────────
        Sample("card_xlsx_preamble", "activity.xlsx", preamble_xlsx([
            preamble_row("10/21/2026", "GREEN BOWL - SPRINSPRINGFIELD          IL", 32.01, ref="1"),
            preamble_row("10/16/2026", "AUTOPAY PAYMENT - THANK YOU", -500.00, city="", category="", ref="2"),
            preamble_row("10/01/2026", "EXAMPLEFITNESS.COM 555-0100            IL", 275.34, ref="3",
                         category="Entertainment-Associations", extended="10000000001MONTHLY DUES"),
        ]), "card", "xlsx_preamble"),
        Sample("card_xlsx_preamble_two_cards", "activity.xlsx", preamble_xlsx([
            preamble_row("10/01/2026", "LUNCH PLACE", 12.00, ref="8"),
            [*preamble_row("10/02/2026", "COFFEE SHOP", 5.00, ref="9")[:3], "-51002",
             *preamble_row("10/02/2026", "COFFEE SHOP", 5.00, ref="9")[4:]],
        ]), "card", "xlsx_preamble"),
        Sample("card_csv_status", "CreditCard.csv",
               b'"DATE","DESCRIPTION","AMOUNT","CHECK #","STATUS"\n'
               b'"10/21/2026","CIRCLEK#1000001 SPRINGFIELD IL","-38.80",,"Posted"\n'
               b'"10/20/2026","TARGET T-0001 SPRINGFIELD IL","-302.14",,"Posted"\n'
               b'"10/19/2026","NETFLIX.COM LOS GATOS CA","-15.49",,"Pending"\n',
               "card", "card_csv_status"),
        Sample("card_csv_type_column", "card.csv",
               b"Transaction Date,Post Date,Description,Category,Type,Amount,Memo\n"
               b"10/02/2026,10/03/2026,STARBUCKS 123,Food & Drink,Sale,-6.25,\n"
               b"10/05/2026,10/05/2026,Payment Thank You-Mobile,,Payment,250.00,\n",
               "card", "csv_type_column"),
        Sample("card_csv_debit_credit", "card.csv",
               b"Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
               b"2026-10-02,2026-10-03,1234,SHELL OIL 57444,Gas/Automotive,41.20,\n"
               b"2026-10-04,2026-10-04,1234,REFUND AMAZON,Merchandise,,19.99\n",
               "card", "csv_debit_credit"),
        Sample("card_csv_status_member", "card.csv",
               b"Status,Date,Description,Debit,Credit,Member Name\n"
               b"Cleared,10/02/2026,COSTCO WHSE #123,88.10,,JANE EXAMPLE\n"
               b"Pending,10/03/2026,UBER TRIP,12.00,,JANE EXAMPLE\n",
               "card", "csv_status_member"),
        Sample("card_csv_positive_charges", "card.csv",
               b"Trans. Date,Post Date,Description,Amount,Category\n"
               b"10/02/2026,10/03/2026,KROGER #123,54.10,Supermarkets\n",
               "card", "csv_positive_charges"),
        Sample("card_tsv_headerless", "export.txt",
               b"10/02/2026\t-12.50\t*\t\tCHIPOTLE 1234 SPRINGFIELD IL\n"
               b"10/03/2026\t-4.00\t*\t\tCORNER COFFEE\n",
               "card", "card_tsv_headerless"),
        Sample("card_csv_minimal", "x.csv",
               b"Date,Description,Amount\n10/02/2026,SOMETHING,12.00\n10/03/2026,OTHER,13.00\n",
               "card", "csv_minimal"),
        Sample("card_csv_accounting", "x.csv",
               b'Transaction Date,Description,Amount\n"Oct 2, 2026",HOTEL,"(1,234.50)"\n',
               "card", "csv_accounting"),

        # ── bank statements and ledgers ──────────────────────────────────────────
        Sample("bank_csv_status", "Checking1.csv",
               b'Date,Description,Amount,Check #,Status\n'
               b'10/01/2026,"PAYROLL  EXAMPLE CORP   DIR DEP",2478.30,,Posted\n'
               b'10/02/2026,"CHECK # 1001",-120.00,1001,Posted\n'
               b'10/03/2026,"GROCERY STORE   SPRINGFIELD IL","-$1,045.12",,Posted\n'
               b'10/04/2026,"COFFEE SHOP",-4.50,,Pending\n',
               "bank", "bank_csv_status"),
        Sample("bank_tsv_status", "Checking1.txt",
               b"Date\tDescription\tAmount\tCheck #\tStatus\n"
               b"10/01/2026\tPAYROLL  EXAMPLE CORP\t2,478.30\t\tPosted\n"
               b"10/02/2026\tCHECK # 1002 \"MEMO\"\t-75.00\t1002\tPosted\n"
               b"10/05/2026\tUTILITY CO   AUTOPAY\t-88.21\t\tPending\n",
               "bank", "bank_csv_status"),
        Sample("bank_tsv_headerless", "Checking1.txt",
               b"10/01/2026\t2478.30\t*\t\tPAYROLL   EXAMPLE CORP DIR DEP\n"
               b"10/02/2026\t-120.00\t*\t1001\tCHECK 1001\n"
               b"10/03/2026\t-1,045.12\t*\t\tGROCERY STORE SPRINGFIELD IL\n",
               "bank", "bank_tsv_headerless"),
        Sample("ledger_with_header", "ledger.tsv",
               b"Date\tDescription\tCredit\tDebit\tReconciled\tBalance\n"
               b"10/01/2026\tOpening balance\t\t\t\t$5,000.00\n"
               b"10/01/2026\tPaycheck\t$2,478.30\t\tX\t$7,478.30\n"
               b"10/02/2026\tMortgage\t\t$(1,500.00)\tx\t$5,978.30\n"
               b"10/03/2026\tGroceries\t\t($120.55)\t\t$5,857.75\n"
               b"\tNotes row without a date\t\t$10.00\t\t\n"
               b"10/30/2026\t\t\t\t\t\n",
               "ledger", "ledger_by_position"),
        Sample("ledger_headerless", "ledger.csv",
               b'10/01/2026,Paycheck,"$2,478.30",,X,\n'
               b'10/02/2026,Rent,,"$(1,200.00)",,\n'
               b'10/02/2026,Placeholder,,,,\n'
               b'11/01/2026,Next month rent,,"($1,200.00)",,\n',
               "ledger", "ledger_by_position"),
    ]


def by_name(name: str) -> Sample:
    return next(s for s in samples() if s.name == name)


def plain(v):
    """Dataclasses, dates and lists as JSON-ready values (the expected files' form)."""
    if dataclasses.is_dataclass(v):
        return {f.name: plain(getattr(v, f.name)) for f in dataclasses.fields(v)}
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return [plain(x) for x in v]
    if isinstance(v, dict):
        return {k: plain(x) for k, x in v.items()}
    return v
