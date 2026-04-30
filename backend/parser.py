"""
Parse wide-format CSV or XLSX files into a structured list of accounts + rows.

Expected input format:
  date column (any of: date, month, period, as of) + one column per account
  Each row = one time period (monthly).
"""

import io
from dataclasses import dataclass
from typing import Literal

import pandas as pd

LIABILITY_KEYWORDS = frozenset(
    {"mortgage", "loan", "credit", "debt", "heloc", "line"}
)


def detect_account_type(name: str) -> Literal["asset", "liability"]:
    lower = name.lower()
    if any(kw in lower for kw in LIABILITY_KEYWORDS):
        return "liability"
    return "asset"


@dataclass
class ParsedAccount:
    name: str
    detected_type: Literal["asset", "liability"]


@dataclass
class ParseResult:
    accounts: list[ParsedAccount]
    # rows[i] = {"period": date, "values": {account_name: float}}
    rows: list[dict]


DATE_COLUMN_NAMES = {"date", "month", "period", "as of", "as_of"}


def _find_date_column(df: pd.DataFrame) -> str:
    for col in df.columns:
        if str(col).strip().lower() in DATE_COLUMN_NAMES:
            return col
    return df.columns[0]


def parse_upload(content: bytes, filename: str) -> ParseResult:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    try:
        if ext == "xlsx":
            df = pd.read_excel(io.BytesIO(content), engine="openpyxl", sheet_name=0)
        else:
            df = pd.read_csv(io.BytesIO(content))
    except Exception as exc:
        raise ValueError(f"Could not read file: {exc}") from exc

    df.columns = [str(c).strip() for c in df.columns]

    date_col = _find_date_column(df)
    try:
        df[date_col] = pd.to_datetime(df[date_col])
    except Exception:
        raise ValueError(
            f"Could not parse dates in column '{date_col}'. "
            "Expected a column named 'date', 'month', 'period', or 'as of' "
            "containing valid date values."
        )

    # Normalize to first of month
    df[date_col] = df[date_col].dt.to_period("M").dt.to_timestamp()
    df = df.dropna(subset=[date_col])

    account_cols = [c for c in df.columns if c != date_col]
    if not account_cols:
        raise ValueError("No account columns found after the date column.")

    accounts = [
        ParsedAccount(name=col, detected_type=detect_account_type(col))
        for col in account_cols
    ]

    for col in account_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    rows = []
    for _, row in df.iterrows():
        period = row[date_col].date()
        values = {}
        for col in account_cols:
            val = row[col]
            if pd.notna(val):
                values[col] = float(val)
        rows.append({"period": period, "values": values})

    return ParseResult(accounts=accounts, rows=rows)
