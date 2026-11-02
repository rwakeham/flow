#!/usr/bin/env bash
# setup.sh — Idempotent first-run & re-run config wizard for Flow.
# Writes/updates .env, then hands off to deploy.sh for the actual deployment.
#
# Usage:
#   ./setup.sh

set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$(basename "${BASH_SOURCE[0]}")"
cd "$REPO_ROOT"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}▶${NC} $*"; }
success() { echo -e "${GREEN}✓${NC} $*"; }
warn()    { echo -e "${YELLOW}⚠${NC} $*"; }
error()   { echo -e "${RED}✗${NC} $*" >&2; }
header()  { STAGE="$*"; echo -e "\n${BOLD}═══ $* ═══${NC}"; }

STAGE="startup"
on_err() {
  local code=$? line=$1
  # -E hands this trap to $(…), <(…) and ( ) subshells, whose exit ends only the
  # subshell. A status the parent sees fires it again there; report only from the top.
  (( BASH_SUBSHELL == 0 )) || exit "$code"
  error "${SELF} failed during '${STAGE}' at ${BASH_SOURCE[0]}:${line} (exit ${code})"
  echo "  Whatever that step touched is left as-is — nothing was rolled back." >&2
  exit "$code"
}
trap 'on_err $LINENO' ERR

# ─── Parse arguments ─────────────────────────────────────────────────────────
if [[ $# -gt 0 ]]; then
  error "Unknown argument: $1"
  echo "Usage: ./setup.sh"
  exit 1
fi

# ─── Prerequisites ───────────────────────────────────────────────────────────
header "Checking prerequisites"

if ! command -v docker &>/dev/null; then
  error "docker is not installed or not in PATH"
  exit 1
fi
success "docker found"

if ! command -v openssl &>/dev/null; then
  error "openssl is not installed or not in PATH"
  exit 1
fi
success "openssl found"

if ! docker compose version &>/dev/null; then
  error "Docker Compose v2 (docker compose) not found"
  exit 1
fi
success "Docker Compose v2 found"

if ! docker info &>/dev/null; then
  error "Docker daemon is not running, or this user cannot reach it"
  exit 1
fi
success "Docker daemon reachable"

# ─── .env helpers ────────────────────────────────────────────────────────────
# Values are written double-quoted with \ " and $ escaped. Docker Compose reads an
# unquoted value with ${VAR} substitution and " #" comments, so a password such as
# `pa$word #1` would reach the app as "pa"; quoted like this, Compose reads back
# exactly what was typed (any character but a newline).
#
# _env_get is the same function in deploy.sh: keep the two identical
# (tests/test_deploy_script.py checks). It reads values written this way, single-quoted
# ones, and the unquoted values older versions of this script wrote.
_env_get() {
  local key="$1" line val out="" i c
  [[ -f .env ]] || return 0
  line="$(grep -E "^${key}=" .env | head -1 || true)"
  [[ -n "$line" ]] || return 0
  val="${line#*=}"
  if [[ ${#val} -ge 2 && "${val:0:1}" == '"' && "${val: -1}" == '"' ]]; then
    val="${val:1:${#val}-2}"
    for (( i = 0; i < ${#val}; i++ )); do
      c="${val:i:1}"
      if [[ "$c" == '\' && $(( i + 1 )) -lt ${#val} ]]; then
        i=$(( i + 1 )); c="${val:i:1}"
        case "$c" in '\'|'"'|'$') ;; *) c="\\${c}" ;; esac
      fi
      out+="$c"
    done
    val="$out"
  elif [[ ${#val} -ge 2 && "${val:0:1}" == "'" && "${val: -1}" == "'" ]]; then
    val="${val:1:${#val}-2}"
    val="${val//\\\'/\'}"
  fi
  printf '%s' "$val"
}

_env_quote() {
  local v="$1"
  v="${v//\\/\\\\}"
  v="${v//\"/\\\"}"
  v="${v//\$/\\\$}"
  printf '"%s"' "$v"
}

# Rewrites .env through a temporary file (mktemp: mode 600) and moves it into place, so
# the file holding the passwords, signing key and API key is readable by its owner only.
_env_set() {
  local key="$1" val="$2" line found=false tmp
  tmp="$(mktemp .env.XXXXXX)"
  if [[ -f .env ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$line" == "${key}="* ]]; then
        $found || printf '%s=%s\n' "$key" "$(_env_quote "$val")"
        found=true
      else
        printf '%s\n' "$line"
      fi
    done < .env > "$tmp"
  fi
  $found || printf '%s=%s\n' "$key" "$(_env_quote "$val")" >> "$tmp"
  mv -f "$tmp" .env
}

# Removes a key's line(s) from .env (for values Flow keeps itself once it has started).
_env_unset() {
  local key="$1" line tmp
  [[ -f .env ]] || return 0
  tmp="$(mktemp .env.XXXXXX)"
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" == "${key}="* ]] || printf '%s\n' "$line"
  done < .env > "$tmp"
  mv -f "$tmp" .env
}

# Values the user set in this run: not offered for removal at the end.
JUST_SET=""

FIRST_RUN=false
[ -f .env ] || FIRST_RUN=true
# An .env written by hand, or by an older version of this script, may be readable by
# every user on the machine.
if [ -f .env ]; then chmod 600 .env; fi

# ─── POSTGRES_PASSWORD ───────────────────────────────────────────────────────
header "Database password"

# Found by the labels Compose puts on it, not a hard-coded name: the project
# name follows the checkout directory. docker info above makes an empty answer
# mean "no volume" rather than "no daemon".
PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$REPO_ROOT" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')}"
PG_VOLUMES="$(docker volume ls -q --filter "label=com.docker.compose.project=${PROJECT}" \
               --filter "label=com.docker.compose.volume=pgdata")"
VOLUME_EXISTS=0
if [[ -n "$PG_VOLUMES" ]]; then VOLUME_EXISTS=1; fi
EXISTING_PG_PASS=$(_env_get POSTGRES_PASSWORD)

if [ -n "$EXISTING_PG_PASS" ]; then
  success "POSTGRES_PASSWORD already set in .env — keeping it"
elif [ "$VOLUME_EXISTS" -gt 0 ]; then
  warn "Existing database volume found but no POSTGRES_PASSWORD in .env"
  warn "Enter the password used when the volume was first created."
  while true; do
    IFS= read -rsp "  Database password: " POSTGRES_PASSWORD; echo
    [ -n "$POSTGRES_PASSWORD" ] && break
    error "Password cannot be empty"
  done
  _env_set POSTGRES_PASSWORD "$POSTGRES_PASSWORD"
  success "POSTGRES_PASSWORD saved"
else
  POSTGRES_PASSWORD=$(openssl rand -hex 16)
  _env_set POSTGRES_PASSWORD "$POSTGRES_PASSWORD"
  success "POSTGRES_PASSWORD generated and saved"
fi

# ─── SECRET_KEY ──────────────────────────────────────────────────────────────
header "Application secret key"

# A key copied from .env.example, or too short, is replaced (deploy.sh and the app
# refuse it). Replacing the key only signs everyone out.
_weak_secret() {
  case "$(printf %s "$1" | tr '[:upper:]' '[:lower:]')" in replacewithsecretrandomhex|changeme) return 0 ;; esac
  (( ${#1} < 32 ))
}
_weak_password() {
  case "$(printf %s "$1" | tr '[:upper:]' '[:lower:]')" in mysecretpassword|changeme|password) return 0 ;; esac
  return 1
}

EXISTING_SECRET=$(_env_get SECRET_KEY)
if [ -n "$EXISTING_SECRET" ] && ! _weak_secret "$EXISTING_SECRET"; then
  success "SECRET_KEY already set in .env — keeping it"
else
  SECRET_KEY=$(openssl rand -hex 32)
  _env_set SECRET_KEY "$SECRET_KEY"
  success "SECRET_KEY generated and saved"
fi

# ─── APP_PORT ────────────────────────────────────────────────────────────────
header "Application port"

EXISTING_PORT=$(_env_get APP_PORT)

_port_free() {
  # grep reads to the end rather than -q, so the listing never dies of SIGPIPE.
  ! (ss -tlnH 2>/dev/null || netstat -tln 2>/dev/null) | grep -E ":$1\b" >/dev/null
}

_next_free_port() {
  local p="${1:-8000}"
  while ! _port_free "$p"; do
    p=$((p + 1))
  done
  echo "$p"
}

if [ -n "$EXISTING_PORT" ]; then
  if _port_free "$EXISTING_PORT"; then
    success "APP_PORT=${EXISTING_PORT} is free — keeping it"
  else
    warn "Configured port ${EXISTING_PORT} is already in use"
    SUGGESTED=$(_next_free_port "$EXISTING_PORT")
    read -rp "  Enter a different port [${SUGGESTED}]: " NEW_PORT
    APP_PORT="${NEW_PORT:-$SUGGESTED}"
    _env_set APP_PORT "$APP_PORT"
    success "APP_PORT set to ${APP_PORT}"
  fi
else
  SUGGESTED=$(_next_free_port 8000)
  if [ "$SUGGESTED" -eq 8000 ]; then
    _env_set APP_PORT 8000
    success "APP_PORT set to 8000"
  else
    warn "Port 8000 is already in use by another service"
    read -rp "  Enter a port to use [${SUGGESTED}]: " NEW_PORT
    APP_PORT="${NEW_PORT:-$SUGGESTED}"
    _env_set APP_PORT "$APP_PORT"
    success "APP_PORT set to ${APP_PORT}"
  fi
fi

APP_PORT=$(_env_get APP_PORT)

# ─── FLOW_PASSWORD ───────────────────────────────────────────────────────────
header "Dashboard password"

# Flow keeps the password itself (as a hash in its database) once it has started: this
# value is for the first start, and a reset while it stays in .env. Sign out ▾ → Change
# password changes it in the app.
EXISTING_PASS=$(_env_get FLOW_PASSWORD)

if [ -n "$EXISTING_PASS" ] && _weak_password "$EXISTING_PASS"; then
  warn "The dashboard password in .env is the example value: choose a new one"
  EXISTING_PASS=""
fi

_read_new_password() {
  while true; do
    IFS= read -rsp "  ${1:-Dashboard password}: " FLOW_PASSWORD; echo
    [ -z "$FLOW_PASSWORD" ] && { error "Password cannot be empty"; continue; }
    _weak_password "$FLOW_PASSWORD" && { error "That is the example password; choose another"; continue; }
    IFS= read -rsp "  Confirm: " FLOW_PASSWORD2; echo
    [ "$FLOW_PASSWORD" = "$FLOW_PASSWORD2" ] && break
    error "Passwords do not match — try again"
  done
  _env_set FLOW_PASSWORD "$FLOW_PASSWORD"
  JUST_SET="${JUST_SET} FLOW_PASSWORD"
}

if [ -n "$EXISTING_PASS" ]; then
  read -rp "  FLOW_PASSWORD is set in .env. Update it? [y/N] " UPDATE_PASS
  if [[ "${UPDATE_PASS:-n}" =~ ^[Yy]$ ]]; then
    _read_new_password "New dashboard password"; success "Dashboard password updated"
  else
    success "Dashboard password unchanged"
  fi
elif [ "$VOLUME_EXISTS" -gt 0 ]; then
  info "Flow keeps the dashboard password itself (Sign out ▾ → Change password in the app)."
  read -rp "  Reset it from here? [y/N] " RESET_PASS
  if [[ "${RESET_PASS:-n}" =~ ^[Yy]$ ]]; then
    _read_new_password "New dashboard password"
    success "Dashboard password set: Flow takes it on the next start (remove it from .env after that, or keep it to pin it)"
  else
    success "Dashboard password kept as it is in Flow"
  fi
else
  _read_new_password "Dashboard password"
  success "Dashboard password set"
fi

# ─── AI features (optional) ──────────────────────────────────────────────────
header "AI features (optional)"

# Card categorization, insights and the Assistant use the Anthropic API with the user's
# own key. Flow keeps the key itself (encrypted in its database) once it has started: a key
# entered here is for the first start, or a replacement while it stays in .env. The models,
# the on/off switch and the redact words are set under AI in Flow's top bar.
EXISTING_KEY=$(_env_get ANTHROPIC_API_KEY)
if [ -n "$EXISTING_KEY" ]; then
  success "ANTHROPIC_API_KEY is set in .env (…${EXISTING_KEY: -4})"
  read -rp "  Change it? [y/N] " SET_AI
elif [ "$VOLUME_EXISTS" -gt 0 ]; then
  info "The API key, models and other AI settings are managed under AI in Flow's top bar."
  read -rp "  Set a key from here instead? [y/N] " SET_AI
else
  info "Flow can use Claude to categorize card merchants its rules don't know, write"
  info "spending insights, and answer questions in the Assistant. It needs your own"
  info "Anthropic API key (console.anthropic.com). Without one, nothing is sent anywhere."
  info "You can also add it later under AI in Flow's top bar."
  read -rp "  Set up the API key now? [y/N] " SET_AI
fi

if [[ "${SET_AI:-n}" =~ ^[Yy]$ ]]; then
  while true; do
    if [ -n "$EXISTING_KEY" ]; then
      read -rsp "  Anthropic API key (Enter keeps the current one, 'off' removes it from .env): " NEW_KEY; echo
    else
      read -rsp "  Anthropic API key (Enter to skip): " NEW_KEY; echo
    fi
    NEW_KEY="$(printf %s "$NEW_KEY" | tr -d '[:space:]')"
    if [ -z "$NEW_KEY" ]; then break; fi
    if [ "$NEW_KEY" = "off" ]; then
      _env_unset ANTHROPIC_API_KEY
      success "ANTHROPIC_API_KEY removed from .env (Flow keeps whatever key it holds; remove that under AI in the top bar)"
      break
    fi
    if ! [[ "$NEW_KEY" =~ ^[A-Za-z0-9_-]+$ ]]; then
      error "That doesn't look like an API key (letters, digits, - and _ only)"
      continue
    fi
    if [[ "$NEW_KEY" != sk-ant-* ]]; then
      read -rp "  Anthropic keys start with sk-ant-. Use this one anyway? [y/N] " ODD_KEY
      [[ "${ODD_KEY:-n}" =~ ^[Yy]$ ]] || continue
    fi
    _env_set ANTHROPIC_API_KEY "$NEW_KEY"
    JUST_SET="${JUST_SET} ANTHROPIC_API_KEY"
    success "API key saved (…${NEW_KEY: -4}); models and the rest are under AI in Flow's top bar"
    break
  done
else
  success "AI settings unchanged"
fi

# ─── Values Flow now keeps ───────────────────────────────────────────────────
# Once Flow has started, the password, the key and the AI settings live in its database
# and are managed in the app. A value still in .env is copied in at every start and wins
# while it is there, so offer to take out what this run didn't just set.
if [ "$VOLUME_EXISTS" -gt 0 ]; then
  LEFTOVER=""
  for _key in FLOW_PASSWORD ANTHROPIC_API_KEY FLOW_AI_MODEL FLOW_AI_MODEL_CATEGORIZE FLOW_AI_MODEL_ASK FLOW_AI_ENABLED FLOW_AI_REDACT FLOW_UPDATE_CHECK; do
    [[ " $JUST_SET " == *" $_key "* ]] && continue
    [[ -n "$(_env_get "$_key")" ]] && LEFTOVER="${LEFTOVER} ${_key}"
  done
  if [[ -n "${LEFTOVER// /}" ]]; then
    header "Values Flow keeps itself"
    info "Flow copied these into its own settings when it started, and manages them in the app"
    info "(AI in the top bar; Sign out ▾ → Change password). While they stay in .env they win:"
    info " ${LEFTOVER}"
    read -rp "  Remove them from .env now? [y/N] " DROP
    if [[ "${DROP:-n}" =~ ^[Yy]$ ]]; then
      for _key in $LEFTOVER; do _env_unset "$_key"; done
      success "Removed from .env:${LEFTOVER}"
    else
      success "Kept in .env"
    fi
  fi
fi

# ─── Handoff ─────────────────────────────────────────────────────────────────
# All container operations, git handling, health checks and summary output live
# in deploy.sh so a setup.sh-triggered deploy and a deploy.sh-triggered deploy
# are identical apart from whether credentials were prompted for first.
header "Handing off to deploy.sh"
exec ./deploy.sh
