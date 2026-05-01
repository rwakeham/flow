"""
Parser for two supported import formats:

1. Bank export (Wells Fargo tab-separated):
       Date\tAmount\t*\tCheckNum\tDescription
   Column 1 is a numeric amount → detected as bank format.
   Returns BankRow objects (source='bank').

2. Ledger/spreadsheet export (user's manual tracking sheet):
       Date\tDescription\tCredit\tDebit\tReconciled\tBalance\t...
   Column 1 is a text description → detected as ledger format.
   Credit column: '$2,478.30' or empty.
   Debit column:  '$(3,099.54)' or '($3,099.54)' (accounting negative) or empty.
   Reconciled column: 'X' or empty.
   Returns LedgerRow objects (source='manual').

Auto-detects delimiter (tab or comma/CSV), strips BOM, handles quoted fields.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class FileFormat(Enum):
    BANK = "bank"
    LEDGER = "ledger"
    UNKNOWN = "unknown"


@dataclass
class BankRow:
    date: date
    amount: float
    bank_description: str
    check_number: str | None


@dataclass
class LedgerRow:
    date: date
    description: str
    amount: float          # positive=credit, negative=debit
    is_reconciled: bool


@dataclass
class ParseResult:
    format: FileFormat
    bank_rows: list[BankRow]
    ledger_rows: list[LedgerRow]


def parse_bank_export(content: bytes) -> list[BankRow]:
    """Backwards-compatible helper: parse a bank export and return BankRow list."""
    result = parse_register_import(content)
    if result.format == FileFormat.LEDGER:
        raise ValueError("File appears to be a ledger export, not a bank export")
    return result.bank_rows


def parse_register_import(content: bytes) -> ParseResult:
    """
    Auto-detect format and delimiter, then parse both bank and ledger exports.
    Returns a ParseResult with format indicator and the appropriate row list populated.
    """
    text = _decode(content)
    rows = _parse_to_rows(text)   # list[list[str]] — all non-empty rows as field lists

    if not rows:
        return ParseResult(format=FileFormat.UNKNOWN, bank_rows=[], ledger_rows=[])

    fmt = _detect_format(rows)

    if fmt == FileFormat.BANK:
        return ParseResult(format=FileFormat.BANK, bank_rows=_build_bank_rows(rows), ledger_rows=[])
    elif fmt == FileFormat.LEDGER:
        return ParseResult(format=FileFormat.LEDGER, bank_rows=[], ledger_rows=_build_ledger_rows(rows))
    else:
        return ParseResult(format=FileFormat.UNKNOWN, bank_rows=[], ledger_rows=[])


# ── Decoding & splitting ───────────────────────────────────────────────────────

def _decode(content: bytes) -> str:
    # Strip UTF-8 BOM if present, then decode
    if content.startswith(b"\xef\xbb\xbf"):
        content = content[3:]
    # Try UTF-8 first, then Latin-1 as fallback
    for enc in ("utf-8", "latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _parse_to_rows(text: str) -> list[list[str]]:
    """
    Split text into rows of fields. Auto-detects tab vs comma delimiter.
    Filters out completely empty rows. Strips whitespace from each field.
    """
    lines = text.splitlines()
    if not lines:
        return []

    # Detect delimiter: use tab if any line contains a tab, else try CSV
    has_tabs = any("\t" in line for line in lines[:20])

    rows: list[list[str]] = []
    if has_tabs:
        for line in lines:
            if not line.strip():
                continue
            fields = [f.strip() for f in line.split("\t")]
            if any(f for f in fields):  # at least one non-empty field
                rows.append(fields)
    else:
        # CSV mode — use the csv module to handle quoted fields correctly
        reader = csv.reader(io.StringIO(text))
        for fields in reader:
            stripped = [f.strip() for f in fields]
            if any(f for f in stripped):
                rows.append(stripped)

    return rows


# ── Format detection ──────────────────────────────────────────────────────────

def _detect_format(rows: list[list[str]]) -> FileFormat:
    """
    Scan up to the first 20 rows with a parseable date to determine format:
    - Bank:   col 0 = date, col 1 = numeric amount
    - Ledger: col 0 = date, col 1 = text description
    """
    checked = 0
    for fields in rows:
        if not fields or len(fields) < 2:
            continue
        try:
            _parse_date(fields[0])
        except ValueError:
            continue  # header or non-date row

        checked += 1
        col1 = fields[1].replace(",", "").replace("$", "").strip()
        if col1:
            try:
                float(col1)
                return FileFormat.BANK
            except ValueError:
                return FileFormat.LEDGER
        # Empty col1 — can't classify from this row alone; keep scanning.
        if checked >= 20:
            break

    return FileFormat.UNKNOWN


# ── Bank format ───────────────────────────────────────────────────────────────

def _build_bank_rows(rows: list[list[str]]) -> list[BankRow]:
    result: list[BankRow] = []
    for fields in rows:
        if len(fields) < 2:
            continue
        try:
            txn_date = _parse_date(fields[0])
        except ValueError:
            continue
        try:
            amount = float(fields[1].replace(",", ""))
        except ValueError:
            continue

        check_number: str | None = None
        if len(fields) >= 4:
            ck = fields[3]
            if ck:
                check_number = ck

        if len(fields) >= 5:
            description = " ".join(fields[4:]).strip()
        elif len(fields) == 4 and check_number and not check_number.isdigit():
            description = check_number
            check_number = None
        else:
            description = ""

        result.append(BankRow(
            date=txn_date,
            amount=amount,
            bank_description=description,
            check_number=check_number,
        ))
    return result


# ── Ledger format ─────────────────────────────────────────────────────────────

def _build_ledger_rows(rows: list[list[str]]) -> list[LedgerRow]:
    """
    Parse the user's manual ledger spreadsheet:
        Date  Description  Credit  Debit  ReconcileFlag  Balance  ...

    Rows with no credit AND no debit (e.g. opening balance markers, blank filler
    rows, and future placeholder rows) are skipped — they carry no transaction.
    Rows with no date are also skipped.
    """
    result: list[LedgerRow] = []
    for fields in rows:
        if len(fields) < 2:
            continue

        # Skip rows with no date
        try:
            txn_date = _parse_date(fields[0])
        except ValueError:
            continue

        description = fields[1] if len(fields) > 1 else ""
        if not description:
            continue

        credit_str = fields[2] if len(fields) > 2 else ""
        debit_str  = fields[3] if len(fields) > 3 else ""

        credit = _parse_accounting_amount(credit_str)
        debit  = _parse_accounting_amount(debit_str)

        # Skip rows with no usable amount (balance markers, placeholders)
        if (credit is None or credit == 0) and (debit is None or debit == 0):
            continue

        if credit is not None and credit != 0:
            amount = credit           # positive = money in
        else:
            amount = -abs(debit)      # negative = money out

        # Reconciled flag: col 4, 'X' or 'x'
        rec_str = fields[4].strip() if len(fields) > 4 else ""
        is_reconciled = rec_str.upper() == "X"

        result.append(LedgerRow(
            date=txn_date,
            description=description,
            amount=amount,
            is_reconciled=is_reconciled,
        ))
    return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_accounting_amount(s: str) -> float | None:
    """
    Parse dollar amounts in accounting notation:
      '$2,478.30'    →  2478.30
      '$(3,099.54)'  →  3099.54  (caller negates if this is a debit)
      '($3,099.54)'  →  3099.54
      ''             →  None
    """
    s = s.strip()
    if not s:
        return None
    cleaned = re.sub(r"[$,()\s]", "", s).strip()
    if not cleaned:
        return None
    try:
        return abs(float(cleaned))
    except ValueError:
        return None


def _parse_date(s: str) -> date:
    """
    Parse date strings in common US-export CSV formats.

    Slash/dash formats are interpreted as month-first (MM/DD/YYYY). Files using
    DD/MM ordering are not supported — the ambiguity would silently misread
    dates like 03/04/2026.
    """
    s = s.strip()
    # ISO formats first since they are unambiguous.
    for fmt in (
        "%Y-%m-%d",      # 2026-01-15
        "%Y/%m/%d",      # 2026/01/15
        "%m/%d/%y",      # 1/15/26 or 01/15/26
        "%m/%d/%Y",      # 1/15/2026 or 01/15/2026
        "%m-%d-%y",      # 01-15-26
        "%m-%d-%Y",      # 01-15-2026
    ):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Cannot parse date: {s!r}")
