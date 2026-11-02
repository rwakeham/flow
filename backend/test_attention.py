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


# ── Snoozing ──────────────────────────────────────────────────────────────────

def test_every_item_has_a_stable_unique_key():
    items = attention.build(full_input())
    keys = [i["key"] for i in items]
    assert len(keys) == len(set(keys))
    assert "unmatched:7" in keys and "cash_behind:8" in keys and "update:9.9.9" in keys and "review" in keys
    assert all(attention.KEY_RE.match(k) for k in keys)


def test_a_snooze_hides_an_item_until_its_date_or_until_its_count_grows():
    items = attention.build(full_input())
    snoozes = {"review": {"until": "2026-11-09", "count": 5}, "payees": {"until": None, "count": 2},
               "unmatched:7": {"until": "2026-11-02", "count": 3}}             # this one has expired
    shown, hidden = attention.split_snoozed(items, snoozes, TODAY)
    assert [h["kind"] for h in hidden] == ["payees", "review"]
    assert hidden[1]["snoozed_until"] == "2026-11-09" and hidden[0]["snoozed_until"] is None
    assert any(i["key"] == "unmatched:7" for i in shown)
    grown = attention.build(full_input(review_open=6, unsorted_payees=3))
    shown, hidden = attention.split_snoozed(grown, snoozes, TODAY)
    assert hidden == []                                                     # something new: both are back
    assert attention.prune_snoozes(snoozes, TODAY) == {"review": snoozes["review"], "payees": snoozes["payees"]}


def test_stored_snoozes_are_read_as_outside_data():
    raw = {"review": {"until": "2026-11-09", "count": 5}, "payees": {"until": None, "count": "x"},
           "<b>": {"until": None, "count": 1}, "update:9.9.9": {"until": "not a date", "count": 1}, "subs_dup": "nope"}
    assert attention.clean_snoozes(raw) == {"review": {"until": "2026-11-09", "count": 5}, "payees": {"until": None, "count": 0}}
    assert attention.clean_snoozes("junk") == {}
