"""The version file is plain semver, and a bad or missing one reads as "unknown", never a crash."""

from __future__ import annotations

import version


def test_the_version_file_is_semver():
    raw = version.VERSION_FILE.read_text(encoding="utf-8")
    assert raw.endswith("\n") and raw.count("\n") == 1, "one line, newline-terminated"
    assert version.SEMVER.match(raw.strip()), f"backend/VERSION must be MAJOR.MINOR.PATCH, not {raw.strip()!r}"
    assert version.APP_VERSION == raw.strip()


def test_bad_or_missing_files_read_as_unknown(tmp_path):
    assert version.read_version(tmp_path / "missing") == "unknown"
    for bad in ("v3.0.0", "3.0", "3.0.0-rc1", "03.0.0", ""):
        f = tmp_path / "VERSION"
        f.write_text(bad + "\n", encoding="utf-8")
        assert version.read_version(f) == "unknown", bad
    f.write_text(" 3.1.4 \n", encoding="utf-8")
    assert version.read_version(f) == "3.1.4"
