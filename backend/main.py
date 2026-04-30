from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sqlalchemy import text

from auth import (
    clear_session_cookie,
    create_session_cookie,
    record_login_attempt,
    require_auth,
    verify_password,
)
from database import Base, engine
from routers import data, upload, register, backup


@asynccontextmanager
async def lifespan(app: FastAPI):
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
        conn.execute(text(
            "ALTER TABLE register_accounts ADD COLUMN IF NOT EXISTS cutoff_balance NUMERIC(18,2)"
        ))
        conn.execute(text(
            "ALTER TABLE register_accounts ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT FALSE"
        ))
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
        conn.commit()
    yield


app = FastAPI(lifespan=lifespan)

app.include_router(upload.router)
app.include_router(data.router)
app.include_router(register.router)
app.include_router(backup.router)
app.mount("/static", StaticFiles(directory="/app/frontend/static"), name="static")


# ── Auth endpoints ────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    password: str


@app.post("/api/auth/login")
def login(body: LoginRequest, request: Request, response: Response):
    client_ip = request.client.host if request.client else "unknown"
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


@app.post("/api/auth/logout", dependencies=[Depends(require_auth)])
def logout(response: Response):
    clear_session_cookie(response)
    return {"ok": True}


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok"}


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.get("/")
def serve_frontend():
    return FileResponse("/app/frontend/index.html")
