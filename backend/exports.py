"""
CSV exports (pure builders, plus the one response helper the routers share): the card transactions
(routers/cards), the cash register with its running balance (routers/register), the balance-sheet
history (routers/data) and the year-end card summary (card_analytics.year_summary). Every text cell
goes through ``spreadsheet_safe``; numbers are written as numbers so a spreadsheet can add them.
"""
from __future__ import annotations

import csv
import io
from urllib.parse import quote

from fastapi.responses import StreamingResponse

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def spreadsheet_safe(value):
    """
    A text cell a spreadsheet won't run: one starting with = + - @ (or a tab or carriage return)
    is a formula when the CSV is opened in Excel, so it gets a leading apostrophe. Statement text
    (merchant names, notes) can come from anyone. Numbers are written as they are.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def download_disposition(name: str, suffix: str) -> str:
    """
    Content-Disposition for a download named after something the user typed. HTTP headers carry
    Latin-1 only, so the plain filename keeps ASCII letters, digits, space, - and _ (a name in
    Cyrillic or Chinese used to fail with a 500); filename* (RFC 6266) carries the real name in
    UTF-8 for browsers that read it.
    """
    ascii_name = "".join(ch if (ch.isascii() and ch.isalnum()) or ch in " -_" else "_" for ch in name).strip(" _")
    real = "".join(ch for ch in name if ch.isprintable() and ch not in '/\\"').strip() or "format"
    return (f'attachment; filename="{ascii_name or "format"}{suffix}"; '
            f"filename*=UTF-8''{quote(real + suffix, safe='')}")


def cell(value):
    """One CSV cell: money as two decimals, counts as they are, text made formula-safe, None blank."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else ""
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, int):
        return str(value)
    return spreadsheet_safe(str(value))


def csv_text(header: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for row in rows:
        w.writerow([cell(c) for c in row])
    return buf.getvalue()


def csv_response(header: list[str], rows: list[list], name: str, suffix: str = ".csv") -> StreamingResponse:
    """The CSV as a download named after ``name`` (any text: download_disposition makes it a header)."""
    return StreamingResponse(iter([csv_text(header, rows)]), media_type="text/csv",
                             headers={"Content-Disposition": download_disposition(name, suffix)})


# ── The cash register ────────────────────────────────────────────────────────

REGISTER_COLUMNS = ["date", "description", "credit", "debit", "balance", "source", "bank_description",
                    "reconciled", "matched", "notes"]


def register_rows(txns: list[dict]) -> list[list]:
    """The rows routers/register._compute_running_balance returns (date order, each with ``balance``):
    one CSV row each, the amount split into credit / debit. A matched bank row is listed with the
    balance unchanged, as the register shows it."""
    out = []
    for t in txns:
        amount = float(t["amount"])
        out.append([t["date"], t["description"] or "", amount if amount > 0 else None, -amount if amount < 0 else None,
                    float(t["balance"]), t["source"], t.get("bank_description") or "", bool(t.get("is_reconciled")),
                    t.get("matched_to_id") is not None, t.get("notes") or ""])
    return out


# ── The balance sheet ────────────────────────────────────────────────────────

BALANCES_LONG_COLUMNS = ["account", "type", "institution", "period", "amount", "source", "updated_at", "ignored", "archived"]


def balances_long_rows(rows: list[dict]) -> list[list]:
    """Every stored balance as it is (no carry-forward): dicts with the column names above."""
    return [[r["account"], r["type"], r.get("institution") or "", r["period"], float(r["amount"]), r.get("source") or "",
             r.get("updated_at") or "", bool(r.get("ignored")), bool(r.get("archived"))] for r in rows]


def balances_wide_rows(periods: list, accounts: list, matrix: dict) -> tuple[list[str], list[list]]:
    """One row per month, one column per account, as the Dashboard draws them (a month an account
    wasn't updated carries its last value, and the ``carried`` column names those accounts), then the
    totals routers/data.summarize computes: ignored accounts are shown but left out of them. Returns
    (header, rows). Liabilities are stored negative and written so."""
    header = ["period"] + [f"{a.name} (ignored)" if a.ignored else a.name for a in accounts] + \
             ["assets", "liabilities", "net_worth", "carried"]
    rows = []
    for i, period in enumerate(periods):
        values = [matrix[a.id][0][i] for a in accounts]
        live = [(a, v) for a, v in zip(accounts, values) if not a.ignored and v is not None]
        assets = sum(v for a, v in live if a.account_type == "asset")
        liabs = sum(v for a, v in live if a.account_type != "asset")
        carried = ", ".join(a.name for a in accounts if matrix[a.id][1][i] and matrix[a.id][0][i] is not None)
        rows.append([str(period)] + [None if v is None else float(v) for v in values]
                    + [float(assets), float(liabs), float(assets + liabs), carried])
    return header, rows


# ── The year-end card summary ────────────────────────────────────────────────

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
YEAR_SUMMARY_COLUMNS = ["source", "category", "subcategory", "tax_note", "net", "gross", "refunds", "count"] + MONTHS


def year_summary_rows(summary: list[dict]) -> list[list]:
    return [[r["source"], r["category"], r["subcategory"], r["tax_note"], r["net"], r["gross"], r["refunds"], r["count"],
             *r["months"]] for r in summary]
