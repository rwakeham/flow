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


# ── The daily backup, retention and encrypted copies ──────────────────────────

from datetime import datetime, timezone  # noqa: E402

import settings_store as st  # noqa: E402

UTC = timezone.utc


def test_due_waits_for_the_hour_and_wants_one_scheduled_backup_a_day():
    assert not bs.due(datetime(2026, 11, 2, 2, 59, tzinfo=UTC), None, 3)
    assert bs.due(datetime(2026, 11, 2, 3, 0, tzinfo=UTC), None, 3)
    assert not bs.due(datetime(2026, 11, 2, 15, 0, tzinfo=UTC), "2026-11-02T03:01:00+00:00", 3)
    assert bs.due(datetime(2026, 11, 2, 3, 5, tzinfo=UTC), "2026-11-01T23:59:00+00:00", 3)
    assert bs.due(datetime(2026, 11, 2, 0, 0, tzinfo=UTC), "2026-11-01T00:00:00+00:00", 0)
    assert bs.due(datetime(2026, 11, 2, 9, 0, tzinfo=UTC), "not a date", 3)       # a bad record never blocks


def test_last_scheduled_is_the_newest_scheduled_backups_created_at():
    entries = [{"id": "a", "trigger": "manual", "created_at": "2026-11-09T00:00:00+00:00"},
               {"id": "b", "trigger": "scheduled", "created_at": "2026-11-01T03:00:00+00:00"},
               {"id": "c", "trigger": "scheduled", "created_at": "2026-11-02T03:00:00+00:00"}]
    assert bs.last_scheduled(entries) == "2026-11-02T03:00:00+00:00"
    assert bs.last_scheduled([entries[0]]) is None


def _entry(i, trigger, day):
    return {"id": f"b{i}", "trigger": trigger, "created_at": f"2026-10-{day:02d}T03:00:00+00:00"}


def test_prune_keeps_the_newest_automatic_backups_and_never_the_users():
    entries = [_entry(1, "scheduled", 5), _entry(2, "manual", 1), _entry(3, "auto-pre-import", 9),
               _entry(4, "uploaded", 2), _entry(5, "scheduled", 7), _entry(6, "pre-restore", 3),
               _entry(7, "auto-pre-import", 4)]
    assert bs.prune(entries, 2) == ["b1", "b7"]           # the two newest (b3: 9th, b5: 7th) stay
    assert bs.prune(entries, 0) == []
    assert bs.prune(entries, 10) == []
    for e in entries:
        if e["trigger"] not in bs.PRUNABLE:
            assert e["id"] not in bs.prune(entries, 1)


def test_prune_backups_deletes_only_what_prune_says(backup_dir):
    for i, (trigger, day) in enumerate([("scheduled", 1), ("scheduled", 2), ("manual", 1)], 1):
        info = {"created_at": f"2026-10-{day:02d}T03:00:00+00:00", "trigger": trigger}
        (backup_dir / f"flow_backup_202610{day:02d}_030000_0000000{i}.zip").write_bytes(
            make_zip(backup_files(**{"backup_info.json": json.dumps(info).encode()})))
    assert bs.prune_backups(1) == 1
    left = sorted(p.name for p in backup_dir.glob("*.zip"))
    assert left == ["flow_backup_20261001_030000_00000003.zip", "flow_backup_20261002_030000_00000002.zip"]


def test_clean_info_keeps_the_scheduled_trigger():
    assert bs.clean_info({"created_at": "2026-11-02T03:00:00+00:00", "trigger": "scheduled"})["trigger"] == "scheduled"


@pytest.fixture
def fast_scrypt(monkeypatch):
    monkeypatch.setattr(bs, "SCRYPT_N_LOG2", 12)


def test_encrypted_copy_round_trips(fast_scrypt):
    blob = bs.encrypt_backup(b"PK\x03\x04 a zip", "correct horse battery")
    assert bs.is_encrypted(blob) and not bs.is_encrypted(b"PK\x03\x04")
    assert bs.decrypt_backup(blob, "correct horse battery") == b"PK\x03\x04 a zip"
    assert blob[:bs.HEADER_LEN].count(b"correct") == 0


def test_encrypted_copy_refuses_the_wrong_passphrase_and_any_change(fast_scrypt):
    blob = bs.encrypt_backup(b"data" * 100, "correct horse battery")
    with pytest.raises(bs.WrongPassphrase):
        bs.decrypt_backup(blob, "wrong horse battery")
    for pos in (10, len(blob) - 5):                     # in the header (the salt) and in the body
        changed = bytearray(blob)
        changed[pos] ^= 0x01
        with pytest.raises(bs.WrongPassphrase):
            bs.decrypt_backup(bytes(changed), "correct horse battery")


def test_encrypted_copy_header_is_checked_before_scrypt_runs(fast_scrypt, monkeypatch):
    with pytest.raises(ValueError):
        bs.decrypt_backup(b"FLOWBK1" + b"x" * 10, "correct horse battery")      # too short
    with pytest.raises(ValueError):
        bs.decrypt_backup(b"not one" + b"x" * 100, "correct horse battery")
    blob = bytearray(bs.encrypt_backup(b"data", "correct horse battery"))
    blob[len(bs.MAGIC) + 16] = 25                      # n_log2 = 25: 4 GB of memory
    monkeypatch.setattr(bs.hashlib, "scrypt", lambda *a, **k: pytest.fail("scrypt ran"))
    with pytest.raises(ValueError):
        bs.decrypt_backup(bytes(blob), "correct horse battery")


def test_real_scrypt_parameters_work():
    blob = bs.encrypt_backup(b"zip", "correct horse battery")
    assert bs.decrypt_backup(blob, "correct horse battery") == b"zip"


def test_backup_settings_defaults_and_clamps(monkeypatch):
    for var in ("FLOW_BACKUP_DAILY", "FLOW_BACKUP_KEEP", "FLOW_BACKUP_HOUR"):
        monkeypatch.delenv(var, raising=False)
    st.testing_state({})
    assert st.backup_daily_flag() and st.backup_keep() == 30 and st.backup_hour() == 3
    st.testing_state({st.BACKUP_DAILY: "off", st.BACKUP_KEEP: "99999", st.BACKUP_HOUR: "-4"})
    assert not st.backup_daily_flag() and st.backup_keep() == 3650 and st.backup_hour() == 0
    st.testing_state({st.BACKUP_KEEP: "lots", st.BACKUP_HOUR: "noon"})
    assert st.backup_keep() == 30 and st.backup_hour() == 3
    monkeypatch.setenv("FLOW_BACKUP_HOUR", "22")
    assert st.backup_hour() == 22 and st.setting_source(st.BACKUP_HOUR) == "env"
    st.testing_state({})


def test_keep_on_restore_holds_the_installs_schedule_and_change_log_stays_out_of_backups():
    assert set(bs.SESSION_KEYS) <= set(bs.KEEP_ON_RESTORE)
    assert {st.BACKUP_DAILY, st.BACKUP_KEEP, st.BACKUP_HOUR, st.UPDATE_CHECK} <= set(bs.KEEP_ON_RESTORE)
