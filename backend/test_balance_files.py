"""
Balance files: a holdings or accounts export read with a balance import format, its
rows matched to balance-sheet accounts for the usual preview. Pure; synthetic files.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-" * 8)

import balance_sheet as bs  # noqa: E402
import import_infer as inf  # noqa: E402
import import_profile as ip  # noqa: E402
import import_profile_resolve as resolve  # noqa: E402

HOLDINGS = (b"Account,Symbol,Description,Quantity,Price,Market Value,As Of\n"
            b"Brokerage X1111,VTI,Total Stock,10,250.00,2500.00,10/31/2026\n"
            b"Brokerage X1111,BND,Bond,5,70.00,350.00,10/31/2026\n"
            b"Roth IRA,VXUS,Intl,3,60.00,180.00,10/31/2026\n"
            b"College fund,CASH,Cash,1,95.50,95.50,10/31/2026\n")


def accounts():
    return [bs.AccountInfo(id=1, name="Brokerage", account_type="asset", last4=["1111"]),
            bs.AccountInfo(id=2, name="Roth IRA", account_type="asset"),
            bs.AccountInfo(id=3, name="Kids 529", account_type="asset"),
            bs.AccountInfo(id=4, name="College fund", account_type="asset", archived=True)]


def read(content=HOLDINGS):
    d = inf.infer(content, "h.csv", "balance")
    return d, ip.parse(content, "h.csv", d.spec)


def test_holdings_draft_and_totals_per_account():
    d, parsed = read()
    assert d.kind == "balance" and d.spec.aggregate == "sum_by_account"
    assert "as_of" in d.spec.columns
    items = ip.to_balance_items(parsed, d.spec)
    assert [(i.label, i.last4, i.amount) for i in items] == [
        ("Brokerage X1111", "1111", 2850.0), ("Roth IRA", "", 180.0), ("College fund", "", 95.5)]
    assert max(r.as_of for r in parsed.rows).isoformat() == "2026-10-31"
    assert bs.next_period(max(r.as_of for r in parsed.rows)).isoformat() == "2026-11-01"


def test_rows_are_matched_by_number_then_marker_then_name():
    d, parsed = read()
    markers = [bs.Marker(account_id=3, label_text="College fund")]       # archived account 4 has the name
    items = bs.assign_accounts(ip.to_balance_items(parsed, d.spec), accounts(), markers)
    got = {i.label: i.account_id for i in items}
    assert got["Brokerage X1111"] is None          # its number is on account 1: the preview matches it
    assert got["Roth IRA"] == 2                    # exact name
    assert got["College fund"] == 3                # a marker's label wins over an archived account's name


def test_archived_accounts_are_not_matched_by_name():
    d, parsed = read()
    items = bs.assign_accounts(ip.to_balance_items(parsed, d.spec), accounts(), [])
    assert {i.label: i.account_id for i in items}["College fund"] is None


def test_matched_rows_preview_like_a_paste():
    d, parsed = read()
    items = bs.assign_accounts(ip.to_balance_items(parsed, d.spec), accounts(), [])
    entries = [bs.Entry(amount=i.amount, last4=i.last4, label=i.label, kind=i.kind, account_id=i.account_id,
                        source="file") for i in items]
    from datetime import date
    out = bs.plan(entries, accounts(), date(2026, 11, 1), date(2026, 11, 2), None, bs.Thresholds())
    by_label = {r["label"]: r for r in out["rows"]}
    assert by_label["Brokerage X1111"]["account_id"] == 1 and by_label["Brokerage X1111"]["value"] == 2850.0
    assert by_label["Roth IRA"]["account_id"] == 2


def test_a_saved_balance_format_reads_the_next_export_and_flags_a_changed_one():
    d, _ = read()
    c = resolve.Candidate(id=9, name="Broker holdings", kind="balance", spec=d.spec)
    later = HOLDINGS.replace(b"2500.00", b"2600.00")
    assert resolve.choose(later, "h.csv", [c], "balance").status == "match"
    changed = HOLDINGS.replace(b"Market Value", b"Current Value")
    r = resolve.choose(changed, "h.csv", [c], "balance")
    assert r.status == "drift" and r.diff["added"] == ["Current Value"]
    assert r.draft["columns"]["value"][0]["header"] == "Current Value"     # re-pointed from the new draft
