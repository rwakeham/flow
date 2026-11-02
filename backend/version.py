"""
Flow's version: the one line in ``backend/VERSION`` (semver, ``MAJOR.MINOR.PATCH``).

It is the only place the version is written, and the release run writes it: releases are
made by hand, and the run bumps it from the commits since the last release before
publishing ``v<VERSION>`` (see CLAUDE.md, Releases). The Docker image carries the file (``COPY . .``), so the
running app reports the version it was built from.
"""

from __future__ import annotations

import re
from pathlib import Path

VERSION_FILE = Path(__file__).with_name("VERSION")
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def read_version(path: Path = VERSION_FILE) -> str:
    """The version string, or "unknown" if the file is missing or not ``MAJOR.MINOR.PATCH``."""
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return value if SEMVER.match(value) else "unknown"


APP_VERSION = read_version()
