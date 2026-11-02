"""
Tests for balance-sheet entry: reading a pasted account summary, the scrubbed
outline the AI fallback sees, the preview checks, and carry-forward.

The sample page has the shape of a typical online-banking account summary (a
fictional "Example Bank"); its names, numbers and amounts are made up. Pure functions; no DB, no network.
"""

from __future__ import annotations

import json
import os
import types
from datetime import date

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")  # models import; never connects
os.environ.setdefault("SECRET_KEY", "test-" * 8)                                     # auth import (router tests)

import balance_sheet as bs  # noqa: E402
import card_ai  # noqa: E402
from routers import balances as balances_router  # noqa: E402
from routers.data import summarize  # noqa: E402

PAGE = """Account Summary - Example Bank
Skip to main content

*
*

Account Summary
Tile ViewOpens a dialog
List View(Selected View)Opens a dialog
Choose Account to DisplayPersonal AccountsSet as default

1. Begin region
   1.
   1. Alex Checking
Account number ending in...1111
Available balance$71.20
   2. Joint Savings
Account number ending in...2222
Available balance$12,345.67
End of region
2. Begin region
   1.
   1. Jordan BrokerageExample Wealth Partners, LLC1
Account number ending in...3333
Example Wealth Partners, LLC1
Total account value
(as of previous close)Opens a dialog$1,234,567.89
   2. Jordan Roth IRAExample Wealth Partners, LLC1
Account number ending in...4444
Example Wealth Partners, LLC1
Total account value
(as of previous close)Opens a dialog$9,876.54
   3. TRUST ACCOUNT
Account number ending in...5555
End of region
3. Begin region
   1.
   1. HOME EQUITY LINE OF CREDITExample Bank, N.A.
Account number ending in...6666
Example Bank, N.A.
Total outstanding balanceOpens a dialog$43,210.00
   2. EXAMPLE CASH BACK VISA® CARD
Account number ending in...7777
Outstanding balance$1,500.25
End of region
4. Begin region
   1.
   1. AUTO LOAN
Account number ending in...8888
Outstanding principal balance†FootnotePD IN FULL
      * Begin region
info
Your auto loan account is closed. You can [view your documents.](https://example.com/x?st=1793456654#)
   2. MORTGAGE
Account number ending in...9999
Outstanding principal balance$150,000.10
End of region
5. Begin region
   1.
   1. EXAMPLE BANK REWARDS
Account number ending in...1212
Available rewards balanceOpens a dialog$45.67
   2. EXAMPLE BANK REWARDS
Account number ending in...1313
Available rewards balanceOpens a dialog123,456
End of region
6. Begin region
   1.
   1. SAFE DEPOSIT BOX
Account number ending in...1414
End of region

Example Bank Offers
* 3% cash back3 days left
Activate
the Circle K dealOpens a dialog

Assets
Cash accounts
$12,416.87
Total assets
$1,256,861.30
Liabilities
Total liabilities†
$194,710.35
Contact Us
1-800-555-0100
"""


def by4(items):
    return {i.last4: i for i in items}


# ── Extraction ───────────────────────────────────────────────────────────────

def test_reads_every_account_on_the_sample_page():
    ex = bs.extract(PAGE)
    got = by4(ex.items)
    assert list(got) == ["1111", "2222", "3333", "4444", "5555", "6666", "7777", "8888", "9999", "1212", "1313", "1414"]
    assert got["1111"].amount == 71.20 and got["1111"].kind == "balance" and got["1111"].label == "Alex Checking"
    assert got["2222"].amount == 12345.67
    assert got["3333"].amount == 1234567.89 and got["3333"].label == "Jordan Brokerage"
    assert got["4444"].amount == 9876.54 and got["4444"].label == "Jordan Roth IRA"
    assert got["6666"].amount == 43210.00 and got["6666"].kind == "owed"
    assert got["6666"].label == "HOME EQUITY LINE OF CREDIT"
    assert got["7777"].kind == "owed" and got["7777"].amount == 1500.25
    assert got["9999"].kind == "owed" and got["9999"].amount == 150000.10
    assert got["8888"].kind == "closed" and got["8888"].amount is None
    assert got["1212"].kind == "rewards" and got["1212"].amount is None     # $ rewards are not a balance
    assert got["1313"].kind == "rewards" and got["1313"].amount is None     # points have no $
    assert got["5555"].kind == "none" and got["1414"].kind == "none"
    assert "previous close" in got["3333"].detail
    # The totals at the foot of the page are not mistaken for an unread layout.
    assert not ex.needs_ai


def test_survives_other_layouts():
    tile = "Everyday Checking ...4321 $1,000.00\nHigh Yield Savings x9876 Available $250.50\nVisa ****1111 Balance: -$20.00"
    got = by4(bs.extract(tile).items)
    assert got["4321"].amount == 1000.00 and got["4321"].label == "Everyday Checking"
    assert got["9876"].amount == 250.50
    assert got["1111"].amount == -20.00
    # Amounts before the account numbers: the local reader can't place them, so it asks for the AI.
    odd = "Checking\n$1,000.00\nAccount ending in 4321\nSavings\n$2,000.00\nAccount ending in 9876\n"
    ex = bs.extract(odd)
    assert not ex.needs_ai or all(i.amount is None for i in ex.items)
    swapped = "$1,000.00 Checking (...4321)\n$2,000.00 Savings (...9876)"
    ex = bs.extract(swapped)
    assert ex.needs_ai and ex.unread


def test_garbage_and_empty_input():
    assert bs.extract("").items == []
    assert bs.extract("hello world\n" * 50).items == []
    ex = bs.extract("Total $5.00")
    assert ex.needs_ai and not ex.items


def test_money_parsing():
    assert bs.parse_money("$1,234.56") == 1234.56
    assert bs.parse_money("-$20.00") == -20.0
    assert bs.parse_money("($20.00)") == -20.0
    assert bs.parse_money("$") is None


# ── The AI outline: nothing identifying leaves Flow ───────────────────────────

SECRETS = ["Alex", "Jordan", "1111", "2222", "3333", "4444", "5555", "6666", "7777", "8888", "9999", "1212",
           "71.20", "12,345", "1,234,567", "9,876", "43,210", "1,500", "150,000", "45.67", "123,456",
           "Example", "VISA", "Circle", "example.com", "1793456654", "555-0100", "Wealth", "Partners", "LLC"]


def test_outline_hides_amounts_numbers_and_names():
    ol = bs.outline(PAGE)
    low = ol.text.lower()
    for s in SECRETS:
        assert s.lower() not in low, s
    assert "<N1>" in ol.text and "<A1>" in ol.text
    assert ol.numbers["<N1>"] == "1111" and ol.amounts["<A1>"] == 71.20
    assert ol.labels["<N1>"] == "Alex Checking"           # kept in Flow for display
    assert "available balance" in low and "outstanding principal balance" in low


def test_ai_request_carries_only_the_outline():
    """The whole request body as sent (system, tools, messages) holds nothing identifying."""
    sent = {}

    class Fake:
        class beta:
            class messages:
                @staticmethod
                def parse(**kw):
                    sent.update(kw)
                    return types.SimpleNamespace(stop_reason="end_turn", usage=None, model=kw["model"],
                                                 parsed_output=balances_router.Pairs(pairs=[]))

    ol = bs.outline(PAGE)
    row = types.SimpleNamespace(request_json=None, response_json=None)
    card_ai._call(Fake, balances_router.SYSTEM, {"outline": ol.text}, balances_router.Pairs,
                  effort="low", max_tokens=4000, log_row=row)
    body = json.dumps(card_ai._jsonable(sent)).lower()
    assert row.request_json and row.response_json
    for s in SECRETS:
        assert s.lower() not in body, s


def test_ai_pairs_map_back_and_local_readings_stand():
    ol = bs.outline("Checking (...4321)\nSavings (...9876)\nTotals\n$1,000.00\n$2,000.00\n$3,000.00")
    n = {v: k for k, v in ol.numbers.items()}
    a = {v: k for k, v in ol.amounts.items()}
    pairs = [{"account": n["4321"], "amount": a[1000.0], "kind": "balance"},
             {"account": n["9876"], "amount": a[1000.0], "kind": "balance"},     # amount reused: dropped
             {"account": "<N99>", "amount": a[2000.0], "kind": "balance"},       # invented token: ignored
             {"account": n["9876"], "amount": a[2000.0], "kind": "owed"}]         # repeat account: ignored
    items = by4(bs.items_from_ai(pairs, ol))
    assert items["4321"].amount == 1000.0 and items["4321"].source == "ai"
    assert items["9876"].amount is None and items["9876"].kind == "none"
    local = [bs.Item("4321", "Checking", 5.0, "balance"), bs.Item("9876", "Savings", None, "none")]
    merged = by4(bs.merge_items(local, [bs.Item("4321", "", 1000.0, "balance", source="ai"),
                                        bs.Item("9876", "", 2000.0, "balance", source="ai")]))
    assert merged["4321"].amount == 5.0 and merged["9876"].amount == 2000.0


# ── Preview checks ───────────────────────────────────────────────────────────

P = date(2026, 11, 1)
TODAY = date(2026, 10, 31)


def accounts():
    return [
        bs.AccountInfo(1, "Jordan Brokerage", "asset", ["3333"], previous=(date(2026, 10, 1), 1_100_000.0)),
        bs.AccountInfo(2, "Jordan Roth", "asset", ["4444"], previous=(date(2026, 10, 1), 9_800.0), existing=9_876.54),
        bs.AccountInfo(3, "Home Equity Line", "liability", ["6666"], previous=(date(2026, 10, 1), -43_000.0)),
        bs.AccountInfo(4, "Mortgage", "liability", ["9999"], previous=(date(2026, 10, 1), -151_000.0), existing=-1.0),
        bs.AccountInfo(5, "Employer 401k", "asset", [], previous=(date(2026, 10, 1), 160_000.0)),
        bs.AccountInfo(6, "Old Loan", "liability", ["8888"], previous=(date(2026, 10, 1), -5_000.0)),
        bs.AccountInfo(7, "Brokerage", "asset", ["2020"], previous=(date(2026, 10, 1), 1.0)),
    ]


def entries():
    return [bs.Entry(amount=i.amount, last4=i.last4, label=i.label, kind=i.kind) for i in bs.extract(PAGE).items]


def rows_by4(out):
    return {r["last4"]: r for r in out["rows"]}


def test_plan_matches_by_last4_and_flips_liabilities():
    out = bs.plan(entries(), accounts(), P, TODAY, date(2026, 10, 1), bs.Thresholds())
    r = rows_by4(out)
    assert out["period_errors"] == [] and out["period_warnings"] == []
    assert r["3333"]["account_id"] == 1 and r["3333"]["value"] == 1234567.89
    assert r["6666"]["value"] == -43210.00 and r["9999"]["value"] == -150000.10
    assert r["3333"]["checked"] and r["6666"]["checked"]
    # Large change vs last period: flagged but still ticked.
    assert any("Large change" in w for w in r["3333"]["warnings"])
    assert not any("Large change" in w for w in r["6666"]["warnings"])
    # Already stored with the same value: nothing to do.
    assert r["4444"]["skip"] and not r["4444"]["checked"]
    # Replacing a different stored value is a warning.
    assert any("Replaces" in w for w in r["9999"]["warnings"])
    # Unknown accounts, rewards, closed and empty ones are skipped with a reason.
    assert r["1111"]["skip"] and r["1111"]["account_id"] is None and not r["1111"]["checked"]
    assert "Rewards" in r["1212"]["skip"] and "Rewards" in r["1313"]["skip"]
    assert "closed" in r["8888"]["skip"] and r["8888"]["account_id"] == 6
    assert r["5555"]["skip"]
    # A tracked account with a saved number that isn't in the paste is listed.
    assert [m["name"] for m in out["not_in_paste"]] == ["Brokerage"]


def test_plan_period_checks():
    out = bs.plan(entries(), accounts(), date(2026, 12, 1), TODAY, None, bs.Thresholds())
    assert out["period_errors"] and not any(r["checked"] for r in out["rows"])
    out = bs.plan(entries(), accounts(), date(2026, 11, 15), TODAY, None, bs.Thresholds())
    assert out["period_errors"]
    out = bs.plan(entries(), accounts(), date(2026, 9, 1), TODAY, date(2026, 11, 1), bs.Thresholds())
    assert out["period_warnings"] and not out["period_errors"]
    assert bs.next_period(date(2026, 10, 31)) == date(2026, 11, 1)
    assert bs.next_period(date(2026, 12, 5)) == date(2027, 1, 1)
    # The suggested period is the month the balances are entered in; next month stays allowed.
    assert bs.current_period(date(2026, 11, 1)) == date(2026, 11, 1)
    assert bs.current_period(date(2026, 11, 20)) == date(2026, 11, 1)
    assert bs.period_problem(bs.current_period(date(2026, 11, 1)), date(2026, 11, 1)) is None
    assert bs.period_problem(bs.next_period(date(2026, 11, 1)), date(2026, 11, 1)) is None


def test_the_users_date_decides_the_period():
    """The server runs on UTC: on Oct 31 evening in the US it is already Nov 1 there."""
    server = date(2026, 11, 1)
    mine = bs.client_today("2026-10-31", server)
    assert mine == date(2026, 10, 31) and bs.current_period(mine) == date(2026, 10, 1)   # October, not November
    assert bs.period_problem(date(2026, 12, 1), mine)                                   # December is still ahead
    assert bs.client_today("2026-11-02", server) == date(2026, 11, 2)                   # east of UTC
    for bad in (None, "", "yesterday", "2026-13-01", "2027-01-01", "2026-10-30"):       # missing, malformed, too far
        assert bs.client_today(bad, server) == server, bad
    assert isinstance(balances_router.user_today(None), date)                          # the route dependency


def test_plan_links_and_conflicts():
    accts = accounts()
    # First-time link: the user picks an account for an unknown number.
    out = bs.plan([bs.Entry(amount=71.2, last4="1111", account_id=5)], accts, P, TODAY, None, bs.Thresholds())
    row = out["rows"][0]
    assert row["link"] and row["account_id"] == 5 and row["value"] == 71.2
    # A number saved on another account can't be linked elsewhere.
    out = bs.plan([bs.Entry(amount=1.0, last4="3333", account_id=5)], accts, P, TODAY, None, bs.Thresholds())
    assert out["rows"][0]["errors"]
    # The same account twice: both rows blocked.
    out = bs.plan([bs.Entry(amount=1.0, last4="3333"), bs.Entry(amount=2.0, account_id=1)],
                  accts, P, TODAY, None, bs.Thresholds())
    assert all(r["errors"] for r in out["rows"])
    # The same number saved on two accounts must be picked by hand.
    accts[6].last4 = ["3333"]
    out = bs.plan([bs.Entry(amount=1.0, last4="3333")], accts, P, TODAY, None, bs.Thresholds())
    assert out["rows"][0]["errors"] and out["rows"][0]["account_id"] is None


def test_thresholds_global_and_per_account():
    a = bs.AccountInfo(1, "X", "asset", previous=(date(2026, 10, 1), 100_000.0))
    th = bs.Thresholds(pct=10, abs=50_000)
    assert bs.change_alert(a, 105_000.0, th) is None
    assert "%" in bs.change_alert(a, 120_000.0, th)
    a.alert_pct = 25
    assert bs.change_alert(a, 120_000.0, th) is None
    big = bs.AccountInfo(2, "Y", "asset", previous=(date(2026, 10, 1), 3_000_000.0))
    assert "$" in bs.change_alert(big, 3_060_000.0, th)          # 2%, but over $50k
    big.alert_abs = 0                                              # 0 turns that check off
    assert bs.change_alert(big, 3_060_000.0, th) is None
    assert bs.change_alert(bs.AccountInfo(3, "Z", "asset"), 1.0, th) is None   # nothing to compare with


def test_typed_entries_for_liabilities():
    accts = accounts()
    out = bs.plan([bs.Entry(amount=150_500.0, account_id=4, kind="owed")], accts, P, TODAY, None, bs.Thresholds())
    assert out["rows"][0]["value"] == -150_500.0
    assert bs.signed_value("liability", -20.0) == 20.0             # a credit balance on a card
    assert bs.signed_value("asset", -20.0) == -20.0


# ── Carry-forward and totals ─────────────────────────────────────────────────

def test_carry_forward():
    assert bs.carry_forward([None, 1.0, None, 3.0, None]) == ([None, 1.0, 1.0, 3.0, 3.0], [False, False, True, False, True])
    assert bs.carry_forward([1.0, None, None], archived=True) == ([1.0, None, None], [False, False, False])


def test_summary_uses_carried_values():
    periods = [date(2026, 10, 1), date(2026, 11, 1)]
    A = types.SimpleNamespace
    accts = [A(id=1, name="Brokerage", account_type="asset", ignored=False),
             A(id=2, name="401k", account_type="asset", ignored=False),
             A(id=3, name="Mortgage", account_type="liability", ignored=False),
             A(id=4, name="Hidden", account_type="asset", ignored=True)]
    m = {1: ([100.0, 110.0], [False, False]), 2: ([50.0, 50.0], [False, True]),
         3: ([-30.0, -29.0], [False, False]), 4: ([999.0, 999.0], [False, True])}
    s = summarize(periods, accts, m)
    assert s["total_assets"] == 160.0 and s["total_liabilities"] == -29.0 and s["net_worth"] == 131.0
    assert s["net_worth_mom_delta"] == 11.0
    assert s["carried_accounts"] == ["401k"]
    assert s["largest_asset"]["name"] == "Brokerage" and s["largest_asset"]["mom_delta"] == 10.0
    assert summarize([], [], {})["as_of"] is None


def test_numbers():
    assert bs.numbers("1234, x5678,1234,12") == ["1234", "5678"]
    assert bs.join_numbers(["1234", "1234"]) == "1234"
    assert bs.join_numbers([]) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
