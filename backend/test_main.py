"""
The app module itself imports and builds (every router included, every module it names imported):
a NameError here used to surface only at container start, after the deploy's health check failed.
FLOW_FRONTEND_DIR points the static mount at the repository's frontend/ instead of the container's path.
"""

from __future__ import annotations

import os
import pathlib

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg2://test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-" * 8)
os.environ["FLOW_FRONTEND_DIR"] = str(pathlib.Path(__file__).resolve().parent.parent / "frontend")

import main  # noqa: E402


def all_paths(app) -> set[str]:
    """Every route path, included routers unfolded (this FastAPI keeps them as one entry each)."""
    out = set()
    for r in app.routes:
        if hasattr(r, "original_router"):
            out |= {x.path for x in r.original_router.routes}
        elif hasattr(r, "path"):
            out.add(r.path)
    return out


def test_the_app_builds_with_every_router():
    paths = all_paths(main.app)
    for p in ("/api/health", "/api/attention", "/api/changes", "/api/backups/settings", "/api/version",
              "/api/cards/export-year.csv", "/api/data/export.csv", "/api/auth/change-password"):
        assert p in paths, p


def test_the_lifespan_names_only_imported_modules():
    # The lifespan runs at startup, after import: a module it names must already be imported here.
    assert main.backup_service.loop and main.change_log.read_via and main.update_check.loop
