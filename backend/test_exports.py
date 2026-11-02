"""
CSV exports (exports.py, pure): formula-safe text cells, numbers kept numeric, the register's
running balance as the API computes it, and the balance sheet in its long and wide layouts.
"""

from __future__ import annotations

import csv
import io
import os
from datetime import date
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-" * 8)

import exports  # noqa: E402
from routers.register import _compute_running_balance  # noqa: E402


def parse(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def test_cells_are_formula_safe_and_numbers_stay_numbers():
    rows = parse(exports.csv_text(["a", "b", "c", "d", "e"], [["=SUM(A1)", -12.5, 3, True, None], ["-x", "plain", 0.0, False, "@y"]]))
    assert rows[1] == ["'=SUM(A1)", "-12.50", "3", "yes", ""]
    assert rows[2] == ["'-x", "plain", "0.00", "", "'@y"]


def test_csv_response_names_the_file_after_the_account():
    res = exports.csv_response(["a"], [["1"]], "Chequing — Ménage", ".csv")
    cd = res.headers["content-disposition"]
    assert cd.startswith('attachment; filename="Chequing') and "filename*=UTF-8''" in cd
    assert res.media_type == "text/csv"


def txn(i, d, amount, source="manual", matched=None, reconciled=False, bank=None, notes=None):
    return SimpleNamespace(id=i, register_account_id=1, date=d, description=f"Row {i}", amount=amount, source=source,
                           bank_description=bank, matched_to_id=matched, is_reconciled=reconciled, notes=notes)


def test_register_rows_carry_the_apis_running_balance():
    txns = [txn(1, date(2026, 1, 2), 100.0, reconciled=True),
            txn(2, date(2026, 1, 3), -40.25, bank="=CHEQUE 12", source="bank"),          # unmatched bank row: counts
            txn(3, date(2026, 1, 4), -10.0, source="bank", matched=1, bank="POS 1"),       # matched: balance unchanged
            txn(4, date(2026, 1, 5), -5.0, notes="-note")]
    result = _compute_running_balance(50.0, txns)
    rows = exports.register_rows(result["transactions"])
    assert [r[4] for r in rows] == [t["balance"] for t in result["transactions"]]
    assert rows[0][2] == 100.0 and rows[0][3] is None and rows[0][7] is True
    assert rows[1][3] == 40.25 and rows[1][4] == 109.75 and rows[1][6] == "=CHEQUE 12"
    assert rows[2][4] == rows[1][4] and rows[2][8] is True                               # matched, same balance
    assert rows[3][9] == "-note"
    text = exports.csv_text(exports.REGISTER_COLUMNS, rows)
    assert "'=CHEQUE 12" in text and "'-note" in text and "109.75" in text


def test_balances_long_rows_keep_every_stored_balance():
    rows = exports.balances_long_rows([{"account": "A", "type": "asset", "institution": None, "period": "2026-01-01",
                                        "amount": "10.5", "source": "paste", "updated_at": "", "ignored": False, "archived": True}])
    assert rows == [["A", "asset", "", "2026-01-01", 10.5, "paste", "", False, True]]


def test_balances_wide_rows_fill_carried_values_and_total_like_the_dashboard():
    acct = lambda i, name, kind, ignored=False: SimpleNamespace(id=i, name=name, account_type=kind, ignored=ignored)  # noqa: E731
    accounts = [acct(1, "Brokerage", "asset"), acct(2, "Mortgage", "liability"), acct(3, "Toy", "asset", ignored=True)]
    periods = [date(2026, 1, 1), date(2026, 2, 1)]
    matrix = {1: ([100.0, 100.0], [False, True]), 2: ([-60.0, -55.0], [False, False]), 3: ([5.0, None], [False, False])}
    header, rows = exports.balances_wide_rows(periods, accounts, matrix)
    assert header == ["period", "Brokerage", "Mortgage", "Toy (ignored)", "assets", "liabilities", "net_worth", "carried"]
    assert rows[0] == ["2026-01-01", 100.0, -60.0, 5.0, 100.0, -60.0, 40.0, ""]
    assert rows[1] == ["2026-02-01", 100.0, -55.0, None, 100.0, -55.0, 45.0, "Brokerage"]
