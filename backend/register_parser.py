"""
Parser for bank export files (Wells Fargo tab-separated format).

Expected format (tab-separated, no header row):
    Date\tAmount\t*\tCheckNum\tDescription

- Date: M/D/YY  (e.g. 4/1/26)
- Amount: float, negative=debit, positive=credit
- Column 3: '*' or blank (reconciled flag — ignored)
- Column 4: check number or blank
- Column 5+: description (remainder of line joined)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime


@dataclass
class BankRow:
    date: date
    amount: float
    bank_description: str
    check_number: str | None


def parse_bank_export(content: bytes) -> list[BankRow]:
    """Parse a Wells Fargo tab-separated export and return a list of BankRow objects."""
    text = content.decode("utf-8", errors="replace")
    rows: list[BankRow] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Split on tabs; fall back to splitting on 2+ whitespace if no tabs found
        if "\t" in line:
            parts = line.split("\t")
        else:
            parts = re.split(r"\s{2,}", line)

        if len(parts) < 2:
            continue

        # --- Date (column 0) ---
        date_str = parts[0].strip()
        try:
            txn_date = _parse_date(date_str)
        except ValueError:
            # If the first field isn't a date, skip the line (e.g. header rows)
            continue

        # --- Amount (column 1) ---
        amount_str = parts[1].strip().replace(",", "")
        try:
            amount = float(amount_str)
        except ValueError:
            continue

        # --- Reconciled flag (column 2) — ignored ---
        # --- Check number (column 3) ---
        check_number: str | None = None
        if len(parts) >= 4:
            ck = parts[3].strip()
            if ck:
                check_number = ck

        # --- Description (column 4+, rest of line joined) ---
        if len(parts) >= 5:
            description = "\t".join(parts[4:]).strip()
        elif len(parts) == 4:
            # Some exports omit the check-number column; treat col 3 as description
            # if it looks like text rather than a number
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


def _parse_date(s: str) -> date:
    """Parse M/D/YY or M/D/YYYY date strings."""
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Cannot parse date: {s!r}")
