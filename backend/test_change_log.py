"""
The change log (change_log.py): a row is added to the caller's session (saved by its commit), who did
it comes from the X-Flow-Via header through an app-wide async dependency, names are clipped, and the
cap keeps the newest rows. No database: a fake session, and one tiny FastAPI app for the dependency.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-" * 8)

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import change_log  # noqa: E402
import backup_service as bs  # noqa: E402


class FakeDB:
    def __init__(self):
        self.added, self.committed, self.executed = [], 0, []

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.committed += 1

    def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        db = self

        class R:
            rowcount = 7

            def scalar(self_inner):
                return 42 if len(db.executed) == 1 else None
        return R()


def test_record_adds_a_row_for_the_callers_commit():
    db = FakeDB()
    row = change_log.record(db, "register", "import", "Imported x.csv", {"account_id": 3, "imported": 12})
    assert db.added == [row] and db.committed == 0                     # the route commits
    assert (row.kind, row.action, row.summary, row.via) == ("register", "import", "Imported x.csv", "ui")
    assert json.loads(row.detail_json) == {"account_id": 3, "imported": 12}
    assert change_log.record(db, "x", "y", "z").detail_json is None


def _run_here(coro):
    """Drive a coroutine that never awaits in THIS context (asyncio.run would copy the context away)."""
    try:
        coro.send(None)
    except StopIteration:
        pass


def test_via_is_the_header_when_allowed_else_the_user():
    _run_here(change_log.read_via("assistant"))
    assert change_log.current_via() == "assistant"
    _run_here(change_log.read_via("hacker"))
    assert change_log.current_via() == "ui"
    _run_here(change_log.read_via(None))
    assert change_log.current_via() == "ui"
    db = FakeDB()
    assert change_log.record(db, "a", "b", "c", via="schedule").via == "schedule"      # explicit wins
    assert change_log.record(db, "a", "b", "c", via="nope").via == "ui"


def test_the_async_dependency_reaches_a_sync_route():
    """The point of read_via being async: a sync route, run in the threadpool, sees the value."""
    app = FastAPI(dependencies=[Depends(change_log.read_via)])

    @app.get("/who")
    def who():
        return {"via": change_log.current_via()}

    c = TestClient(app)
    assert c.get("/who", headers={"X-Flow-Via": "assistant"}).json() == {"via": "assistant"}
    assert c.get("/who").json() == {"via": "ui"}
    assert c.get("/who", headers={"X-Flow-Via": "root"}).json() == {"via": "ui"}


def test_names_are_clipped_to_one_line():
    assert change_log.clip("  Maple   Street\nStudio ") == "Maple Street Studio"
    assert change_log.clip("x" * 100, 10) == "x" * 9 + "…"
    assert change_log.clip(None) == ""
    assert change_log.money("1234.5") == "1,234.50" and change_log.money("n/a") == "n/a"
    assert len(change_log.record(FakeDB(), "a", "b", "s" * 500).summary) == 300


def test_prune_keeps_the_newest_rows():
    db = FakeDB()
    assert change_log.prune(db, keep=5) == 7                           # the cutoff id was 42
    assert db.executed[0][1] == {"keep": 5} and db.executed[1][1] == {"cutoff": 42} and db.committed == 1
    assert change_log.prune(FakeDB.__new__(FakeDB) if False else _empty(), keep=5) == 0


def _empty():
    db = FakeDB()

    class R:
        def scalar(self):
            return None
    db.execute = lambda *a, **k: R()
    return db


def test_rows_json_tolerates_bad_detail():
    from types import SimpleNamespace
    rows = [SimpleNamespace(id=1, created_at=None, kind="a", action="b", summary="s", via="ui", detail_json="{not json")]
    assert change_log.rows_json(rows)[0]["detail"] is None


def test_the_change_log_stays_out_of_backups():
    assert "change_log" not in bs.CARD_TABLES
