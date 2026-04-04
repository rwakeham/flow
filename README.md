# Flow

A self-hosted personal finance dashboard. Track net worth over time, manage investments and liabilities, and run a full cash flow register with bank import and reconciliation.

## Features

**Dashboard**
- Net worth chart with month-over-month history
- Asset allocation breakdown (investments, real estate, cash, etc.)
- Liability tracking
- Manual balance entry with account override support
- Accounts marked as "ignored" are excluded from charts

**Cash Flow Register**
- Multiple named register accounts (e.g. Checking, Savings)
- Manual transaction entry with description autocomplete from prior transactions
- Import bank statements (CSV) — supports multiple date formats and column layouts
- Running balance column (Quicken-style: balance updates on every row)
- Reconciliation: match bank-imported rows to manual ledger entries
- Bulk reconcile: select multiple bank rows and add or delete in one action
- Duplicate a manual transaction to quickly enter similar entries
- "Today" marker row so you can see where the current date falls relative to transactions
- Balance cutoff: set a cutoff date + balance to hide older transactions
- Default account: one account auto-selected when you open the register

**General**
- Single-password authentication with session cookie
- Dark mode support
- Fully self-hosted — no external services or accounts required

## Requirements

- Docker with Compose (v2 recommended, v1 also supported)
- `openssl` (for secret generation during setup)
- A logo image at `frontend/static/logo.png` (optional — shown in the header)

## First-time Setup

```bash
git clone <repo-url>
cd flow
./setup.sh
```

`setup.sh` will:
1. Check for Docker and Compose
2. Generate a random database password and secret key and save them to `.env`
3. Find a free port (default 8000) and save it to `.env`
4. Prompt you to set a dashboard password
5. Build and start the containers
6. Wait for the app to be healthy and print the URL

Re-running `setup.sh` is safe — it keeps your existing secrets and offers to update the password.

## Deploying Updates

```bash
./deploy.sh
```

`deploy.sh` pulls the latest code from `main`, auto-detects any pending `claude/` feature branches, offers to merge them, then rebuilds and restarts the containers. If `requirements.txt` changed it rebuilds from scratch automatically.

```bash
./deploy.sh --skip-git   # rebuild and restart without touching git
```

## Environment Variables

Stored in `.env` (created by `setup.sh`, never committed):

| Variable | Description |
|---|---|
| `POSTGRES_PASSWORD` | Password for the PostgreSQL database |
| `FLOW_PASSWORD` | Password to log in to the dashboard |
| `SECRET_KEY` | Secret used to sign session cookies |
| `APP_PORT` | Host port the app listens on (default: 8000) |

## Architecture

```
flow/
├── backend/              # FastAPI application
│   ├── main.py           # App entry point, lifespan migrations
│   ├── models.py         # SQLAlchemy ORM models
│   ├── auth.py           # Session-cookie authentication
│   ├── database.py       # DB engine and session factory
│   ├── parser.py         # Investment/balance CSV parser
│   ├── register_parser.py# Bank statement CSV parser
│   ├── reconciler.py     # Bank-to-ledger match suggestions
│   └── routers/
│       ├── data.py       # /api/data — accounts, balances, timeseries
│       ├── upload.py     # /api/upload — CSV import
│       └── register.py   # /api/register — cash flow register
├── frontend/
│   ├── index.html        # Single-page app (vanilla JS)
│   └── static/           # Static assets (logo, etc.)
├── docker-compose.yml
├── setup.sh              # First-run setup
└── deploy.sh             # Update and redeploy
```

**Stack:** FastAPI · SQLAlchemy · PostgreSQL 16 · vanilla JS · Docker

## Data Models

| Model | Description |
|---|---|
| `Account` | Investment/liability account (name, type, ignored flag) |
| `Balance` | Monthly balance snapshot for an account |
| `RegisterAccount` | Cash flow register account (checking, savings, etc.) |
| `Transaction` | Individual register transaction (manual or bank-imported) |

## Bank Import

Go to **Cash Flow → Import Bank CSV**. The parser handles various CSV layouts and date formats automatically (`MM/DD/YY`, `MM/DD/YYYY`, `YYYY-MM-DD`, etc.). After import, unmatched bank rows appear in the register for reconciliation — match them to existing manual entries or use **Add to Ledger** to create one.

## Useful Commands

```bash
docker compose logs -f app   # live app logs
docker compose down          # stop everything
docker compose down -v       # stop and delete the database (destructive)
```
