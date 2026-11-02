from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sqlalchemy import text

import asyncio
import json
import logging

from auth import (
    COOKIE_NAME,
    check_settings,
    clear_session_cookie,
    create_session_cookie,
    login_lock,
    password_problem,
    record_login_attempt,
    request_client_ip,
    require_auth,
    require_password,
    revoke_session,
    revoked_sessions,
    session_valid,
    set_revoked,
    set_session_epoch,
    verify_password,
)
import asset_versions
import import_profile
import settings_store
import update_check
from body_limit import MB, BodyLimit, cookie_value, too_many_rows
from security_headers import SameOrigin, SecurityHeaders
import card_taxonomy
from database import Base, SessionLocal, engine
from models import REVOKED_SESSIONS_KEY, SESSION_EPOCH_KEY, AppSetting
from routers import data, upload, register, backup, cards, ask, balances, cashflow, import_profiles, ai, updates, attention

log = logging.getLogger("flow")


@asynccontextmanager
async def lifespan(app: FastAPI):
    check_settings()   # a placeholder SECRET_KEY or FLOW_PASSWORD stops startup (and the deploy's health check)
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        conn.execute(text(
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS ignored BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        conn.execute(text(
            "ALTER TABLE register_accounts ADD COLUMN IF NOT EXISTS notes TEXT"
        ))
        conn.execute(text(
            "ALTER TABLE register_accounts ADD COLUMN IF NOT EXISTS cutoff_date DATE"
        ))
        conn.execute(text("ALTER TABLE register_accounts ADD COLUMN IF NOT EXISTS institution VARCHAR"))
        conn.execute(text(
            "ALTER TABLE register_accounts ADD COLUMN IF NOT EXISTS cutoff_balance NUMERIC(18,2)"
        ))
        conn.execute(text(
            "ALTER TABLE register_accounts ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        conn.execute(text(
            "ALTER TABLE ai_request_log ADD COLUMN IF NOT EXISTS error_kind VARCHAR(12)"
        ))
        conn.execute(text(
            "ALTER TABLE ai_request_log ADD COLUMN IF NOT EXISTS key_fingerprint VARCHAR(16)"
        ))
        conn.execute(text("ALTER TABLE ai_request_log ADD COLUMN IF NOT EXISTS request_json TEXT"))
        conn.execute(text("ALTER TABLE ai_request_log ADD COLUMN IF NOT EXISTS response_json TEXT"))
        conn.execute(text("ALTER TABLE card_insight_reports ADD COLUMN IF NOT EXISTS card_aliases_json TEXT"))
        # One account, several cards (authorized users, reissues): last4 holds a list.
        conn.execute(text("ALTER TABLE card_accounts ALTER COLUMN last4 TYPE TEXT"))
        conn.execute(text("ALTER TABLE card_transactions "
                          "ADD COLUMN IF NOT EXISTS name_set_by_user BOOLEAN NOT NULL DEFAULT FALSE"))
        # Amount rules can cover a whole category, not only one merchant.
        conn.execute(text("ALTER TABLE card_amount_rules ADD COLUMN IF NOT EXISTS scope_l1 VARCHAR"))
        conn.execute(text("ALTER TABLE card_amount_rules ADD COLUMN IF NOT EXISTS scope_l2 VARCHAR"))
        conn.execute(text("ALTER TABLE card_amount_rules ALTER COLUMN merchant DROP NOT NULL"))
        conn.execute(text("ALTER TABLE card_transactions ADD COLUMN IF NOT EXISTS pre_rule_category VARCHAR"))
        conn.execute(text("ALTER TABLE card_transactions ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ"))
        conn.execute(text("ALTER TABLE card_transactions ADD COLUMN IF NOT EXISTS reviewed_reasons VARCHAR"))
        conn.execute(text("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS institution VARCHAR"))
        conn.execute(text("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last4 TEXT"))
        conn.execute(text("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS alert_pct DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS alert_abs DOUBLE PRECISION"))
        conn.execute(text("ALTER TABLE balances ADD COLUMN IF NOT EXISTS source VARCHAR(10) NOT NULL DEFAULT 'upload'"))
        conn.execute(text("ALTER TABLE balances ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now()"))
        # Ask's daily limit was replaced by a monthly count; its only setting lived here.
        conn.execute(text("DROP TABLE IF EXISTS app_settings"))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  name TEXT PRIMARY KEY,"
            "  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            ")"
        ))
        already_applied = conn.execute(text(
            "SELECT 1 FROM schema_migrations WHERE name = :name"
        ), {"name": "backfill_bank_description_on_manual"}).first()
        if not already_applied:
            conn.execute(text("""
                UPDATE transactions AS m
                   SET bank_description = b.bank_description
                  FROM transactions AS b
                 WHERE m.source          = 'manual'
                   AND m.matched_to_id   = b.id
                   AND b.source          = 'bank'
                   AND b.bank_description IS NOT NULL
                   AND m.bank_description IS NULL
            """))
            conn.execute(text(
                "INSERT INTO schema_migrations (name) VALUES (:name)"
            ), {"name": "backfill_bank_description_on_manual"})
        if not conn.execute(text(
            "SELECT 1 FROM schema_migrations WHERE name = :name"
        ), {"name": "card_compact_merchant_keys"}).first():
            _migrate_compact_merchant_keys(conn)
            conn.execute(text(
                "INSERT INTO schema_migrations (name) VALUES (:name)"
            ), {"name": "card_compact_merchant_keys"})
        if not conn.execute(text(
            "SELECT 1 FROM schema_migrations WHERE name = :name"
        ), {"name": "card_rename_brewery_detail"}).first():
            _migrate_rename_detail(conn, "Alcohol / Wine", "Brewery / distillery", "Brewery / distillery / winery")
            conn.execute(text(
                "INSERT INTO schema_migrations (name) VALUES (:name)"
            ), {"name": "card_rename_brewery_detail"})
        if not conn.execute(text(
            "SELECT 1 FROM schema_migrations WHERE name = :name"
        ), {"name": "card_split_subscriptions_telecom"}).first():
            _migrate_split_subscriptions_telecom(conn)
            conn.execute(text(
                "INSERT INTO schema_migrations (name) VALUES (:name)"
            ), {"name": "card_split_subscriptions_telecom"})
        if not conn.execute(text(
            "SELECT 1 FROM schema_migrations WHERE name = :name"
        ), {"name": "card_verdicts_per_card"}).first():
            # Recurring verdicts became per card. Existing ones keep card 0 ("every card").
            conn.execute(text("ALTER TABLE card_recurring_verdicts "
                              "ADD COLUMN IF NOT EXISTS card_account_id INTEGER NOT NULL DEFAULT 0"))
            conn.execute(text("ALTER TABLE card_recurring_verdicts DROP CONSTRAINT IF EXISTS card_recurring_verdicts_pkey"))
            conn.execute(text("ALTER TABLE card_recurring_verdicts ADD PRIMARY KEY (canonical_merchant, card_account_id)"))
            conn.execute(text(
                "INSERT INTO schema_migrations (name) VALUES (:name)"
            ), {"name": "card_verdicts_per_card"})
        # Import formats: every layout is a stored format, and a card import records the one it used.
        conn.execute(text("ALTER TABLE card_imports ADD COLUMN IF NOT EXISTS profile_id INTEGER "
                          "REFERENCES import_profiles(id) ON DELETE SET NULL"))
        # A recurring series can be marked ended without a confirm/dismiss verdict.
        conn.execute(text("ALTER TABLE card_recurring_verdicts ADD COLUMN IF NOT EXISTS ended_through DATE"))
        conn.execute(text("ALTER TABLE card_recurring_verdicts ALTER COLUMN status DROP NOT NULL"))
        conn.commit()
    with SessionLocal() as db:
        card_taxonomy.refresh(db)        # your own subcategories join the built-in ones
        set_session_epoch(_read_session_epoch(db))
        set_revoked(_read_revoked(db))
        # The password, API key and AI settings live in the database; whatever .env still sets is
        # copied in (and wins while it is there). No password anywhere stops the start.
        settings_store.load(db)
        copied = settings_store.import_from_env(db)
        if copied:
            log.info("Copied %s from .env into Flow's settings; they can be removed from .env now "
                     "(./setup.sh offers to)", ", ".join(copied))
        require_password(settings_store.password_hash())
        update_check.load(db)            # the last release check and the version the user hid
        # The merchant rules were made generic (every US airline and large hotel group its own
        # detail, no local names): stored charges follow the rules, and a merchant no rule
        # recognizes any more keeps its category as a remembered choice. After refresh, so
        # renamed categories are applied.
        if not db.execute(text("SELECT 1 FROM schema_migrations WHERE name = :name"),
                          {"name": "card_rules_generic_v1"}).first():
            import card_rules_refresh
            card_rules_refresh.run(db)
            db.execute(text("INSERT INTO schema_migrations (name) VALUES (:name)"), {"name": "card_rules_generic_v1"})
            db.commit()
    # The daily check for a newer release runs on its own (update_check.loop: two minutes after
    # startup, then daily, skipped while the switch is off); cancelled at shutdown.
    checker = asyncio.create_task(update_check.loop(SessionLocal))
    # The daily backup and retention (backup_service.loop: a few minutes after startup, then every
    # 15 minutes: the day's backup once it is due and the switch is on, then the automatic backups
    # beyond the keep count go).
    backups = asyncio.create_task(backup_service.loop(SessionLocal))
    yield
    checker.cancel()
    backups.cancel()


def _read_session_epoch(db) -> int:
    row = db.get(AppSetting, SESSION_EPOCH_KEY)
    try:
        return int(row.value) if row and row.value else 0
    except ValueError:
        return 0


def _read_revoked(db) -> dict[str, float]:
    """The session ids "Sign out" ended (flow_settings, JSON {id: expiry}); a damaged row reads as none."""
    row = db.get(AppSetting, REVOKED_SESSIONS_KEY)
    try:
        data = json.loads(row.value) if row and row.value else {}
        return {str(k): float(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _save_revoked(db) -> None:
    row = db.get(AppSetting, REVOKED_SESSIONS_KEY) or AppSetting(key=REVOKED_SESSIONS_KEY)
    row.value = json.dumps(revoked_sessions())
    db.add(row)
    db.commit()


def _migrate_compact_merchant_keys(conn) -> None:
    """
    Card merchant keys became compact (letters and digits only, see
    card_privacy.compact_key), so "CIRCLE K", "CIRCLE_K" and "CIRCLEK" are one
    merchant. Rewrite stored keys; where several cache rows collapse into one,
    keep the user's row over an AI row, then the most recently updated.
    """
    compact = "regexp_replace(upper(merchant_key), '[^A-Z0-9]', '', 'g')"
    conn.execute(text(f"UPDATE card_transactions SET merchant_key = {compact} WHERE merchant_key ~ '[^A-Z0-9]'"))
    conn.execute(text("UPDATE card_transactions SET merchant_key = '' WHERE length(merchant_key) < 3"))
    cols = ("canonical_merchant, merchant_family, cat_l1, cat_l2, cat_l3, source, subscription_hint, "
            "confidence, model, prompt_version, sanitized_label, created_at, updated_at")
    conn.execute(text(f"""
        CREATE TEMP TABLE merchant_categories_compact ON COMMIT DROP AS
        SELECT DISTINCT ON (nk) nk, {cols}
          FROM (SELECT *, {compact} AS nk FROM merchant_categories) s
         WHERE length(nk) >= 3
         ORDER BY nk, (source = 'user') DESC, updated_at DESC NULLS LAST
    """))
    conn.execute(text("DELETE FROM merchant_categories"))
    conn.execute(text(f"""
        INSERT INTO merchant_categories (merchant_key, {cols})
        SELECT nk, {cols} FROM merchant_categories_compact
    """))


def _migrate_rename_detail(conn, l2: str, old: str, new: str) -> None:
    """
    A built-in detail (L3) was renamed: rewrite it on stored transactions, the
    merchant cache, amount rules and the category recorded before an amount rule.
    """
    for table in ("card_transactions", "merchant_categories", "card_amount_rules"):
        conn.execute(text(f"UPDATE {table} SET cat_l3 = :new WHERE cat_l2 = :l2 AND cat_l3 = :old"),
                     {"new": new, "old": old, "l2": l2})
    conn.execute(text(
        "UPDATE card_transactions SET pre_rule_category = replace(pre_rule_category, :o, :n) "
        "WHERE pre_rule_category LIKE :like"
    ), {"o": f"|{l2}|{old}|", "n": f"|{l2}|{new}|", "like": f"%|{l2}|{old}|%"})


def _migrate_split_subscriptions_telecom(conn) -> None:
    """
    "Subscriptions / Telecom" was split into Bills & Utilities and Digital &
    Media (memberships to Shopping or Services), and Home > Utilities moved to
    Bills & Utilities. Rewrite the L1 on stored transactions and cached
    merchants, including the user's own choices; L2 and L3 are unchanged.
    """
    for table in ("card_transactions", "merchant_categories"):
        triples = conn.execute(text(
            f"SELECT DISTINCT cat_l1, cat_l2, cat_l3 FROM {table} "
            "WHERE cat_l1 = :legacy OR (cat_l1 = 'Home' AND cat_l2 = 'Utilities')"
        ), {"legacy": card_taxonomy.LEGACY_SUB_L1}).all()
        for l1, l2, l3 in triples:
            new_l1 = card_taxonomy.split_legacy_category(l1, l2, l3)
            if new_l1:
                conn.execute(text(
                    f"UPDATE {table} SET cat_l1 = :new WHERE cat_l1 = :l1 AND cat_l2 = :l2 "
                    "AND cat_l3 IS NOT DISTINCT FROM :l3"
                ), {"new": new_l1, "l1": l1, "l2": l2, "l3": l3})


# No /docs, /redoc or /openapi.json: the API is the app's own, not a public one.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# Every request body is capped before it is read: 10 MB files plus room for the form around
# them; a bulk import plan carries up to MAX_BATCH_FILES of them. Added first so the security
# headers (added after, so outside it) are on its 413s too.
app.add_middleware(BodyLimit, default=12 * MB,
                   paths={"/api/import/batch/plan": upload.MAX_BATCH_FILES * upload.MAX_UPLOAD_BYTES + 2 * MB},
                   signed_in=lambda scope: session_valid(cookie_value(scope, COOKIE_NAME)))
# Outside the body limit: a cross-origin POST is refused before any of its body is read.
app.add_middleware(SameOrigin)
app.add_middleware(SecurityHeaders)
# A file past import_profile.MAX_ROWS, wherever it is read (import, detect, editor, balance file).
app.add_exception_handler(import_profile.TooManyRows, too_many_rows)

app.include_router(upload.router)
app.include_router(attention.router)
app.include_router(data.router)
app.include_router(register.router)
app.include_router(backup.router)
app.include_router(cards.router)
app.include_router(ask.router)
app.include_router(balances.router)
app.include_router(cashflow.router)
app.include_router(import_profiles.router)
app.include_router(ai.router)
app.include_router(updates.router)
FRONTEND_DIR = "/app/frontend"
NO_CACHE = "no-cache"          # keep a copy, but check it (ETag) before every use


class RevalidatedStatic(StaticFiles):
    """/static files with Cache-Control: no-cache, so a deploy's new JS and CSS are picked up."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = NO_CACHE
        return resp


app.mount("/static", RevalidatedStatic(directory=f"{FRONTEND_DIR}/static"), name="static")


# ── Auth endpoints ────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    password: str


@app.post("/api/auth/login")
def login(body: LoginRequest, request: Request, response: Response):
    client_ip = request_client_ip(request)   # behind FLOW_TRUSTED_PROXIES, the forwarded client
    # The lock is checked before the password, so a locked-out client learns nothing from a guess.
    locked, retry_after = login_lock(client_ip)
    if not locked:
        ok = verify_password(body.password)
        locked, retry_after = record_login_attempt(client_ip, success=ok)
    if locked:
        raise HTTPException(
            status_code=429,
            detail="Too many failed attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    if not ok:
        raise HTTPException(status_code=401, detail="Invalid password")
    create_session_cookie(response)
    return {"ok": True}


class ChangePasswordRequest(BaseModel):
    current: str
    new: str


@app.post("/api/auth/change-password", dependencies=[Depends(require_auth)])
def change_password(body: ChangePasswordRequest, response: Response):
    """Settings → Account → Change password: the current password is required, and every session ends
    (the cookie tag covers the password hash), this one included."""
    if settings_store.password_source() == "env":
        raise HTTPException(status_code=409, detail="FLOW_PASSWORD is set in .env and would put the old "
                            "password back on the next start. Remove it from .env (./setup.sh offers to), "
                            "run ./deploy.sh, then change the password here.")
    if not verify_password(body.current):
        raise HTTPException(status_code=403, detail="The current password is wrong.")
    problem = password_problem(body.new)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    with SessionLocal() as db:
        settings_store.set_password(db, body.new)
    clear_session_cookie(response)
    return {"ok": True}


@app.post("/api/auth/logout", dependencies=[Depends(require_auth)])
def logout(request: Request, response: Response):
    """End this session: the cookie is cleared and its id revoked, so a copy of it is no good either."""
    if revoke_session(request.cookies.get(COOKIE_NAME)):
        with SessionLocal() as db:
            _save_revoked(db)
    clear_session_cookie(response)
    return {"ok": True}


@app.post("/api/auth/logout-everywhere", dependencies=[Depends(require_auth)])
def logout_everywhere(response: Response):
    """End every session, in every browser and device (this one too): bump the session generation."""
    with SessionLocal() as db:
        epoch = _read_session_epoch(db) + 1
        row = db.get(AppSetting, SESSION_EPOCH_KEY) or AppSetting(key=SESSION_EPOCH_KEY)
        row.value = str(epoch)
        db.add(row)
        db.commit()
    set_session_epoch(epoch)
    set_revoked({})                  # every id is ended by the new generation; the list can go
    with SessionLocal() as db:
        _save_revoked(db)
    clear_session_cookie(response)
    return {"ok": True}


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok"}


# /api/version (signed in only, so the bare health check gives away no version) is
# routers/updates.py: the running version and the update check.


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.get("/")
def serve_frontend():
    """The single-page app, with its /static references content-versioned (asset_versions)."""
    with open(f"{FRONTEND_DIR}/index.html", encoding="utf-8") as f:
        html = asset_versions.versioned(f.read(), f"{FRONTEND_DIR}/static")
    return HTMLResponse(html, headers={"Cache-Control": NO_CACHE})
