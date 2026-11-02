# CLAUDE.md

Developer guide for AI-assisted work on Flow.

## Project overview

Flow is a self-hosted personal finance dashboard: net worth tracking, investment/liability
balances (typed, pasted from a bank's account summary, or read from a holdings export), a full cash flow register
with bank statement import and reconciliation, and upload-only credit card accounts with categorization,
subscription detection and spend analytics. Every import file is read with a user-defined import format (no
bank layouts in code).
Stack: FastAPI + SQLAlchemy + PostgreSQL 16, vanilla JS frontend, Docker Compose.

## Repository layout

```
backend/                — FastAPI application
  main.py                 App entry point; lifespan runs schema setup + migrations
  models.py               SQLAlchemy ORM models
  auth.py                 Single-password session-cookie authentication
  database.py             DB engine and session factory (reads DATABASE_URL)
  cash_flow.py            Cash flow Sankey (pure): payee keys and roles, card-payment/transfer matching, the diagram
  asset_versions.py       Content-hash ?v= on index.html's /static references (cache-busting)
  exports.py              CSV exports (pure): formula-safe cells, csv_response, the register / balance-sheet
                          row builders; spreadsheet_safe and download_disposition live here
  VERSION                 Flow's version, MAJOR.MINOR.PATCH: the only place it is written (see Releases)
  version.py              Reads VERSION (APP_VERSION; "unknown" if missing or malformed)
  update_check.py         Daily check of GitHub's latest release against APP_VERSION: strict reply parsing and the
                          comparison (pure), the stored result, the background loop
  settings_store.py       Settings (AI models, switch, redact words: flow_settings) and secrets (password hash,
                          encrypted API key: flow_secrets) in the database; .env wins while a variable is set
  reset_password.py       Host-side password reset: docker compose exec app python reset_password.py
  balance_sheet.py        Balance entry (pure): paste reading, scrubbed AI outline, preview checks, carry-forward
  attention.py            The Dashboard's "Needs attention" panel (pure): the ordered items and where each opens
  change_log.py           The change log: record() a row with the route's commit, who did it (X-Flow-Via), the cap
  reconciler.py           Bank-to-ledger match suggestions
  import_dedup.py         De-duplication of re-imported bank statements
  import_batch.py         Bulk import: suggests each file's account (pure; no DB)
  import_profile.py       Import formats (pure): Spec, parse any CSV/TSV/XLSX by its format, sign check,
                          drift diff; the row types (BankRow, LedgerRow, CardRow, CardParseResult) and adapters
  import_profile_resolve.py Which saved format reads a file for an account (match / changed header / none),
                          format links, and the structured import errors
  import_infer.py         Drafts a format from a sample file; outline() = column shapes only (for the Assistant)
  testdata/               Synthetic sample files and their formats (sample_files.py), named by layout shape,
                          with the rows each reads to (expected_rows/)
  recurring.py            Recurring-transaction projection
  backup_service.py       Backup file generation (all register and card tables); backup ids and zip size limits;
                          the daily backup loop, retention (due / prune) and the encrypted copy (encrypt_backup)
  security_headers.py     ASGI middleware: CSP, nosniff, Referrer-Policy, X-Frame-Options on every response;
                          SameOrigin refuses state-changing requests from another origin (403)
  body_limit.py           ASGI middleware: request body size cap (413), per-path limits
  card_rules.py           Card txn types + merchant rules (SPONSOR_VENUES, then COMMON_MERCHANT_RULES, then
                          MERCHANT_RULES; US_AIRLINES / AIRLINE_EXTRAS / HOTEL_GROUPS generate per-brand details)
  card_rules_refresh.py   Re-files stored rule-categorized charges after the rules change (card_rules_generic_v1)
  card_taxonomy.py        L1 > L2 > L3 category tree, derived from card_rules
  card_privacy.py         Merchant sanitization: the boundary for every AI call
  card_classifier.py      Category precedence (user > rule > cached AI > weak guess)
  card_import.py          Card import planning: dedup, typing, sanitizing, classifying
  card_ai.py              Anthropic API: merchant categorization and aggregate-only insights
  card_amount_rules.py    Category by amount range, per merchant or per category
  card_recurring.py       Subscription / recurring-charge detection
  card_analytics.py       Spend analytics tables, the insights payload and the year-end summary (year_summary)
  ask.py                  Assistant chat (code name "ask"): history checks, topic gate, streamed tool loop, number check
  ask_data.py             Assistant's index, trim list and lookup tools (summaries; aliases; cleaned names)
  ask_proposals.py        Assistant's propose_* tools: checked change proposals the user applies
  ask_files.py            A statement file attached in the Assistant: outline + draft summary, re-checked per request
  test_*.py               pytest suites (pure functions; no DB, no network)
  Dockerfile
  .dockerignore           Keeps tests, testdata/ and caches out of the image
  routers/
    data.py               /api/accounts (create/edit/delete, history), /api/data — timeseries, summary
    cashflow.py           /api/cashflow — the Sankey for a period; payee roles (Sort payees)
    balances.py           /api/balances — paste read (+ AI fallback), preview, apply, single edits, alert settings
    upload.py             /api/import/detect and /api/import/batch/plan (which saved formats read a file)
    attention.py          /api/attention — the Needs attention panel's items, gathered from the other routers
    changes.py            /api/changes — the change log, newest first, a page at a time
    import_profiles.py    /api/import-profiles — formats, account links, draft, preview, export/import
    register.py           /api/register — cash flow register
    cards.py              /api/cards — card accounts, imports, transactions, analytics, AI
    ask.py                /api/ask — Assistant chat (NDJSON stream), status and monthly question count
    backup.py             /api/backup
    ai.py                 /api/ai — the AI settings (key, switch, models, redact words: settings_store), the AI privacy log
    updates.py            /api/version — the running version and the update check (check now, hide, the daily switch)
tests/
  test_deploy_script.py   Behaviour tests for deploy.sh and setup.sh (deploys nothing)
docs/screenshots/       README screenshots, taken from a demo install with synthetic data (no real data)
frontend/               — Static frontend, served directly by the backend
  index.html              Single-page app (vanilla JS)
  static/                 Static assets, mounted at /static: logo, cards.js / cards.css
                          (the Cards section UI), balances.js / balances.css (Update balances and the
                          account editor), cashflow.js / cashflow.css (Dashboard → Cash Flow Analysis), importprofiles.js / .css (import formats in the
                          import dialog), accounts.css (the shared account-dialog frame), ask.js / ask.css
                          (the Assistant panel), ai.js / ai.css (Settings → AI: models and privacy log) and settings.js /
                          settings.css (the ⚙ Settings dialog: AI, Backups, Updates, Account sections), loaded
                          after index.html's inline script; vendor/ holds Chart.js (MIT)
docker-compose.yml      — db (Postgres) + app (FastAPI) services
.github/workflows/
  ci.yml                  Tests (backend, deploy.sh/setup.sh, release tooling) on pull requests and main
  release.yml             Every push to main bumps the version, tags and publishes the GitHub Release;
                          pull requests get a preview (see Releases)
.github/dependabot.yml  — Weekly PRs for new base-image digests and action pins
.github/release/        — Release tool and privacy check (see Releases); push_check.py is the pre-push hook
.claude/settings.json   — Claude Code hooks: push_check.py runs before every git push
setup.sh                — Config wizard; writes .env (credentials, port, first-start password, optional AI key), hands off to deploy.sh
deploy.sh               — Deployment pipeline (see Deployment below)
.env.example            — Template for .env
README.md               — User guide (features, what leaves the machine, running it safely, limits)
LICENSE                 — MIT
SECURITY.md             — Security model (private-network deployment, design decisions, accepted risks); reporting
```

## Running locally

Flow runs only under Docker Compose; there is no supported way to run the app outside it
(paths such as `/app/frontend` and `/backups` are the container's).

```bash
./setup.sh          # first run: writes .env, then deploys via deploy.sh
./deploy.sh          # after a change: rebuilds and restarts the containers
```

The frontend is static and bind-mounted (`./frontend`, read-only), so a frontend-only change
needs no rebuild: reload the page (every response is `no-cache`). A backend change needs
`./deploy.sh`. Logs: `docker compose logs -f app`.

The tests need no database, Docker or running app (pure functions). Run them with Python
3.12, the version `backend/Dockerfile` builds on; pytest is not in `requirements.txt`, which
is what the image installs:

```bash
cd backend
pip install -r requirements.txt pytest
python -m pytest
python3 ../tests/test_deploy_script.py   # deploy.sh behaviour, in throwaway repos; deploys nothing
python -m pytest ../.github/release      # release tool and the privacy check over every tracked file
```

CI (`.github/workflows/ci.yml`) runs the same on every pull request and push to `main`.

## Architecture

- The backend is a single FastAPI app (`main.py`) that also serves the static frontend —
  there is no separate frontend server or build step.
  `/` serves `index.html` with every `src`/`href="/static/..."` given `?v=<content hash>`
  (`asset_versions.versioned`) and `Cache-Control: no-cache`; `/static` files are no-cache too
  (revalidated by ETag). A deploy's new JS/CSS is therefore always picked up: reference new
  files as `/static/...` in `index.html` and they are versioned automatically.
- On startup, the `lifespan` handler in `main.py` creates any missing tables
  (`Base.metadata.create_all`), applies a small set of idempotent `ALTER TABLE ... IF NOT
  EXISTS` statements, and runs one-off data migrations tracked in a `schema_migrations`
  table so they apply exactly once. There is no separate migration tool (no Alembic) —
  schema changes are added directly to the lifespan handler.
- `GET /api/health` returns `{"status": "ok"}` once the app has started; because migrations
  run during startup, a health check failure after a deploy means either the app failed to
  boot (migration or `DATABASE_URL` problem) or the app never came up.
- Authentication is a single shared dashboard password, verified in `auth.py` against a scrypt
  hash kept in the database (`settings_store`, table `flow_secrets`; `FLOW_PASSWORD` in `.env` sets it
  on the first start and resets it while it stays there), backed by a signed session cookie
  (`SECRET_KEY`). There are no user accounts. The cookie carries `{"v": 2, "p": <HMAC of the session
  generation and the password hash under SECRET_KEY>, "s": <session id>}`, so changing the password
  (Settings → Account → Change password, `POST /api/auth/change-password`, current password required;
  `reset_password.py` from the host; or `FLOW_PASSWORD`) or the key signs every browser out; the tag is
  keyed so the readable cookie can't be used to guess the password. "Sign out everywhere" (top bar
  Sign out ▾, `POST /api/auth/logout-everywhere`) bumps the generation (`flow_settings`
  `session_epoch`, loaded at startup into `auth.set_session_epoch`), ending every cookie,
  copies included. The cookie also carries a random session id (`s`; `SESSION_VERSION` 2): "Sign out"
  (`POST /api/auth/logout`) revokes that id (`auth.revoke_session`; the list lives in `flow_settings`
  `revoked_sessions` as JSON until each cookie would have expired, loaded at startup into
  `auth.set_revoked`), so a copy of a signed-out cookie is refused while other devices stay in. A backup
  restore keeps the running install's generation and revoked list (`backup_service.SESSION_KEYS`). Startup refuses a key under 32 characters, the
  `.env.example` placeholders and a blank or example password (`auth.check_settings`, first
  thing in the lifespan); `deploy.sh`'s preflight and `setup.sh` apply the same lists (keep the
  three in step). The login throttle (5 failures per 5 minutes; `auth.login_lock` is checked
  BEFORE the password, so a locked-out client gets 429 whatever it sends) counts the direct peer, or behind
  a proxy in `FLOW_TRUSTED_PROXIES` the right-most untrusted `X-Forwarded-For` entry
  (`auth.client_ip`); its table is capped at 10,000 clients. `/docs`, `/redoc` and
  `/openapi.json` are off. CSRF: the cookie is `SameSite=Lax` and `security_headers.SameOrigin` refuses
  (403) a POST / PUT / PATCH / DELETE whose `Sec-Fetch-Site` isn't `same-origin` or `none`, or, for a browser
  without it, whose `Origin` / `Referer` host isn't `Host` (or `X-Forwarded-Host`); a request with none of
  those headers passes. That covers another app on another port of the same host name, which is the same
  *site* to a browser. It is installed outside `BodyLimit`, so a cross-origin body is never read.
  `security_headers.SecurityHeaders` adds the CSP (inline script and
  style still allowed, since index.html's script and handlers are inline; no outside source at
  all: Chart.js 4.4.1 is vendored in `static/vendor/` from the npm package, checked against the
  registry's integrity hash), so add a library the same way rather than loading it from a CDN.
  In an inline handler, pass a string as `escHtml(JSON.stringify(value))`: escaping only `"`
  lets `&quot;` in the data close the string.
  Render user text with `escHtml` (or `textContent`) in every template string.
  Dates (`YYYY-MM-DD`, periods `YYYY-MM-01`) are local calendar days: parse them with index.html's
  `parseLocalDate` (never `new Date('2026-10-01')`, which is UTC midnight, the day before west of UTC),
  label a period with `periodLabel`, and turn a local `Date` back into a string with `localIso` (never
  `toISOString()`, the UTC date); `todayIso()` is the user's today.
  Request bodies are capped by `body_limit.BodyLimit` (12 MB; the bulk import plan
  `MAX_BATCH_FILES` x 10 MB): a declared oversize `Content-Length` is refused with 413 before
  it is read, a chunked body once it passes the limit. Those limits are for a signed-in browser:
  FastAPI reads (and spools) a body before `require_auth` runs, so without a valid session cookie
  (`signed_in`, Starlette's cookie parser + `auth.session_valid`) a body over `PUBLIC_LIMIT` (64 KB,
  room for the login form) is refused with 401 the same way, before it is read. List fields in request models carry
  `Field(max_length=...)` (bulk IDs 10,000, balance rows 1,000, `?accounts=` 500 ...): give any
  new list a cap too. Files are capped at 50,000 non-blank rows (`import_profile.MAX_ROWS`; an
  XLSX sheet stops after 200,000 scanned rows, blank or not): `read_sheets` raises `TooManyRows`
  at the first row over, `main.py` turns it into 413 wherever a file is read, and a bulk plan
  lists it as that file's error. It is not a `ValueError`, so layout checks don't swallow it.
  An `.xlsx` is checked from its zip directory before openpyxl opens it (`check_xlsx_limits`: 500
  parts, 150 MB unpacked, 30 MB of shared strings, which openpyxl reads whole before any row).
- **Backups** (`backup_service.py`, `routers/backup.py`): a backup id must match
  `BACKUP_ID_RE` (`flow_backup_YYYYMMDD_HHMMSS_<8 hex>`) and resolve inside `BACKUP_DIR`, or it is
  "not found"; other files in `/backups` aren't listed. Every zip read (upload, list, restore) is
  checked from its directory first (`check_zip_limits`: 50 members, 100 MB each, 200 MB in all,
  decompressed), and an upload keeps only the backup's own `*.json` members. A backup's
  `backup_info.json` is someone else's data once uploaded: `clean_info` rebuilds it from known fields
  (`created_at` text, `trigger` from `TRIGGERS`, `tables` as whole-number counts) wherever it is read,
  and `id` / `filename` are set after it, so it can't name another backup. Card-table rows go into
  `INSERT`s by column name, so `_check_card_columns` allows only the table's own columns (from the
  ORM metadata) before anything is deleted, and the statement quotes them. The card CSV export
  passes text cells through `routers/cards.spreadsheet_safe` (a leading `'` before = + - @).
  **The daily backup** (`backup_service.loop`, started by the lifespan beside the update check; a few
  minutes after startup, then every 15 minutes): `run_scheduled` takes one `scheduled` backup a day once
  it is `due` (at or after `settings_store.backup_hour()` UTC, judged by the newest scheduled backup's
  `created_at`, never the file's mtime; a restart after the hour catches up) while
  `backup_daily_flag()` is on, then `prune_backups(backup_keep())` removes the automatic backups
  (`PRUNABLE`: scheduled, auto-pre-import) beyond the newest N (0 keeps all); manual, uploaded and
  pre-restore backups are never removed. `due` / `prune` / `last_scheduled` are pure. One `_lock` serializes
  every write and prune. The settings (`backup_daily`, `backup_keep`, `backup_hour`: `SETTING_ENV` →
  `FLOW_BACKUP_*`) are `GET`/`PUT /api/backups/settings` (409 while `.env` sets one) and shown under
  Settings → Backups; a restore keeps them, with the session keys and `update_check`
  (`KEEP_ON_RESTORE`). **Encrypted copies** are download/upload only, the files on the server stay plain:
  `POST /api/backups/{id}/download` with `{passphrase}` (8..200 chars, a POST so it is never in a URL)
  returns `encrypt_backup`'s container as `<id>.flowbackup` (`MAGIC` FLOWBK1, 16-byte salt, scrypt
  n_log2/r/p, 12-byte nonce, AES-GCM over the zip with the header as AAD; key = `hashlib.scrypt`, N=2^17,
  r=8, p=1, `maxmem` 2×128·r·N since OpenSSL refuses exactly 128·r·N); the upload route takes an optional
  `passphrase` form field and `decrypt_backup`s a file that starts with the magic (422 on
  `WrongPassphrase`; header parameters outside n_log2 10..18, r 1..16, p 1..4 are refused before scrypt
  runs, since an uploaded header is someone else's data). The passphrase is never stored.
- **Balance sheet** (`accounts`, `balances`, `balance_sheet.py`, `routers/balances.py`): values are typed,
  pasted, or read from a balance file (Update balances → File: `POST /api/balances/import/read` reads a holdings or
  accounts export with any saved `balance` import format, `resolve.choose` across all of them since one file lists
  several accounts; `balance_sheet.assign_accounts` matches each row by last 4 (left to the preview), then a paste
  marker's label, then the exact account name; rows come back shaped like a paste reading, an `as_of` column sets
  the suggested period, and applied balances are stored with source `file`; the balance account dialog has the
  Import files section). A period is the 1st of a month; the current month is suggested (`current_period`) and the user
  picks any up to next month (`next_period`, the latest `period_problem` allows). "Today" is the user's date, not the server's UTC one: the browser sends `X-Flow-Today`
  (`balances.js` `balToday`), believed within a day of the server's date (`balance_sheet.client_today`). `balance_sheet.extract` reads a pasted account-summary page locally, as a first reading only
  (English wording; anchored on "ending in …1234", "x1234", "****1234", amount after it within a window; rewards/points, closed and no-balance accounts are kinds,
  not values). Accounts match by `Account.last4` (comma-separated, like cards); an unmatched row can be linked to
  an account in the preview and applying saves the number. `plan` is the preview: liabilities stored negative
  (banks show them positive), change alerts vs the previous value (`flow_settings` global % and $, overridable per
  account; 0 turns one off), overwrites, older periods, duplicates, numbers on two accounts. `/apply` checks again.
  Missing months carry the last value forward (`carry_forward`, `routers/data.balance_matrix`; `carried` in the
  timeseries, `carried_accounts` in the summary); archived accounts don't carry. Manage Accounts shows each account's
  newest balance (`/api/accounts` `latest`) and flags one with nothing for the user's month (`behind`,
  `balance_sheet.behind`, by `X-Flow-Today`); a cash account is behind when its newest bank row (else any row) is
  over `routers/register.REGISTER_BEHIND_DAYS` (40) old. The panel header counts them while collapsed. The AI fallback is opt-in per
  paste: `balance_sheet.outline` replaces amounts and account numbers with tokens and every word outside `VOCAB`
  with `~`; the model returns token pairs (`items_from_ai`), local readings stand (`merge_items`). Logged as
  `ai_request_log` purpose `balances`. Text pasted into the Assistant that looks like a summary opens this dialog
  instead of being sent (`balLooksLikeSummary`). Don't name a table `app_settings`: startup drops that legacy table.
  **Paste markers** (`balance_markers`, `balance_sheet.Marker`): where an account's balance sits on a paste,
  taught by example. `read_with_markers` runs before the anchored reader and `combine` lets a marker replace its
  reading (same number or label); the AI only fills what neither found (`merge_items`, keyed so number-less
  marker items don't collide). A marker is a label (`marker_label`: no amounts or long numbers), optional last 4
  (must appear near it) and a rule: `first_after` the label, or `on_line_with` a phrase below it. The preview's
  "Show Flow where an account is…" sends two clicks (name line, amount) to `POST /api/balances/markers/from-example`;
  `marker_from_example` keeps a marker only if it finds that same amount again, and a spot belongs to one account
  (re-teaching removes another account's marker for it; on reading, the newest marker for a spot wins).
  `/apply` with `remember` + `text` turns AI-read rows into markers (`marker_for_item`, source `ai`), so the next
  paste needs no AI. `/read` returns `lines` (`paste_lines`) for the teach view; the balance account editor lists
  an account's markers. Archived accounts' markers are not read. Markers never leave the server.
- **Cash flow** (`cash_flow.py`, `routers/cashflow.py`, `static/cashflow.js`; Dashboard → Cash Flow Analysis tab, this year by default): a Sankey
  of money in (register deposits by payee) → spending / debt payments / saving and investing / added to cash,
  balanced by "Drawn from cash" when more went out. Register rows are the ledger (manual + unmatched bank rows,
  after the cutoff). Each payee (`cash_flow.payee`: compact key of the sanitized label) gets a role in
  `payee_roles` (income, card_payment, transfer, spending + optional top-level category, debt / saving + optional
  balance-sheet account name, ignore). Precedence: the user's role > what the row matches (`detect_pairs`: a
  payment equal to a card statement's `payment` row within 6 days; an amount leaving one cash account and
  arriving in another within 4 days) > payee-name keywords > deposits default to income; anything else is
  "Not sorted yet" (never guessed). An outgoing payment whose name says "transfer" but matched no other
  cash account is NOT a transfer (`unmatched_transfer`): it stays "Not sorted yet" with a hint, because it
  usually went to an investment or savings account Flow doesn't track (dropping it hid real saving). Card payments are replaced by the period's card charges (charge date, by
  L1, refunds netted), so nothing counts twice; transfers and "ignore" are left out. Periods are per-month
  averages by default (`months_between`: exact for whole months). `cash_flow.tag` gives every transaction its
  bar (group, `detail`, `sub` = card subcategory or cash payee); `build` aggregates those, and the drill-in
  (`GET /api/cashflow/detail?group=`) returns them, so a list always adds up to its bar. In the UI a group bar
  zooms in (its bars, then one level further), any other bar lists its transactions under the diagram; card
  rows open the Cards edit dialog in place (`cfwEditCard`, `CD.onEdited` reloads the diagram), cash rows open
  Sort payees. Assistant: the `cash_flow` lookup and `propose_payee_role` (payee by its lookup label; withheld and
  person-to-person labels are refused). Payee names reach the Assistant only through `AskData.safe`.
- **Change log** (`change_log.py`, `models.ChangeLog` / table `change_log`, `routers/changes.py`, Settings → Change log in
  `static/changes.js`): every route that changes data calls `change_log.record(db, kind, action, summary, detail)`
  before its `db.commit()`, so the row is saved with the change and never without it (where a settings helper
  commits itself, `record` goes right before the call). Summaries are built in code from `clip`ped names and
  counts, never multi-line user text (the page still escapes them). `via` is who acted: `ui` (default),
  `assistant` (the browser's `askApply` sends `X-Flow-Via: assistant`; `read_via` is an app-wide dependency on
  `FastAPI(dependencies=...)` that MUST stay `async`, because a sync dependency runs in a threadpool with a copied
  context and its contextvar never reaches the route), `schedule` (the backup loop, `card_ai.run_categorization`)
  and `startup` (the rules refresh); background and startup writers pass `via` explicitly. Not logged: review
  ticks, insights, Assistant questions, sign-in, the auto-pre-import backup. The table is NOT in
  `backup_service.CARD_TABLES`: a restore keeps the log and records itself, and an uploaded backup can't inject
  rows. `change_log.prune` (newest `MAX_ROWS`, 20,000) runs from the backup loop's tick. Adding a new
  state-changing route: record there too (see Common tasks).
- **Needs attention** (`attention.py`, `routers/attention.py`, the `#attention` panel in `index.html` under the metric
  cards): `GET /api/attention` (`X-Flow-Today`) gathers every signal from what the other routers already compute,
  so each count matches the tab its row opens, and `attention.build` (pure) orders them: AI paused
  (`card_ai.key_problem`), bank rows to reconcile and auto-matches to confirm per cash account
  (`routers/register.account_signals`, one grouped query shared with `/api/register/accounts`, the reconcile
  view's rules), balance accounts behind (`routers/data.list_accounts`, not ignored/archived), cash accounts
  behind, payees needing a role (`routers/cashflow.list_payees`, this year), card charges to review
  (`routers/cards.review_result` over one `_load` of this year), subscriptions overdue / price change / on two
  cards (`loaded.recurring_items`: `status`, the "price change" / "up " strings in `change_detected`,
  "duplicate" in `notes`), formats `needs_preview`, backups (`backup_service.list_backups` newest `created_at`
  over `BACKUP_OLD_DAYS`, `backup_daily_flag`), the update (`update_check.status`). Each item's `nav` is a shape
  `askNavigate` (`static/ask.js`) opens: a section (`{section, tab, payees, accounts}`, `{section:'cashflow',
  account, reconcile}`, `{section:'cards', tab}`) or a dialog (`{action:'settings', section}`, `balances`,
  `import`, `formats`); `test_attention.known_nav` lists them. The panel is fetched after `loadAll` draws the
  page and when the Dashboard is shown again; collapsed state in `localStorage`. Text is escaped by the page.
  **Snoozing**: every item has a stable `key` (`kind`, or `kind:<id>` per account, `update:<version>`);
  `POST /api/attention/snooze {key, for: day|week|month|null}` stores `{until, count}` under the item's key in
  one `flow_settings` JSON row (`attention.SNOOZE_KEY`, read back through `clean_snoozes` since it travels in
  backups), `DELETE /api/attention/snooze/{key}` lifts it. `split_snoozed` hides an item while its date hasn't
  passed (no date = ignore until it changes) AND its count hasn't grown; `prune_snoozes` drops expired ones on
  every write. The reply is `{items, hidden}`; the panel's "Later ▾" menu and the "N hidden" footer are in `index.html`.
- **CSV exports** (`exports.py`): every export goes through `exports.csv_response(header, rows, name)`, which
  writes floats with two decimals, ints as they are, booleans as yes/blank, None blank and text through
  `spreadsheet_safe` (a leading `'` before = + - @, so an amount must be a number, never a string: a "-12.50"
  string would be quoted); the filename comes from `download_disposition` (any text). The card transactions
  (`routers/cards` `/export.csv`), the register (`/api/register/{id}/export.csv`: the rows
  `_compute_running_balance` returns, so the balance column is the page's, filters not applied), the balance
  sheet (`/api/data/export.csv?layout=long|wide`: `wide` is `balance_matrix` with carried values filled and
  named in a `carried` column, totals as `summarize`, ignored accounts shown but left out of them; cash
  accounts are not in it) and the year-end summary (`/api/cards/export-year.csv?year=`:
  `card_analytics.year_summary` over every card's `SPEND_TYPES` rows and `cash_flow.tag`'s cash `spend`
  entries, `tax_note` by original built-in names through `Row.b2`: giving, taxes, medical, insurance,
  tuition; Cards → Settings → Export has the year picker, `cdYears` from the cards' coverage).
- **Clear filters**: every filter bar has a "✕ Clear filters" button (`.clear-filters`) shown only while a
  filter differs from its default (Cards bar, Cards transactions, Settings merchants, the register, Cash Flow,
  Sort payees). Add one to any new filter bar.
- **Import formats** (`import_profile.py`, `import_profile_resolve.py`, `import_infer.py`, tables
  `import_profiles` + `import_profile_links`, `routers/import_profiles.py`). No file layout is built into
  the code: every file is read with a saved format (a `Spec`, stored as JSON) linked to the account it is
  imported into (link kinds `cash`, `card`, `balance`). Columns are found by header text (drafts match
  `import_infer.VOCAB` on `vocab_key`, which drops a currency note: "Deposits ($)", "Amount (CAD)"); a headerless
  format reads by position and is chosen by how cleanly it reads (`fits`). The import routes resolve the
  account's linked formats (`resolve_for_account`): no match is 422 `{code: "no_profile", draft, matches}`
  (`import_infer.infer` drafts one, in the account's kind; `matches` are saved formats of that kind that
  read it); a changed header is 409 `{code: "profile_drift", diff, draft}` (the old format re-pointed by
  name, `repoint`); a sign the file contradicts (`verify_sign`: running balance, account history, card
  payment rows) is 422 `sign_reversed`; unreadable rows are 422 `row_errors` unless `allow_errors=true`.
  Card payment rows count as evidence only when they run the opposite way from the other rows: a merchant
  merely *named* like a payment ("ONLINE PAYMENT SOLUTIONS") runs with the charges and settles nothing, so
  the editor's sign question appears instead. OFX / QFX / QIF files are refused by name
  (`import_infer.looks_like_ofx`). A column the header names as an amount may hold whole numbers
  (`NAMED_MONEY_ROLES`, "2,500", "-950"; dot groups such as "1.234" stay out); elsewhere a whole number is
  a reference. A draft for a card account never assigns `check_number` (not a card role). All amounts
  positive with a two-valued text column in another language gets a note pointing at the
  amount-plus-direction layout, since the sign question's one answer would flip deposits too.
  A file resembling a header format (half its columns or more) is drift, never read by a headerless
  format by position (`Evaluation.matches`). Amount cells with words or dates are not amounts. A fresh install has
  no formats. Formats travel
  in backups (kept when restoring an older backup without them) and export as JSON (no data).
  UI (`static/importprofiles.js`): `ipOpenEditor` (the file's first rows from `POST /api/import-profiles/grid`
  with a role picker per column and click-a-row-for-header, settings beside them, live `preview` with the sign
  check and a "did money leave the account?" question when nothing confirms it, for every amount layout
  (answering flips the sign, swaps the money-out / money-in columns, or swaps the direction words), and a
  month-first or day-first question when `preview` reports `date_order_ambiguous`
  (`import_profile.date_order_ambiguous`: both orders read every row, differently); a new format can't be
  saved until both are answered; "Use for" account links);
  `ipOpenList` (all formats: edit, duplicate, export, delete, import JSON); `ipAccountSection` (the cash and
  balance dialogs' "Import files": linked formats, set up from a sample file, link, import a format file); and
  `ipProblem`, which turns the import endpoints' structured errors into the matching editor. The import dialog
  shows "Format: <name>" with Edit / Change (`profile_id` picks another linked format) and keeps Import
  disabled while rows can't be read until "Import the rest" is ticked; bulk-import rows with no format (or a
  changed one) offer "Set up format…" / "Update format…" and plan again after saving. An import check counts
  as the preview a format imported from JSON needs (`needs_preview` cleared by `mark_previewed`).
- **Account dialogs** (cash `#manage-acct-modal` in index.html, new and edit; card `#cd-acct-modal` in
  cards.js; balance `#bal-acct-modal`) share one frame (`static/accounts.css`, `.acct-dlg`) and one order:
  Name, Institution, Notes (not for balance accounts), the kind's section, Import files, Options; Delete left,
  Cancel and Save right. Keep new account dialogs on this frame.
- Bank imports are read by the account's format into `Transaction` rows (`row_source` `bank`; `manual`
  formats import ledger entries) and reconciled against manually entered rows via `reconciler.py` /
  `import_dedup.py`.
  A bank file mostly already in another cash account (bank-imported rows only, same rule as
  cards: `import_dedup.best_other_account`, signed amounts so transfers between accounts don't
  count) is flagged by the import check and refused with 409 unless `allow_other_account=true`.
  Ledger (spreadsheet) imports are not checked.
- **Credit cards** live in their own tables (`card_accounts`, `card_imports`,
  `card_transactions`, `merchant_categories`, `card_recurring_verdicts`, `card_manual_recurring`,
  `ai_request_log`, `card_insight_reports`) and router (`routers/cards.py`). Nothing outside that router and the Assistant reads
  them, which is what keeps cards off the dashboard and out of net worth (a card's balance can be added as a balance-sheet liability). Card amounts use the
  register's sign (charges negative); analytics flip to a spend-positive convention.
  `/api/import/detect` returns `type: "card"` when only card formats read the file, `register` when a
  cash format does (a bank-layout file can still go to a card: the account picked decides), `changed`
  for a saved format's export with a changed header and `new` (with a draft) when no format reads it.
  Because many files carry no card number, `card_import.other_card_match` (on the shared
  `import_dedup.best_other_account`) compares each upload
  with every other card (date, amount, first 12 description characters, one-to-one, only within
  the dates that card covers). If 60%+ (and at least 3, or all of a tiny file) already exist on
  another card and fewer on the chosen one, the import check warns and the import returns 409
  unless `allow_other_card=true`.
- **Bulk import** (`import_batch.plan`, `POST /api/import/batch/plan`, read-only): the import dialog
  takes several files; the plan pre-assigns a file only when the evidence points one way (rows overlap
  exactly one account by `import_dedup.is_likely_match` with at least 3 matches, or a card number in the
  file is listed on exactly one card, with no overlap contradicting it, or the format that reads the
  file is linked to exactly one account: `BatchFile.format_accounts`) and otherwise leaves it for
  the user with the reason. A file no saved format reads (or whose header changed) is listed with an
  error: it is imported on its own first, where its format is set up. Assigned files (and the user's picks, sent back as `pinned`) count as history
  for the rest of the batch. `CardAccount.last4` holds every card number on the account, comma-separated
  (`card_import.card_numbers`); each import adds the file's numbers (`remember_numbers`), so a new number on
  the matched card is no objection; only a number listed on another card is. All of them are redacted. A card file with a guessed sign and no overlap is skipped (single-file import
  only). The browser then sends files oldest first to the normal import endpoints; a file the user picked
  by hand goes with `allow_other_*`. `card_ai.run_categorization` runs once more when asked mid-run, so
  merchants from later files in a batch are not left waiting.
- **AI privacy contract.** Categorization sends only `card_privacy.sanitize` labels (gated by
  `is_safe_label`), the issuer category label and the processor name, keyed by a throwaway id.
  Insights send only `card_analytics.insights_payload` (aggregates, cards aliased "Card A" by
  `card_analytics.card_aliases`; each report stores its alias map in `card_aliases_json`). Real names
  are put back only for display, by `card_analytics.unalias` (handles "Cards C and D" too).
  Every call is logged in `ai_request_log`: the data sent (`payload_json`), the whole request body
  (`request_json`: system prompt, tools, messages, settings) and the whole response (`response_json`),
  shown in the AI section of Settings (top bar → ⚙ Settings → AI, `static/ai.js`, `/api/ai/log`), beside the model each feature uses
  (`card_ai.FEATURES`: add a new feature there). Any new AI call must record both (`card_ai.record_request` /
  `record_response`, as `card_ai._call` and `routers/ask.make_logger` do) and be covered by a test
  asserting nothing identifying is in it. Merchant
  answers are cached in `merchant_categories` so each merchant is sent once; categorization runs
  as a FastAPI background task after import (one run at a time, guarded by a lock).
- **Merchant keys are compact** (`card_privacy.compact_key`: uppercase letters and digits only),
  so `CIRCLE K`, `CIRCLEK`, `CIRCLE_K` and `CIRCLE-K` are one merchant; labels keep spaces.
  `card_classifier.MerchantIndex` also serves a cached merchant to a truncated or extended
  spelling (shared prefix of 8+ characters covering 60%+ of the longer key), at import and
  again before each AI run (`card_ai.resolve_from_cache`), so those never cost a call.
  Merchant-scope category edits still use exact key equality. `card_rules.COMMON_MERCHANT_RULES`
  (Flow's ~400 common US brands, checked before the general MERCHANT_RULES) categorizes the rest locally;
  add brands there with an explicit canonical name.
- **Brands in code are peers, never one person's.** No rule, test, example or prompt names a brand or
  place that only fits one user's life: no city's venues, airport, newspaper, DMV office or local store.
  A brand gets special treatment (its own category detail, extra rules, a Review flag, a perk name) only
  as one row of a list that treats its peers the same way: `US_AIRLINES` (each US airline its own
  Airfare detail), `AIRLINE_EXTRAS` (lounges, vacation packages, inflight purchases), `HOTEL_GROUPS` (each
  large hotel group its own Lodging detail), direct-sale carmakers and fitness-device makers, the perk
  wording of every large issuer (`BENEFIT_CREDIT_RE`), `SPONSOR_VENUES` (sponsor-named arenas nationwide,
  checked first so a ticket isn't filed under its sponsor) and `card_analytics.CO_BRANDS`. Regional chains
  go in `COMMON_MERCHANT_RULES` beside other regions' chains. Tests use fictional names or a mix of
  national brands. When rules change what a stored charge would get, `card_rules_refresh` (a named
  migration) re-files rule-categorized charges, and keeps the category of a merchant no rule recognizes
  any more as a remembered choice (`merchant_categories`, source `user`).
- **Merchant edits** (`routers/cards.py`): a merchant is every purchase/refund shown under one name
  plus rows sharing those rows' statement spellings (`_merchant_rows`). `POST /transactions/{id}/edit`
  (the dialog) and `POST /merchant/edit` (the merchant page) change name, parent brand and category
  together; merchant-wide edits remember the answer for every spelling and keep rows the user changed
  on their own (`category_source = 'user'` or `name_set_by_user`) unless `overwrite_user`.
- **Amount rules** (`card_amount_rules.py`, `card_amount_rules` table): non-overlapping amount ranges
  (lower bound included, upper excluded) scoped to one merchant or to a category (`scope_l1`, optional
  `scope_l2`: every merchant in it), setting a category (`category_source = 'amount_rule'`). A
  merchant's own rules come first; category rules match the category a charge had before any rule
  (`pre_rule_category`, recorded when a rule first changes it). `card_amount_rules.apply` re-derives
  them for stored charges after a rule change, an import, an AI run and a merchant edit (rules follow a
  renamed merchant); a charge a rule no longer covers gets its recorded category back;
  `category_source = 'user'` rows are never touched.
- **Recurring detection** (`card_recurring.detect`) judges each (card, merchant) series, with three
  Flow rules on top: a series that moves cards is joined (`_link_moves`, even after a single charge
  on the old card); user-defined series (`card_manual_recurring`, `ManualSeries`) claim matching
  charges first (merchant name or parent brand, optional card and amount range); subscription-like
  subcategories (`SUBSCRIPTION_L2`: memberships, streaming, software ...) need only two charges; and each series is active / overdue / ended (or "expected" for a manual one with no
  charges yet). Ended and expected series are left out of totals.
- **Review tab** (`card_analytics.review`): each item carries reason codes (`large`, `repeat`, `foreign` ...).
  Ticking it off (`POST /api/cards/review`) stores `reviewed_at` and the codes it had (`reviewed_reasons`)
  on the transaction; it stays checked off until it gains a code it did not have then (a later repeat).
  Charges in a subscription series (`recurring_kind == 'subscription'`) are left out.
  Co-brand codes (`<brand>_card`, from `card_analytics.CO_BRANDS`: each US airline's and hotel group's
  co-brand card, recognized by brand or loyalty-program words such as SkyMiles, AAdvantage or Honors) flag
  a flight or stay not charged on that brand's card, only for brands the user holds a card for
  (`co_brands_held` over `card_names`, every card's name, passed by the router).
- **The old "Subscriptions / Telecom" L1 is split** into
  Bills & Utilities (phone, internet, TV, plus Home's utilities) and Digital & Media (streaming,
  software, cloud, news); memberships sit under Shopping or Services. Billing cadence is the
  Subscriptions tab's job, whatever the category. `card_taxonomy.split_legacy_category` drives the
  one-off `card_split_subscriptions_telecom` migration of stored rows.
- **Your own categories** (`custom_categories`, `card_taxonomy.Custom`): subcategories (and details) under
  the fixed L1s, loaded into `card_taxonomy` at startup and after each change (`refresh`). Built-in name
  lists still drive built-in behaviour; a custom subcategory carries its own: `fixedness`
  (`card_analytics._fixedness`), `role` (bill / subscription / habit in `card_recurring`), `rewards`
  (`card_rules.reward_key`) and `ai_visible` (false: left out of the categorization prompt and masked
  as "(private subcategory)" in Insights and Assistant lookups). Rename and delete (`/api/cards/categories`)
  move transactions, the merchant cache, amount rules and `pre_rule_category` with them (`_retag`).
- **Renaming and moving categories** (`routers/cards._rename`, `POST /api/cards/categories/rename` and
  `/move`). Any subcategory or detail under a choosable L1 can be renamed, built-in or custom; L1s are fixed.
  A built-in rename is a `category_renames` row keyed by the ORIGINAL names (l3 "" = the subcategory).
  Stored rows, the merchant cache and rules carry the new (current) name; the rules' answers are renamed as
  they are assigned (`card_taxonomy.current`, in `card_classifier.classify_transaction`, the fee path and
  `card_amount_rules.base_lookup`). Code that keys behaviour on built-in names compares the original name:
  `card_taxonomy.base` / `base_l2`, `Row.b2` / `Row.b3` in analytics and the trim list, `reward_key`,
  `card_recurring`'s BILL/HABIT/SUBSCRIPTION lists, `_fixedness`. **Any new comparison against a built-in
  category name must go through `base` (or `Row.b2`/`b3`), and any new place that assigns a rule's category
  must go through `current`.** An old name stops being valid (`is_builtin` checks the current name).
  Renaming back to the original deletes the row. Move files everything under one subcategory/detail
  elsewhere, remembers each merchant (`_remember`, source `user`) so imports follow, and leaves the source
  in the list. Cards → Settings → Rename a category lists built-in renames with Restore original.
- **The Assistant** (UI name; code name "ask": `ask.py`, `ask_data.py`, `ask_proposals.py`,
  `routers/ask.py`, `static/ask.js`) is a chat panel answered only from Flow's card and cash-register data.
  Tool strictness: the lookups are `strict: true`; the `propose_*` tools are not, because every strict
  schema in a request compiles into one grammar with a size limit (16 strict tools hit "compiled grammar is
  too large"). `ask_proposals.checked_args` checks proposal input against its schema in code instead (enums
  matched case-insensitively). `test_ask.test_strict_tools_stay_inside_the_grammar_budget` caps strict
  tools at 10 and 3,000 schema characters: make a new tool non-strict rather than raise the cap.
  It cannot change anything: its `propose_*` tools (`ask_proposals.py`) check a suggestion against the
  data (category exists or is proposed earlier in the answer, a detail exists in the list, is already on
  charges or was proposed with `propose_new_detail`, merchant exists, no overlapping amount rule) and
  return a card with the exact request for Flow's normal endpoint; the browser sends it when
  the user presses Apply, and that endpoint validates again. Code owns the facts: Claude gets an index (coverage,
  category totals by month, category-list counts, the top of the trim list, the latest month's net worth
  (`net_worth_index`), the page on screen) and nine strict lookups (including `categories`: the list with
  built-in/yours flags, original names of renamed built-ins and charge counts; `spending_breakdown` groups by
  detail too; `net_worth`: the balance sheet by month as the Dashboard chart draws it, `net_worth_series` lining
  the balance periods up with the cash register's month-end balances from the rows already loaded and counting
  cash as assets, ignored accounts listed but left out, with each account's value and change, or one account's
  history) that return summaries; only `find_card_transactions` / `find_cash_transactions` return single rows
  (25 at most). Cards are "Card A", cash accounts "Account A", balance-sheet accounts "Asset A" / "Liability A"
  (`load_balances` over `routers/data.balance_matrix`; institutions are never loaded; `realias` converts real
  names in returned history; `unalias` restores them in the answer); names pass `is_safe_label`, transfers to people keep
  only the service ("Zelle transfer"). Advice comes from `ask_data.trim_items`, which code ranks (easy
  first, then monthly cost). One question: a low-effort topic gate (`{"relevant": bool}`; a failure
  counts as relevant and the `OFF_TOPIC` reply is the backstop), then up to 5 streamed rounds (4
  lookups each, 4,000 output tokens each, 120k input / 12k output in total; the last round has
  `tool_choice: none`). Proposals don't count as lookups: 12 per round, 20 per answer. When the next
  round will have tools off, the tool results end with `FINAL_ROUND_NOTE` so the model writes its answer
  instead of narrating calls it can't make. A round whose text degenerates into repetition
  (`looks_stuck`, checked every 400 streamed characters) is stopped, trimmed (`trim_stuck`) and ends with
  `STUCK_NOTE`; proposals made so far are kept. Numbers in the answer not found in what was sent are listed as unverified. The
  browser keeps the conversation in `sessionStorage`; the server keeps only `ask_usage` (questions per
  day, shown as this month's and last month's totals; no limit; failed questions are not counted).
  Each API call is one `ai_request_log` row (`ask_topic`, `ask` per round).
  **Attached files** (`ask_files.py`, `POST /api/ask/attach`, the 📎 / drag-and-drop in `static/ask.js`): a statement
  file is read on the server into `import_infer.outline` (cleaned headers, column shapes) and a draft summary
  (roles by column number, amount layout, sign, date order, decimal mark: enums and integers only); the file and
  the full draft (header text, first rows) stay in the page's memory, never in `sessionStorage`. Each question
  sends `attachment: {outline, summary}`, which `checked_attachment` rebuilds from known keys, enums and small
  integers and cleans every header again (`safe_header`), so nothing free-form from a browser reaches the model;
  it is appended to the question as `ATTACHED FILE` and the topic gate is skipped. `propose_import_profile`
  (non-strict) names columns by number and is checked against the outline and `Spec.validate`; its card has
  `action: "open_import_editor"` instead of `apply`, and the browser lays it over the local draft and opens
  `ipOpenEditor` with the file (Apply all skips it). The editor's "Ask the Assistant" hands its sample file over.
- **Rejected keys pause AI.** Each `ai_request_log` row records an `error_kind` and a short hash of
  the key used (`key_fingerprint`, never the key). If the newest request failed for `auth` (401/403:
  expired, revoked) or `billing` reasons and the key is unchanged, `card_ai.key_problem` reports it:
  imports stop queueing AI runs, Insights and the Assistant refuse (it does not count the question), and the
  Cards UI shows a persistent "AI paused" banner. Changing the key or any successful request (the AI dialog's
  "Test the key", `card_ai.test_key`, purpose `key_test`, included) clears it; the manual "Re-run AI"
  action always tries once.
- **Settings and secrets live in the database** (`settings_store.py`), not in `.env`: the AI models,
  on/off switch and redact words as `flow_settings` rows (`SETTING_ENV`; they travel in backups), the
  password hash and the Anthropic API key (AES-GCM under a key derived from `SECRET_KEY`, so a database
  dump alone can't read it) as `flow_secrets` rows (`AppSecret`; never in a backup: `backup_service`
  doesn't list the table). `SECRET_KEY` stays in `.env` as the root key. Precedence: a variable set in
  `.env` wins while it is there and is copied into the store at every start (`import_from_env`, in the
  lifespan), so removing it afterwards changes nothing; the AI dialog shows such a value as set by `.env`
  and `PUT /api/ai/settings`, `PUT /api/ai/key` and the password change refuse (409) to change it
  underneath. An install with no password anywhere refuses to start (`auth.require_password`). A key
  saved under a SECRET_KEY that later changed reads as `api_key_unreadable` and the dialog asks for it
  again. One in-memory copy (`settings_store.load` at startup and after a backup restore), one worker.
  The AI dialog (`static/ai.js`, `routers/ai.py`) has the key (save, test, remove), the switch, the three
  models (datalist of known names, validated like `setup.sh` did) and the redact words.
- **What the AI may see: one switch per data area** (`settings_store.AI_AREAS`: `ai_cards`, `ai_cash`,
  `ai_balances` → `FLOW_AI_CARDS` / `FLOW_AI_CASH` / `FLOW_AI_BALANCES`, default on, env-lockable like the rest,
  under the global switch in Settings → AI, `static/ai.js` "What the AI may see"). `card_ai.AREAS` / `AREA_LABELS`,
  `area_enabled(area)` (= `enabled()` and the area's flag) and `area_off_reason(area)` (the global reason first,
  then the area's) are the only gates: cards = `categorize_pending`, `generate_insights`, the import preview
  and queue, `/api/cards/categorize` and `/api/cards/ai/status` (so the Cards banner reads "AI for card spending
  is switched off"); balances = `/api/balances/read-ai` and the `ai` block of `/api/balances/settings`; cash has
  no feature outside the Assistant. **Any new AI feature gates on its area, not on `enabled()`.** The Assistant:
  `routers/ask._run` passes `card_ai.areas_enabled()` to `ask_data.load(db, areas)`, which does not read an area
  that is off (so nothing of it can reach the index or a lookup), to `build_index` (the area's blocks left out,
  `"off": [...]` listed) and to `ask.answer`, which offers only `ask_data.tools_for` + `ask_proposals.tools_for`
  (`TOOL_AREA` in each module; `propose_import_profile` has no area) and answers a call to a tool not offered
  with an error result. `ask.rules(areas)` appends the Access paragraph: a question needing an off area is
  answered with exactly `NEEDS_ACCESS <area>`, which `answer` turns into `{"type": "declined", "kind": "access",
  "area", "message"}` (counted like an off-topic reply); `declined_message(areas)` names only the areas on.
  `GET /api/ask/status` carries `areas`, and `POST /api/ask` is 409 when all three are off. The panel
  (`static/ask.js` `askRenderBanner`, `#ask-banner`): the Assistant button stays visible with AI off; the banner
  says "AI is off: <reason>", "every area is switched off" or "Not available to the Assistant: …" with an
  "Open Settings → AI" button (`askNavigate({action:'settings', section:'ai'})`), the input is disabled while
  nothing can be asked (`askCanAsk`), suggestions follow the areas, and an access `declined` item carries the
  same button. `aiRefreshCards` refreshes the panel after any change in Settings → AI.
- **Update check** (`update_check.py`, `routers/updates.py`, Settings → Updates in `index.html` / `static/settings.js`): a background
  task started by the lifespan (`update_check.loop`; two minutes after startup, so the deploy's health check
  comes first, then daily) does one GET to GitHub's latest-release endpoint for `update_check.REPO`, server-side
  (the browser never calls out; the CSP stays closed) and with nothing of the user's. The reply is external data:
  `parse_release` keeps only a `vMAJOR.MINOR.PATCH` tag, an `html_url` under the repository's releases page and
  the date, at most `MAX_REPLY` bytes, and nothing from it is rendered as HTML. `is_newer` compares semver tuples
  with `APP_VERSION`; a dev checkout ahead of the latest release is no update. The last result and the release
  the user hid are `flow_settings` rows (`STATE_KEY`, `DISMISSED_KEY`, loaded by `update_check.load` at startup),
  in memory between checks. `GET /api/version` returns `{version, update: status()}`; `POST /check` runs one now
  whatever the switch says (a failure is shown only there: the daily check logs and tries tomorrow); `POST
  /dismiss` hides a release until a newer one; `PUT /settings` is the daily switch (`flow_settings`
  `update_check`, `SETTING_ENV` → `FLOW_UPDATE_CHECK`, refused with 409 while `.env` sets it). The Settings button shows a
  dot (fetched once after sign-in) and Settings → Updates the version line with the release link, hide, and the switch.
  Flow never installs an update: updating stays `git pull && ./deploy.sh`. README's "What leaves your machine"
  describes the call; keep it in step.

## Key conventions

- **Migrations live in `main.py`'s lifespan handler, not in `deploy.sh`.** The app runs
  its own schema setup at startup, guarded by a `schema_migrations` table so each named
  migration applies once. The deploy's health check is what proves a migration
  succeeded, since a failed migration crashes the app and the health check then fails.
- **`setup.sh` never touches git or containers.** It only writes `.env`, then execs into
  `deploy.sh`. This keeps a setup-triggered deploy and a deploy.sh-triggered
  deploy identical except for whether credentials were prompted for first — duplicating
  the pipeline in two places is exactly the drift this avoids.
- **`docker compose build` and `docker compose up` are separate steps in `deploy.sh`.**
  `up --build` does not accept `--no-cache`, so folding the two together silently breaks
  the no-cache rebuild path the moment a dependency file changes.

## Common tasks

### Add a new backend endpoint

A route that changes data also records the change: `change_log.record(db, "<kind>", "<action>", "<summary built from
clipped names and counts>", {<small detail>})` just before its `db.commit()`, so the row is saved with the change.

1. Add the route to the relevant router in `backend/routers/`, or create a new router and
   include it in `main.py`.
2. Add/adjust SQLAlchemy models in `models.py` if new data is stored.
3. If a schema change is needed, add an idempotent `ALTER TABLE ... IF NOT EXISTS` (or a
   named entry guarded by `schema_migrations`) to the `lifespan` handler in `main.py`.

### Add a new environment variable

A value the user should be able to change in the app (an AI setting, a secret) is not an
environment variable: add a key to `settings_store` (`SETTING_ENV` or `SECRET_ENV`), a field in
the AI dialog, and leave `.env` for host state (`APP_PORT`, the database password, `SECRET_KEY`,
the proxy settings). For host state:

1. Read it in the relevant backend module with `os.environ[...]` (required) or
   `os.environ.get(..., default)` (optional).
2. Add it to `.env.example` with a placeholder value.
3. Add it to the Environment variables table below.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_PASSWORD` | *(required, generated by `setup.sh`)* | PostgreSQL password, shared between `db` and `app` containers; the app adds it to `DATABASE_URL` itself (`database.database_url`), so any character works |
| `FLOW_PASSWORD` | *(first start only; set by `setup.sh`)* | Dashboard password for the first start; afterwards kept as a hash in `flow_secrets`, and a value here resets it while it stays (`settings_store.import_from_env`) |
| `SECRET_KEY` | *(required, generated by `setup.sh`)* | Secret used to sign session cookies |
| `APP_PORT` | `8000` | Host port the app is exposed on |
| `DATABASE_URL` | *(set by `docker-compose.yml`, not `.env`)* | SQLAlchemy connection string without the password (`POSTGRES_PASSWORD` is passed beside it, since `@ / : # ? %` would break a URL); not something a user sets directly |
| `COOKIE_SECURE` | `false` | When truthy (`1`/`true`/`yes`), marks the session cookie `Secure`; enable when serving over HTTPS behind a reverse proxy |
| `FLOW_TRUSTED_PROXIES` | *(empty)* | Comma-separated IPs or CIDR ranges of reverse proxies whose `X-Forwarded-For` is believed for the login throttle (`auth.client_ip`); a malformed entry stops startup |
| `FLOW_RECURRING_SUBSTREAMS` | `1` (enabled) | Set to `0`/`false`/`no`/`off` to disable the recurring-transaction substream detection algorithm |
| `ANTHROPIC_API_KEY` | *(unset: the key stored in Flow, or AI off)* | The Anthropic API key; normally entered under Settings → AI and stored encrypted (`flow_secrets`). Set here it is copied in and wins while it stays |
| `FLOW_AI_MODEL` | `claude-sonnet-5-5` | Claude model for every AI feature; normally set in the AI dialog (`flow_settings` `ai_model`); the default lives only in `card_ai.DEFAULT_MODEL` |
| `FLOW_AI_MODEL_CATEGORIZE` | *(empty: the AI dialog's value, else `FLOW_AI_MODEL`)* | Model for merchant categorization only (`card_ai.model_name(TASK_CATEGORIZE)`; `ai_model_categorize`) |
| `FLOW_AI_MODEL_ASK` | *(empty: the AI dialog's value, else `FLOW_AI_MODEL`)* | Model for the Assistant's topic gate and answer rounds (`TASK_ASK`; `ai_model_ask`) |
| `FLOW_AI_ENABLED` | `1` (enabled) | Set to `0`/`false`/`no`/`off` to turn AI calls off even with a key set (the AI dialog's switch, `ai_enabled`) |
| `FLOW_AI_CARDS` | `1` (enabled) | `0` keeps card spending from the AI: no categorization, Insights, or Assistant card lookups and proposals (`ai_cards`) |
| `FLOW_AI_CASH` | `1` (enabled) | `0` keeps the cash register from the AI: no Assistant cash, cash-flow or payee lookups and proposals (`ai_cash`) |
| `FLOW_AI_BALANCES` | `1` (enabled) | `0` keeps the balance sheet from the AI: no AI reading of pasted balances, no Assistant net worth lookup (`ai_balances`) |
| `FLOW_AI_REDACT` | *(empty)* | Comma-separated extra words stripped from merchant names before any AI call (the AI dialog's field, `ai_redact`) |
| `FLOW_UPDATE_CHECK` | `1` (enabled) | Set to `0`/`false`/`no`/`off` to turn the daily check for a newer release off (Settings → Updates has the same switch, `update_check`) |
| `FLOW_BACKUP_DAILY` | `1` (enabled) | Set to `0`/`false`/`no`/`off` to turn the daily backup off (Settings → Backups, `backup_daily`) |
| `FLOW_BACKUP_KEEP` | `30` | Automatic backups kept, newest first; `0` keeps all (`backup_keep`, clamped 0..3650) |
| `FLOW_BACKUP_HOUR` | `3` | UTC hour the daily backup is taken at or after (`backup_hour`, 0..23) |

`.env.example` lists the four user-facing variables (`POSTGRES_PASSWORD`, `FLOW_PASSWORD`,
`SECRET_KEY` blank, so a copied file can't deploy as is; `APP_PORT`), plus the optional ones commented out.
`setup.sh` writes the four (the password is required before the first start; once a database volume
exists it is optional, offered as a reset, since Flow keeps the password itself), then offers the
Anthropic API key (read hidden; `off` removes the line). The models, switches (global and per area) and
redact words are the AI dialog's; `setup.sh` no longer asks for them. In `docker-compose.yml` every Flow-kept setting is passed as
`${VAR:-}` with a blank default: a non-blank default would reach the app as a set variable and lock the
setting in the dialog (409). On a re-run with a database volume, `setup.sh` offers to
remove any of `FLOW_PASSWORD`, `ANTHROPIC_API_KEY`, `FLOW_AI_*`, `FLOW_UPDATE_CHECK` and `FLOW_BACKUP_*` still in `.env` that this run didn't
just set (`_env_unset`), since Flow has copied them in. `deploy.sh` requires `POSTGRES_PASSWORD` and
`SECRET_KEY`, requires `FLOW_PASSWORD` only before the first start (no `pgdata` volume by Compose's
labels), and notes the values Flow keeps that are still set. `DATABASE_URL`, `COOKIE_SECURE`,
`FLOW_TRUSTED_PROXIES` and `FLOW_RECURRING_SUBSTREAMS` are derived automatically or optional with safe defaults,
so the wizard doesn't ask for them.

**How `.env` is written and read.** Compose reads an unquoted `.env` value with `${VAR}` substitution and
` #` comments (`pa$word #1` would reach the app as `pa`), so `setup.sh`'s `_env_set` writes every value
double-quoted with `\`, `"` and `$` escaped (`_env_quote`), the one form Compose reads back exactly; passwords
may hold any character but a newline, spaces at the ends included (`IFS= read`). `_env_set` rewrites the file
through `mktemp` (mode 600) and `setup.sh` also `chmod 600`s an existing `.env`; `deploy.sh` changes no files,
so it only warns about an `.env` other users can read. `_env_get` (identical in both scripts; the test checks)
reads double-quoted, single-quoted and the older unquoted values. `tests/test_deploy_script.py` round-trips
awkward passwords through `_env_set` / `_env_get` and, where `docker compose` is installed, through Compose.

## Deployment

- **Migrations** — run at application startup in the FastAPI `lifespan` handler
  (`backend/main.py`), not as a `deploy.sh` step. The `/api/health` check after `up -d`
  is what proves they succeeded: a failed migration crashes the app, `restart:
  unless-stopped` loops it, and the health check times out and fails the deploy.
  This is only safe because the app runs a single uvicorn worker (no `--workers` flag
  in `backend/Dockerfile`) — startup migrations are not safe to run concurrently, so
  adding worker or replica concurrency requires adding a lock around this path first.
- **Host grants** — `docker-compose.yml` mounts `./frontend:/app/frontend:ro` into the
  `app` container so the backend can serve the static frontend directly. This is the
  app's own source tree, read-only, and not a privilege grant beyond the container's own
  code. `deploy.sh` doesn't create it: it is tracked source, created by `git checkout` as
  the deploying user (never by the Docker daemon, so never root-owned), and the container
  can't change its ownership through a read-only mount.
- **Container user** — the app runs as the unprivileged `flow` user (uid 10001,
  `backend/Dockerfile`); `/backups` (the `backupdata` volume) is its only writable path. The
  one-shot `backups-owner` service (`postgres:16-alpine`, run as root, `chown -R 10001:10001`)
  runs before the app on every `up` so a volume created by an older, root-run image is handed
  over; `app` waits for it (`service_completed_successfully`). Its exited container is expected.
- **Base images are pinned by digest**: `python:3.12-slim@sha256:…` in `backend/Dockerfile`, and
  `postgres:16-alpine@sha256:…` for both `db` and `backups-owner` (keep the two equal). Pin the
  multi-platform index digest (`docker buildx imagetools inspect <image>` prints it), never a
  single-platform one, or ARM hosts break. Dependabot proposes new digests; never move Postgres to
  another major version (a newer server can't open the data directory without a dump and restore).
  The Dockerfile removes pip (and ensurepip's bundled copy) in the install layer: the running
  image has no package installer.
- **Deployed by** — by hand (`git pull && ./deploy.sh`) or unattended (for example a job
  that updates the checkout, then runs `deploy.sh` with no terminal and `DEPLOY_OLD_HEAD`
  set to the commit the running app was built from). So
  `deploy.sh` must work with no prompt at any time, and it has no git step: it deploys the
  checkout as it is. `tests/test_deploy_script.py` proves it leaves git alone, refuses a bad `.env` and
  decides `--no-cache` correctly, without deploying anything.
- **`.env`** — host state: not in git, not reproducible from the repo. It stays out of
  git via `.gitignore`. No script in this repo may stash, clean, or otherwise remove
  untracked files. It holds the passwords, the signing key and any API key: keep it mode 600
  (`setup.sh` does).

## Releases

- **Version**: `backend/VERSION`, one line, `MAJOR.MINOR.PATCH`, and the only place it is written
  (`version.py` reads it; `test_version.py` checks it). The image carries it (`COPY . .`). It is
  shown under Settings → Updates (`GET /api/version`, signed-in only: `/api/health` stays bare so an
  unauthenticated caller can't tell which fixes a server has) and printed by `deploy.sh`.
- **Every merge to `main` is a release; the bump is automatic.** Don't edit `backend/VERSION` in a
  change. The release picks the bump from the commits since the last release:
  - a `Release: major|minor|patch|none` trailer on a commit decides for that commit, so **add one
    whenever the subject alone would mislead**: `Release: major` for anything users must act on
    (an `.env` change, a migration that can't be undone, a removed feature; say what to do in the
    body), `Release: minor` for a new feature whose subject doesn't start with a verb below;
  - otherwise `BREAKING CHANGE:` in the body (or a subject starting "Breaking") means major, a
    change touching only tests or CI means none, one touching docs (README, `docs/`) a patch, a subject starting Add / Introduce / New /
    Support / Enable / Allow means minor, and anything else patch;
  - the release takes the highest. A push that changed only tests or CI (`trivial`) is not
    released on its own: it goes out with the next user-facing change, or as a patch from a manual run.
- **A release is a tag on `main`**: the run for a push to `main` (`release.yml`, after the tests)
  commits the bump to `backend/VERSION` as `Release <version>`, tags that commit `v<version>` with the
  notes (the commit subjects since the previous release, merges and the bump left out; pull request
  numbers stay and link) and creates the GitHub Release from it. `main`'s ruleset requires a pull request
  and offers no bypass for the workflow token, so the bump is pushed over SSH with a write deploy key on
  the ruleset's bypass list (the `RELEASE_DEPLOY_KEY` secret; the release README has the setup); its push
  would start a run, so the bump commit's body says `[skip ci]`. Actions → Release → Run workflow still works, for
  a chosen bump or a dry run. Every pull request runs the same steps as a preview, with the denylist
  required unless the pull request comes from a fork or Dependabot (no repository secrets there). The
  tool and its setup live in `.github/release/` (README there).
- **Nothing personal in the repository, ever.** The repository is public and its history is permanent:
  a privacy check runs before every release and, through `test_release.py`, over every tracked file on
  every pull request (secrets, session links, real email addresses, card-like numbers, home directory
  paths, `.env`/key/database/backup files, and the maintainer's denylist in the `PUBLISH_DENYLIST`
  secret). Test data and docs use synthetic values: `example.com` addresses, made-up names and
  institutions ("Example Bank"), and brands only as peers (see "Brands in code are peers"). Commit
  messages are public too: no session links, no personal detail. Commit subjects become the release
  notes, so write them for users. **Before pushing, run `python3 .github/release/release.py preview`**
  (the Claude Code hook in `.claude/settings.json` runs it before every `git push` and refuses the
  push on a finding; the denylist is read from `$PUBLISH_DENYLIST` or `$PUBLISH_DENYLIST_FILE` when
  set, else CI applies it on the pull request). A denylist hit means the term comes out of the tree,
  never the entry out of the list.
- **Actions are pinned by commit** (`uses: owner/action@<sha> # vX.Y.Z`), like the base images;
  Dependabot proposes updates.
