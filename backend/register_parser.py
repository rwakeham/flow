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
   Debit column:  '$(3,099.54)' (accounting negative) or empty.
   Reconciled column: 'X' or empty.
   Returns LedgerRow objects (source='manual').
"""

from __future__ import annotations

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
    """
    Backwards-compatible helper: parse a bank export and return BankRow list.
    Raises ValueError if the file is not in bank format.
    """
    result = parse_register_import(content)
    if result.format == FileFormat.LEDGER:
        raise ValueError("File appears to be a ledger export, not a bank export")
    return result.bank_rows


def parse_register_import(content: bytes) -> ParseResult:
    """
    Auto-detect format and parse both bank and ledger exports.
    Returns a ParseResult with format indicator and the appropriate row list populated.
    """
    text = content.decode("utf-8", errors="replace")
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    if not lines:
        return ParseResult(format=FileFormat.UNKNOWN, bank_rows=[], ledger_rows=[])

    # Detect format from first parseable line
    detected = _detect_format(lines)

    if detected == FileFormat.BANK:
        rows = _parse_bank_lines(lines)
        return ParseResult(format=FileFormat.BANK, bank_rows=rows, ledger_rows=[])
    elif detected == FileFormat.LEDGER:
        rows = _parse_ledger_lines(lines)
        return ParseResult(format=FileFormat.LEDGER, bank_rows=[], ledger_rows=rows)
    else:
        return ParseResult(format=FileFormat.UNKNOWN, bank_rows=[], ledger_rows=[])


def _split_line(line: str) -> list[str]:
    if "\t" in line:
        return line.split("\t")
    return re.split(r"\s{2,}", line)


def _detect_format(lines: list[str]) -> FileFormat:
    """
    Examine the first few parseable lines to determine file format.
    Bank format: col 0 = date, col 1 = numeric amount.
    Ledger format: col 0 = date, col 1 = text description.
    """
    for line in lines[:5]:
        parts = _split_line(line)
        if len(parts) < 2:
            continue
        try:
            _parse_date(parts[0].strip())
        except ValueError:
            continue  # not a date row, skip

        col1 = parts[1].strip().replace(",", "")
        try:
            float(col1)
            return FileFormat.BANK
        except ValueError:
            # col1 is text — ledger format
            if col1:  # non-empty text description
                return FileFormat.LEDGER

    return FileFormat.UNKNOWN


# ── Bank format ───────────────────────────────────────────────────────────────

def _parse_bank_lines(lines: list[str]) -> list[BankRow]:
    rows: list[BankRow] = []
    for line in lines:
        parts = _split_line(line)
        if len(parts) < 2:
            continue

        try:
            txn_date = _parse_date(parts[0].strip())
        except ValueError:
            continue

        amount_str = parts[1].strip().replace(",", "")
        try:
            amount = float(amount_str)
        except ValueError:
            continue

        check_number: str | None = None
        if len(parts) >= 4:
            ck = parts[3].strip()
            if ck:
                check_number = ck

        if len(parts) >= 5:
            description = "\t".join(parts[4:]).strip()
        elif len(parts) == 4:
            if check_number and not check_number.isdigit():
                description = check_number
                check_number = None
            else:
                description = ""
        else:
            description = ""

        rows.append(BankRow(
            date=txn_date,
            amount=amount,
            bank_description=description,
            check_number=check_number,
        ))
    return rows


# ── Ledger format ─────────────────────────────────────────────────────────────

def _parse_ledger_lines(lines: list[str]) -> list[LedgerRow]:
    """
    Parse the user's manual ledger spreadsheet format:
        Date  Description  Credit  Debit  ReconcileFlag  Balance  ReconcileBalance
    Credit and Debit are mutually exclusive per row.
    Amounts use accounting notation: $(3,099.54) for negatives.
    """
    rows: list[LedgerRow] = []
    for line in lines:
        parts = _split_line(line)
        if len(parts) < 2:
            continue

        try:
            txn_date = _parse_date(parts[0].strip())
        except ValueError:
            continue

        description = parts[1].strip()
        if not description:
            continue

        # Credit (col 2) and Debit (col 3)
        credit_str = parts[2].strip() if len(parts) > 2 else ""
        debit_str  = parts[3].strip() if len(parts) > 3 else ""

        credit = _parse_accounting_amount(credit_str)
        debit  = _parse_accounting_amount(debit_str)

        if credit is None and debit is None:
            continue  # no usable amount

        # Credit is positive, debit is negative (debit values are stored as positive
        # in the accounting format $(X), we negate them here)
        if credit is not None and credit != 0:
            amount = credit
        elif debit is not None and debit != 0:
            amount = -abs(debit)
        else:
            continue

        # Reconciled flag (col 4): 'X' or 'x' means reconciled
        rec_str = parts[4].strip() if len(parts) > 4 else ""
        is_reconciled = rec_str.upper() == "X"

        rows.append(LedgerRow(
            date=txn_date,
            description=description,
            amount=amount,
            is_reconciled=is_reconciled,
        ))
    return rows


def _parse_accounting_amount(s: str) -> float | None:
    """
    Parse dollar amounts in accounting notation:
      '$2,478.30'    →  2478.30
      '$(3,099.54)'  →  3099.54  (caller negates if this is a debit)
      ''             →  None
    """
    s = s.strip()
    if not s:
        return None
    # Remove $, spaces, commas, and accounting parens
    cleaned = s.replace("$", "").replace(",", "").replace("(", "").replace(")", "").strip()
    if not cleaned:
        return None
    try:
        return abs(float(cleaned))
    except ValueError:
        return None


def _parse_date(s: str) -> date:
    """Parse M/D/YY or M/D/YYYY date strings."""
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Cannot parse date: {s!r}")
