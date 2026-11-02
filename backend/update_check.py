"""
Update check: once a day Flow asks GitHub for its newest release and, when it is newer than the
version running, says so under Sign out ▾ (a dot on the button; the version line in the menu
links to the release). The check never installs anything: updating stays ``git pull && ./deploy.sh``.

What leaves the machine: one GET to api.github.com for this repository's latest release, with
nothing about the user or their data; GitHub sees the server's address and a plain User-Agent,
as for any visit to the site. The check is on by default and switched off under Sign out ▾
(``flow_settings`` ``update_check``) or with ``FLOW_UPDATE_CHECK=0`` in .env, which wins while
it is there. "Check for updates" in that menu runs it by hand, switch or no switch.

The reply is someone else's data: only ``tag_name`` (must be ``vMAJOR.MINOR.PATCH``),
``html_url`` (must be this repository's releases page, plain characters) and ``published_at``
(its date) are read, at most ``MAX_REPLY`` bytes of it, and nothing from it is rendered as HTML.
A failure (no outbound access, GitHub down or rate limited) is logged and shown only to a check
run by hand; the daily one tries again tomorrow.

The last result and the version the user hid are ``flow_settings`` rows (``STATE_KEY``,
``DISMISSED_KEY``), kept in memory between checks (one worker). The parsing, comparison and
status are pure; ``run_check`` and ``loop`` take the database.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import settings_store
from version import APP_VERSION, SEMVER

log = logging.getLogger("flow.update")

REPO = "rwakeham/flow"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases/"
USER_AGENT = "Flow-update-check"
TIMEOUT = 10.0                    # seconds, the one request
MAX_REPLY = 256 * 1024            # bytes read of the reply (the notes are in it; 256 KB is plenty)
FIRST_DELAY = 120                 # seconds after startup: the deploy's health check comes first
INTERVAL = 24 * 60 * 60           # between checks

STATE_KEY = "update_check_state"  # flow_settings: JSON of the last check (see run_check)
DISMISSED_KEY = "update_dismissed"  # flow_settings: the release version the user hid

_URL_RE = re.compile(r"^[A-Za-z0-9:./_-]{1,200}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


class CheckFailed(Exception):
    """The check could not be made or the reply made no sense; the message is short and plain."""


@dataclass(frozen=True)
class Release:
    version: str      # "3.2.0"
    url: str          # the release page on GitHub
    published: str    # "YYYY-MM-DD", or "" when the reply had no date


# ── Pure parts ───────────────────────────────────────────────────────────────

def parse_version(text: str) -> tuple[int, int, int] | None:
    m = SEMVER.match((text or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def parse_release(data: object) -> Release | None:
    """The release in GitHub's reply, or None unless every field read is exactly what is expected."""
    if not isinstance(data, dict):
        return None
    tag, url, published = data.get("tag_name"), data.get("html_url"), data.get("published_at")
    if not isinstance(tag, str) or not tag.startswith("v") or parse_version(tag[1:]) is None:
        return None
    if not isinstance(url, str) or not url.startswith(RELEASES_URL) or not _URL_RE.match(url):
        return None
    date = published[:10] if isinstance(published, str) and _DATE_RE.match(published) else ""
    return Release(tag[1:].strip(), url, date)


def is_newer(candidate: str, current: str = APP_VERSION) -> bool:
    """True when ``candidate`` is a higher version than ``current``; False for anything unreadable."""
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and b is not None and a > b


def _open(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 - a fixed https URL
        return resp.read(MAX_REPLY)


def fetch_latest(open_url=None) -> Release:
    """GitHub's latest release for this repository; raises CheckFailed with a short reason."""
    open_url = open_url or _open
    try:
        raw = open_url(API_URL, TIMEOUT)
    except urllib.error.HTTPError as exc:
        raise CheckFailed(f"GitHub answered {exc.code}") from None
    except (OSError, ValueError) as exc:           # URLError and timeouts are OSErrors
        raise CheckFailed(str(exc) or exc.__class__.__name__) from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise CheckFailed("unexpected reply") from None
    release = parse_release(data)
    if release is None:
        raise CheckFailed("unexpected reply")
    return release


# ── State (one worker: the in-memory copy is the only one) ───────────────────

_state: dict = {}          # checked_at, version, url, published, error
_dismissed: str = ""
_lock = threading.Lock()   # the daily check and a check by hand never run together


def testing_state(state: dict | None = None, dismissed: str = "") -> None:
    """Tests: set the in-memory copy without a database."""
    global _state, _dismissed
    _state, _dismissed = dict(state or {}), dismissed


def load(db) -> None:
    """Read the last result and the hidden version (startup, and after a backup restore)."""
    from models import AppSetting
    global _state, _dismissed
    row = db.get(AppSetting, STATE_KEY)
    try:
        _state = json.loads(row.value) if row and row.value else {}
        if not isinstance(_state, dict):
            _state = {}
    except ValueError:
        _state = {}
    row = db.get(AppSetting, DISMISSED_KEY)
    _dismissed = row.value if row and row.value and parse_version(row.value) else ""


def _save(db, key: str, value: str) -> None:
    from models import AppSetting
    row = db.get(AppSetting, key)
    if value:
        row = row or AppSetting(key=key)
        row.value = value
        db.add(row)
    elif row is not None:
        db.delete(row)
    db.commit()


def latest() -> Release | None:
    """The newest release the last successful check found, if any."""
    if not _state.get("version"):
        return None
    return Release(_state["version"], _state.get("url", ""), _state.get("published", ""))


def status() -> dict:
    """What the Sign out menu shows: the switch, the last check, and ``update`` when a newer release
    is known and not hidden."""
    found = latest()
    show = found if found and is_newer(found.version) and _dismissed != found.version else None
    return {
        "enabled": settings_store.update_check_flag(),
        "source": settings_store.setting_source(settings_store.UPDATE_CHECK),
        "checked_at": _state.get("checked_at", ""),
        "error": _state.get("error", ""),
        "latest": found.__dict__ if found else None,
        "update": show.__dict__ if show else None,
        "dismissed": _dismissed,
    }


def run_check(db, open_url=None) -> tuple[bool, str]:
    """One check now. Returns (ok, message) for the menu; the result is stored either way, and a
    failure keeps the release found last time."""
    global _state
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _lock:
        try:
            release = fetch_latest(open_url)
        except CheckFailed as exc:
            _state = {**_state, "checked_at": now, "error": str(exc)}
            _save(db, STATE_KEY, json.dumps(_state))
            log.info("update check: %s", exc)
            return False, f"Couldn't check for updates: {exc}."
        _state = {"checked_at": now, "version": release.version, "url": release.url,
                  "published": release.published, "error": ""}
        _save(db, STATE_KEY, json.dumps(_state))
    if is_newer(release.version):
        log.info("Flow %s is available (running %s): %s", release.version, APP_VERSION, release.url)
        return True, f"Flow {release.version} is available."
    if is_newer(APP_VERSION, release.version):
        return True, f"You're ahead of the latest release ({release.version})."
    return True, "You're on the latest release."


def dismiss(db, version: str) -> bool:
    """Hide this release's notice until a newer one; False for a version that isn't one."""
    global _dismissed
    if parse_version(version) is None:
        return False
    _dismissed = version.strip()
    _save(db, DISMISSED_KEY, _dismissed)
    return True


# ── The daily task ───────────────────────────────────────────────────────────

def _check_in_session(session_factory) -> None:
    with session_factory() as db:
        run_check(db)


async def loop(session_factory, first_delay: float = FIRST_DELAY, interval: float = INTERVAL) -> None:
    """Background task started by main.py's lifespan: a check shortly after startup, then daily,
    skipped while the switch is off. It never raises, so it outlives any failure."""
    await asyncio.sleep(first_delay)
    while True:
        if settings_store.update_check_flag():
            try:
                await asyncio.to_thread(_check_in_session, session_factory)
            except Exception:    # noqa: BLE001 - the task must keep going
                log.exception("update check")
        await asyncio.sleep(interval)
