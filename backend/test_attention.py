"""
The Dashboard's "Needs attention" panel (attention.build, pure): fixed order, the counts, the empty
state, and that every item points somewhere the page knows how to open.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import attention

TODAY = date(2026, 11, 2)


def full_input(**over):
    base = dict(
        today=TODAY,
        balance_accounts=[{"id": 1, "name": "Brokerage", "ignored": False, "archived": False, "behind": True},
                          {"id": 2, "name": "Old loan", "ignored": False, "archived": True, "behind": True},
                          {"id": 3, "name": "Ignored", "ignored": True, "archived": False, "behind": True},
                          {"id": 4, "name": "House", "ignored": False, "archived": False, "behind": False}],
        cash_accounts=[{"id": 7, "name": "Checking", "behind": False, "unmatched": 3, "pending": 1},
                       {"id": 8, "name": "Savings", "behind": True, "unmatched": 0, "pending": 0}],
        unsorted_payees=2, review_open=5, subs_overdue=1, subs_price_change=2, subs_duplicate=1,
        ai_problem={"kind": "auth", "message": "The key was refused."},
        update={"version": "9.9.9", "url": "https://github.com/rwakeham/flow/releases/tag/v9.9.9"},
        formats_unpreviewed=1, last_backup_at="2026-10-01T03:00:00+00:00", backup_daily=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def known_nav(nav: dict) -> bool:
    if "action" in nav:
        if nav["action"] not in attention.NAV_ACTIONS:
            return False
        return nav["action"] != "settings" or nav.get("section") in attention.SETTINGS_SECTIONS
    keys = attention.NAV_SECTIONS.get(nav.get("section"))
    return keys is not None and set(nav) - {"section"} <= keys


def test_items_come_in_the_fixed_order_with_their_counts():
    items = attention.build(full_input())
    assert [i["kind"] for i in items] == [
        "ai_paused", "unmatched", "pending", "balances_behind", "cash_behind", "payees", "review",
        "subs_overdue", "subs_price", "subs_dup", "formats", "backup_off", "backup_old", "update"]
    by = {i["kind"]: i for i in items}
    assert by["unmatched"]["count"] == 3 and by["unmatched"]["nav"] == {"section": "cashflow", "account": 7, "reconcile": True}
    assert by["balances_behind"]["count"] == 1 and "November" in by["balances_behind"]["text"]   # ignored and archived left out
    assert by["cash_behind"]["nav"] == {"section": "cashflow", "account": 8}
    assert by["backup_old"]["text"] == "No backup in 32 days"
    assert by["update"]["text"] == "Flow 9.9.9 is available"
    assert "The key was refused." in by["ai_paused"]["text"]


def test_every_item_points_somewhere_the_page_can_open():
    for item in attention.build(full_input()):
        assert known_nav(item["nav"]), item


def test_nothing_to_do_is_an_empty_list():
    quiet = full_input(balance_accounts=[], cash_accounts=[], unsorted_payees=0, review_open=0, subs_overdue=0,
                       subs_price_change=0, subs_duplicate=0, ai_problem=None, update=None, formats_unpreviewed=0,
                       last_backup_at="2026-11-01T03:00:00+00:00", backup_daily=True)
    assert attention.build(quiet) == []
    assert attention.build({"today": TODAY, "last_backup_at": "2026-11-02T03:00:00+00:00"}) == []   # a dict works too


def test_backup_signals():
    never = attention.build({"today": TODAY, "last_backup_at": None})
    assert [i["kind"] for i in never] == ["backup_old"] and never[0]["text"] == "No backup has been taken yet"
    fresh = attention.build({"today": TODAY, "last_backup_at": "2026-10-27T03:00:00+00:00"})   # 6 days: fine
    assert fresh == []
    off = attention.build({"today": TODAY, "last_backup_at": "2026-11-02T03:00:00+00:00", "backup_daily": False})
    assert [i["kind"] for i in off] == ["backup_off"]


def test_names_are_returned_verbatim_for_the_page_to_escape():
    items = attention.build({"today": TODAY, "last_backup_at": "2026-11-02T03:00:00+00:00",
                             "cash_accounts": [{"id": 1, "name": "<b>Checking</b>", "unmatched": 2, "pending": 0, "behind": False}]})
    assert items[0]["text"] == "2 bank rows to reconcile in <b>Checking</b>"
