"""
The update check: the strict reading of GitHub's reply, the version comparison, what the menu
is told, and the switch. No network: fetch_latest takes the function that opens the URL, and
the database is a dict standing in for the flow_settings rows.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")  # models import; never connects

import pytest  # noqa: E402

import settings_store  # noqa: E402
import update_check as uc  # noqa: E402

GOOD = {"tag_name": "v99.0.0", "html_url": "https://github.com/rwakeham/flow/releases/tag/v99.0.0",
        "published_at": "2026-11-02T04:00:00Z", "body": "<script>notes</script>", "name": "Flow 99.0.0"}


class FakeDb:
    """Just enough of a Session for update_check._save and load."""
    def __init__(self):
        self.rows = {}

    def get(self, model, key):
        return self.rows.get(key)

    def add(self, row):
        self.rows[row.key] = row

    def delete(self, row):
        self.rows.pop(row.key, None)

    def commit(self):
        pass


def reply(data) -> bytes:
    return json.dumps(data).encode()


def test_only_three_fields_are_read_and_each_must_be_exactly_right():
    assert uc.parse_release(GOOD) == uc.Release("99.0.0", GOOD["html_url"], "2026-11-02")
    for bad in [None, [], "x", {},
                {**GOOD, "tag_name": "99.0.0"}, {**GOOD, "tag_name": "v99.0"}, {**GOOD, "tag_name": "v99.0.0-rc1"},
                {**GOOD, "tag_name": 3}, {**GOOD, "html_url": "https://example.com/releases/tag/v99.0.0"},
                {**GOOD, "html_url": "https://github.com/other/repo/releases/tag/v99.0.0"},
                {**GOOD, "html_url": GOOD["html_url"] + '"><script>'}, {**GOOD, "html_url": None}]:
        assert uc.parse_release(bad) is None, bad
    assert uc.parse_release({**GOOD, "published_at": "soon"}).published == ""
    assert uc.parse_release({**GOOD, "published_at": None}).published == ""


def test_newer_means_a_higher_semver_and_nothing_else():
    assert uc.is_newer("3.2.0", "3.1.9") and uc.is_newer("4.0.0", "3.9.9") and uc.is_newer("3.1.10", "3.1.9")
    assert not uc.is_newer("3.1.0", "3.1.0") and not uc.is_newer("3.0.9", "3.1.0")
    assert not uc.is_newer("v3.2.0", "3.1.0") and not uc.is_newer("3.2.0", "unknown") and not uc.is_newer("", "3.1.0")


def test_fetch_reads_the_reply_and_reports_failures_in_a_few_words():
    assert uc.fetch_latest(lambda url, timeout: reply(GOOD)).version == "99.0.0"
    with pytest.raises(uc.CheckFailed, match="unexpected reply"):
        uc.fetch_latest(lambda url, timeout: b"<html>rate limited</html>")
    with pytest.raises(uc.CheckFailed, match="unexpected reply"):
        uc.fetch_latest(lambda url, timeout: reply({"tag_name": "nope"}))

    # An exception's own text never gets through: each kind of failure has fixed wording.
    def no_route(url, timeout):
        raise OSError("[Errno -2] Name or service not known")
    with pytest.raises(uc.CheckFailed, match="^couldn't reach GitHub$"):
        uc.fetch_latest(no_route)

    def slow(url, timeout):
        raise TimeoutError("timed out after 10.0s")
    with pytest.raises(uc.CheckFailed, match="^GitHub didn't answer in time$"):
        uc.fetch_latest(slow)

    def rate_limited(url, timeout):
        import urllib.error
        raise urllib.error.HTTPError(url, 403, "rate limit exceeded for 203.0.113.9", {}, None)
    with pytest.raises(uc.CheckFailed, match="^GitHub answered 403$"):
        uc.fetch_latest(rate_limited)


def test_a_check_stores_the_result_and_the_menu_shows_a_newer_release():
    settings_store.testing_state()
    uc.testing_state()
    db = FakeDb()
    assert uc.run_check(db, lambda url, timeout: reply(GOOD)) == (True, "Flow 99.0.0 is available.")
    s = uc.status()
    assert s["enabled"] and s["update"]["version"] == "99.0.0" and s["latest"]["url"] == GOOD["html_url"]
    assert s["checked_at"] and not s["error"]
    assert json.loads(db.rows[uc.STATE_KEY].value)["version"] == "99.0.0"

    # A failure keeps the release found last time, and says what went wrong.
    def down(url, timeout):
        raise OSError("connection reset by 203.0.113.9")
    ok, message = uc.run_check(db, down)
    assert (ok, message) == (False, "Couldn't check for updates: couldn't reach GitHub.")
    assert uc.status()["update"]["version"] == "99.0.0" and uc.status()["error"] == "couldn't reach GitHub"
    assert "203.0.113.9" not in json.dumps(uc.status())

    # The same version as the one running, or an older one, is no update.
    from version import APP_VERSION
    assert uc.run_check(db, lambda url, timeout: reply({**GOOD, "tag_name": "v" + APP_VERSION}))[1] == "You're on the latest release."
    assert uc.status()["update"] is None and uc.status()["latest"]["version"] == APP_VERSION
    assert uc.run_check(db, lambda url, timeout: reply({**GOOD, "tag_name": "v0.0.1"}))[1] == "You're ahead of the latest release (0.0.1)."
    assert uc.status()["update"] is None


def test_hiding_a_release_lasts_until_a_newer_one():
    settings_store.testing_state()
    uc.testing_state()
    db = FakeDb()
    uc.run_check(db, lambda url, timeout: reply(GOOD))
    assert not uc.dismiss(db, "latest") and uc.status()["update"]
    assert uc.dismiss(db, "99.0.0") and uc.status()["update"] is None and uc.status()["dismissed"] == "99.0.0"
    uc.run_check(db, lambda url, timeout: reply({**GOOD, "tag_name": "v99.0.1"}))
    assert uc.status()["update"]["version"] == "99.0.1"
    # Both survive a restart: load() reads them back.
    uc.testing_state()
    uc.load(db)
    assert uc.status()["latest"]["version"] == "99.0.1" and uc.status()["dismissed"] == "99.0.0"


def test_the_daily_switch_is_on_unless_turned_off(monkeypatch):
    monkeypatch.delenv("FLOW_UPDATE_CHECK", raising=False)
    settings_store.testing_state()
    assert settings_store.update_check_flag() and settings_store.setting_source(settings_store.UPDATE_CHECK) is None
    settings_store.testing_state({settings_store.UPDATE_CHECK: "0"})
    assert not settings_store.update_check_flag()
    settings_store.testing_state({settings_store.UPDATE_CHECK: "1"})
    assert settings_store.update_check_flag()
    monkeypatch.setenv("FLOW_UPDATE_CHECK", "off")
    assert not settings_store.update_check_flag() and settings_store.setting_source(settings_store.UPDATE_CHECK) == "env"
