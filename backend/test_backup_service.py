"""
Backup hardening: backup ids that aren't Flow's own file names are refused before
the disk is touched, and zips are size-checked from their directory before
anything is decompressed. In-memory zips and a temporary backup directory.
"""

from __future__ import annotations

import io
import json
import os
import zipfile

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-" * 8)

import pytest  # noqa: E402

import backup_service as bs  # noqa: E402

GOOD_ID = "flow_backup_20261031_120000_0a1b2c3d"


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def backup_files(**extra) -> dict[str, bytes]:
    files = {name: b"[]" for name in bs._REQUIRED_FILES}
    files["backup_info.json"] = json.dumps({"created_at": "2026-10-31T12:00:00+00:00", "trigger": "manual"}).encode()
    files.update(extra)
    return files


@pytest.fixture
def backup_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(bs, "BACKUP_DIR", tmp_path)
    return tmp_path


@pytest.mark.parametrize("bad", [
    "../etc/passwd", "..", "/etc/passwd", "flow_backup_20261031_120000_0a1b2c3d/../x",
    "flow_backup_20261031_120000_0A1B2C3D", "flow_backup_2026_x", "", "flow_backup_20261031_120000_0a1b2c3d.zip",
])
def test_malformed_ids_are_refused(backup_dir, bad):
    (backup_dir / "x.zip").write_bytes(make_zip(backup_files()))   # a file a traversal could reach
    with pytest.raises(ValueError, match="Backup not found"):
        bs.get_backup_path(bad)


def test_a_real_backup_id_resolves(backup_dir):
    (backup_dir / f"{GOOD_ID}.zip").write_bytes(make_zip(backup_files()))
    assert bs.get_backup_path(GOOD_ID) == (backup_dir / f"{GOOD_ID}.zip").resolve()
    with pytest.raises(ValueError):
        bs.get_backup_path(GOOD_ID.replace("0a1b", "ffff"))     # well-formed but missing


def test_list_skips_files_that_are_not_flow_backups(backup_dir):
    (backup_dir / f"{GOOD_ID}.zip").write_bytes(make_zip(backup_files()))
    (backup_dir / "holiday photos.zip").write_bytes(make_zip(backup_files()))
    assert [b["id"] for b in bs.list_backups()] == [GOOD_ID]


def test_a_good_upload_validates():
    info = bs.validate_zip_bytes(make_zip(backup_files()))
    assert info["trigger"] == "manual"


def test_too_many_members_is_refused():
    files = backup_files(**{f"extra{i}.json": b"[]" for i in range(bs.MAX_ZIP_MEMBERS)})
    with pytest.raises(ValueError, match="at most"):
        bs.validate_zip_bytes(make_zip(files))


def test_a_member_too_large_once_decompressed_is_refused(monkeypatch):
    monkeypatch.setattr(bs, "MAX_MEMBER_BYTES", 1000)
    content = make_zip(backup_files(**{"transactions.json": b"[" + b" " * 5000 + b"]"}))
    assert len(content) < 1000                                     # small on the wire
    with pytest.raises(ValueError, match="too large"):
        bs.validate_zip_bytes(content)


def test_too_much_data_in_total_is_refused(monkeypatch):
    monkeypatch.setattr(bs, "MAX_TOTAL_BYTES", 3000)
    big = b"[" + b" " * 900 + b"]"
    content = make_zip(backup_files(**{"accounts.json": big, "balances.json": big, "transactions.json": big,
                                       "register_accounts.json": big}))
    with pytest.raises(ValueError, match="decompressed"):
        bs.validate_zip_bytes(content)


def test_a_real_zip_bomb_is_refused_without_decompressing():
    content = make_zip(backup_files(**{"transactions.json": b"\0" * (bs.MAX_MEMBER_BYTES + 1)}))
    assert len(content) < 1_000_000
    with pytest.raises(ValueError, match="too large"):
        bs.validate_zip_bytes(content)


def test_an_upload_keeps_only_the_backups_own_files(backup_dir):
    content = make_zip(backup_files(**{"../../evil.json": b"x", "notes.txt": b"x", "card_accounts.json": b"[]"}))
    saved = bs.save_uploaded_backup(content, bs.validate_zip_bytes(content))
    assert bs.BACKUP_ID_RE.fullmatch(saved["id"])
    with zipfile.ZipFile(bs.get_backup_path(saved["id"])) as zf:
        names = set(zf.namelist())
    assert names == bs._REQUIRED_FILES | {"card_accounts.json"}


def _card_backup(**overrides) -> bytes:
    files = backup_files()
    files.update({f"{t}.json": b"[]" for t in bs.CARD_TABLES})
    files.update(overrides)
    return make_zip(files)


@pytest.mark.parametrize("rows", [
    [{"id) SELECT 1 WHERE false; CREATE TABLE pwned (x int); --": 1, "id": 1}],    # SQL in a column name
    [{"id": 1, "no_such_column": 2}],
    ["not an object"],
    {"id": 1},
])
def test_a_crafted_backup_is_refused_before_anything_is_deleted(tmp_path, rows):
    p = tmp_path / f"{GOOD_ID}.zip"
    p.write_bytes(_card_backup(**{"card_insight_reports.json": json.dumps(rows).encode()}))

    class NoDB:                     # any statement means the restore got past the check
        def execute(self, *a, **k):
            raise AssertionError("touched the database")

    with pytest.raises(ValueError, match="card_insight_reports"):
        bs.restore_from_path(p, NoDB())


def test_real_column_names_pass_the_check():
    bs._check_card_columns({"flow_settings": [{"key": "alert_pct", "value": "10"}],
                            "card_insight_reports": []})


# ── An uploaded backup's info is rebuilt from known fields ───────────────────

HOSTILE_INFO = {"created_at": 123, "trigger": "<b>x</b>", "id": GOOD_ID, "filename": "x.zip",
                "tables": {"accounts": "<img src=x onerror=alert(1)>", "transactions": 5, "balances": True,
                           "evil": 9, "card_transactions": -1}}


def test_info_keeps_only_known_fields_of_the_right_type():
    info = bs.clean_info(HOSTILE_INFO)
    assert info == {"created_at": "", "trigger": "uploaded", "tables": {"transactions": 5}}
    assert bs.clean_info("not a dict") == {"created_at": "", "trigger": "uploaded", "tables": {}}
    good = bs.clean_info({"created_at": "2026-10-31T12:00:00+00:00", "trigger": "manual", "tables": {"accounts": 3}})
    assert good == {"created_at": "2026-10-31T12:00:00+00:00", "trigger": "manual", "tables": {"accounts": 3}}


def test_a_hostile_upload_cannot_inject_markup_borrow_an_id_or_break_imports(backup_dir):
    content = make_zip(backup_files(**{"backup_info.json": json.dumps(HOSTILE_INFO).encode()}))
    saved = bs.save_uploaded_backup(content, bs.validate_zip_bytes(content))
    assert saved["id"] != GOOD_ID and saved["trigger"] == "uploaded"
    listed = bs.list_backups()
    assert [b["id"] for b in listed] == [saved["id"]]                       # its own id, not the one it named
    assert "<" not in json.dumps(listed)
    assert bs.has_backup_today() is False                                   # no AttributeError on created_at=123


def test_backup_info_that_isnt_json_is_refused():
    with pytest.raises(ValueError):
        bs.validate_zip_bytes(make_zip(backup_files(**{"backup_info.json": b"{nope"})))
