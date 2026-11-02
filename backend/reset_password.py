#!/usr/bin/env python3
"""
Reset the dashboard password from the host, for when it is forgotten:

    docker compose exec app python reset_password.py

It asks for a new password twice (typed hidden), stores its hash (settings_store) and
ends every session, so each browser signs in again with the new one. The other way is
to set FLOW_PASSWORD in .env and run ./deploy.sh: it is copied in on the next start,
and wins while it stays there.
"""

from __future__ import annotations

import getpass
import sys

import auth
import settings_store
from database import SessionLocal
from models import SESSION_EPOCH_KEY, AppSetting


def main() -> int:
    if settings_store.password_source() == "env" or auth.FLOW_PASSWORD:
        print("FLOW_PASSWORD is set in .env: it is the password, and would put itself back on the next start.\n"
              "Change it there (or remove it from .env, run ./deploy.sh, then run this again).", file=sys.stderr)
        return 1
    new = getpass.getpass("New dashboard password: ")
    problem = auth.password_problem(new)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    if getpass.getpass("Confirm: ") != new:
        print("The passwords differ.", file=sys.stderr)
        return 1
    with SessionLocal() as db:
        settings_store.set_password(db, new)
        row = db.get(AppSetting, SESSION_EPOCH_KEY) or AppSetting(key=SESSION_EPOCH_KEY)
        try:
            row.value = str(int(row.value or 0) + 1)
        except ValueError:
            row.value = "1"
        db.add(row)
        db.commit()
    print("Password changed. Restart Flow (./deploy.sh, or docker compose restart app) and sign in with it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
