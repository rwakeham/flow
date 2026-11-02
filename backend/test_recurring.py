"""
Tests for recurring-transaction detection, focused on sub-stream splitting.

Runnable directly (``python3 test_recurring.py`` from the backend/ dir) or via
pytest. Dates are generated relative to ``date.today()`` so the cases stay
recent regardless of when the suite runs.
"""

from __future__ import annotations

import calendar
import os
from dataclasses import dataclass
from datetime import date

from recurring import detect_recurring_patterns


@dataclass
class Txn:
    date: date
    description: str
    amount: float
    source: str = "manual"


def _nth_of_month_back(today: date, months_back: int, day: int) -> date:
    y, m = today.year, today.month - months_back
    while m <= 0:
        m += 12
        y -= 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(day, last))


def _last_of_month_back(today: date, months_back: int) -> date:
    y, m = today.year, today.month - months_back
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, calendar.monthrange(y, m)[1])


def _acme_two_streams(today: date) -> list[Txn]:
    """Same payee, same sign: $4000 on the 15th and $6000 on the last day."""
    txns: list[Txn] = []
    for k in range(1, 8):
        txns.append(Txn(_nth_of_month_back(today, k, 15), "Acme Payroll Deposit", 4000.0))
        txns.append(Txn(_last_of_month_back(today, k), "Acme Payroll Deposit", 6000.0))
    return txns


def test_acme_two_streams_split_into_two_monthly_patterns():
    today = date.today()
    patterns = detect_recurring_patterns(
        _acme_two_streams(today), days_ahead=40, split_substreams=True
    )
    assert len(patterns) == 2, f"expected 2 patterns, got {len(patterns)}: {patterns}"
    assert all(p.frequency == "monthly" for p in patterns), patterns
    amounts = sorted(p.amount for p in patterns)
    assert amounts == [4000.0, 6000.0], amounts


def test_disabled_via_argument_reverts_to_single_pattern():
    today = date.today()
    patterns = detect_recurring_patterns(
        _acme_two_streams(today), days_ahead=40, split_substreams=False
    )
    assert len(patterns) == 1, f"expected merged single pattern, got {patterns}"


def test_disabled_via_env_var_reverts_to_single_pattern():
    today = date.today()
    prev = os.environ.get("FLOW_RECURRING_SUBSTREAMS")
    os.environ["FLOW_RECURRING_SUBSTREAMS"] = "0"
    try:
        patterns = detect_recurring_patterns(_acme_two_streams(today), days_ahead=40)
    finally:
        if prev is None:
            del os.environ["FLOW_RECURRING_SUBSTREAMS"]
        else:
            os.environ["FLOW_RECURRING_SUBSTREAMS"] = prev
    assert len(patterns) == 1, f"expected merged single pattern, got {patterns}"


def test_single_monthly_stream_is_not_over_split():
    today = date.today()
    txns = [Txn(_nth_of_month_back(today, k, 20), "Netflix", -15.99) for k in range(1, 8)]
    patterns = detect_recurring_patterns(txns, days_ahead=40, split_substreams=True)
    assert len(patterns) == 1, f"expected 1 pattern, got {patterns}"
    assert patterns[0].frequency == "monthly", patterns


def test_weekly_stream_is_not_split_by_day_of_month():
    today = date.today()
    # 12 weekly payments ending a few days ago.
    from datetime import timedelta
    txns = [Txn(today - timedelta(days=7 * k + 3), "Gym Membership", -12.0) for k in range(12)]
    patterns = detect_recurring_patterns(txns, days_ahead=40, split_substreams=True)
    assert len(patterns) == 1, f"expected 1 weekly pattern, got {patterns}"
    assert patterns[0].frequency == "weekly", patterns


def test_price_change_same_day_stays_one_stream():
    today = date.today()
    # Rent on the 1st: rises from $1800 to $2000 partway through — one stream.
    txns = []
    for k in range(1, 8):
        amt = 1800.0 if k > 3 else 2000.0
        txns.append(Txn(_nth_of_month_back(today, k, 1), "Landlord Rent", -amt))
    patterns = detect_recurring_patterns(txns, days_ahead=40, split_substreams=True)
    assert len(patterns) == 1, f"expected 1 pattern despite price change, got {patterns}"
    assert patterns[0].frequency == "monthly", patterns


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {fn.__name__}: {e}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    raise SystemExit(1 if failures else 0)
