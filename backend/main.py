from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from sqlalchemy import text

from auth import clear_session_cookie, create_session_cookie, require_auth, verify_password
from database import Base, engine
from routers import data, upload, register


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
        # Backfill bank_description on manual transactions created via Add-to-Ledger.
        # These have source='manual' but bank_description was not copied at creation time,
        # causing them to appear in the description autocomplete alongside user-typed entries.
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
        conn.commit()
    yield


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(upload.router)
app.include_router(data.router)
app.include_router(register.router)


# ── Auth endpoints ────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    password: str


@app.post("/api/auth/login")
def login(body: LoginRequest, response: Response):
    if not verify_password(body.password):
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
