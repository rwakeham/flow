#!/usr/bin/env bash
# setup.sh — Idempotent first-run & re-run setup for Flow
#
# Usage:
#   ./setup.sh              # prompt for password, build & start
#   ./setup.sh --skip-git   # skip git pull/merge (local dev)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# ─── Colour helpers ──────────────────────────────────────────────────────────
if [ -t 1 ]; then
  _R='\033[0m'; _BOLD='\033[1m'
  _CYAN='\033[0;36m'; _GREEN='\033[0;32m'
  _YELLOW='\033[0;33m'; _RED='\033[0;31m'
else
  _R=''; _BOLD=''; _CYAN=''; _GREEN=''; _YELLOW=''; _RED=''
fi

info()    { echo -e "${_CYAN}  →  $*${_R}"; }
success() { echo -e "${_GREEN}  ✓  $*${_R}"; }
warn()    { echo -e "${_YELLOW}  ⚠  $*${_R}"; }
error()   { echo -e "${_RED}  ✗  $*${_R}" >&2; }
header()  { echo -e "\n${_BOLD}${_CYAN}▶  $*${_R}"; }

# ─── Parse arguments ─────────────────────────────────────────────────────────
SKIP_GIT=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-git) SKIP_GIT=true; shift ;;
    *)
      error "Unknown argument: $1"
      echo "Usage: ./setup.sh [--skip-git]"
      exit 1
      ;;
  esac
done

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

if docker compose version &>/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
  COMPOSE="docker-compose"
else
  error "Neither 'docker compose' (v2) nor 'docker-compose' (v1) found"
  exit 1
fi
success "Compose: $COMPOSE"

# ─── .env helpers ────────────────────────────────────────────────────────────
_env_get() {
  local key="$1"
  if [ -f .env ]; then
    grep -E "^${key}=" .env | head -1 | cut -d= -f2- || true
  fi
}

_env_set() {
  local key="$1" val="$2"
  [ -f .env ] || touch .env
  if grep -qE "^${key}=" .env; then
    local escaped
    escaped=$(printf '%s\n' "$val" | sed 's/[\/&]/\\&/g')
    sed -i "s|^${key}=.*|${key}=${escaped}|" .env
  else
    printf '%s=%s\n' "$key" "$val" >> .env
  fi
}

FIRST_RUN=false
[ -f .env ] || FIRST_RUN=true

# ─── POSTGRES_PASSWORD ───────────────────────────────────────────────────────
header "Database password"

VOLUME_EXISTS=$(docker volume ls --format '{{.Name}}' | grep -cE 'flow.*pgdata' || true)
EXISTING_PG_PASS=$(_env_get POSTGRES_PASSWORD)

if [ -n "$EXISTING_PG_PASS" ]; then
  success "POSTGRES_PASSWORD already set in .env — keeping it"
elif [ "$VOLUME_EXISTS" -gt 0 ]; then
  warn "Existing database volume found but no POSTGRES_PASSWORD in .env"
  warn "Enter the password used when the volume was first created."
  while true; do
    read -rsp "  Database password: " POSTGRES_PASSWORD; echo
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

EXISTING_SECRET=$(_env_get SECRET_KEY)
if [ -n "$EXISTING_SECRET" ]; then
  success "SECRET_KEY already set in .env — keeping it"
else
  SECRET_KEY=$(openssl rand -hex 32)
  _env_set SECRET_KEY "$SECRET_KEY"
  success "SECRET_KEY generated and saved"
fi

# ─── FLOW_PASSWORD ───────────────────────────────────────────────────────────
header "Dashboard password"

EXISTING_PASS=$(_env_get FLOW_PASSWORD)

if [ -n "$EXISTING_PASS" ] && [ "$FIRST_RUN" = false ]; then
  read -rp "  Update dashboard password? [y/N] " UPDATE_PASS
  if [[ "${UPDATE_PASS:-n}" =~ ^[Yy]$ ]]; then
    while true; do
      read -rsp "  New dashboard password: " FLOW_PASSWORD; echo
      [ -z "$FLOW_PASSWORD" ] && { error "Password cannot be empty"; continue; }
      read -rsp "  Confirm new password: " FLOW_PASSWORD2; echo
      [ "$FLOW_PASSWORD" = "$FLOW_PASSWORD2" ] && break
      error "Passwords do not match — try again"
    done
    _env_set FLOW_PASSWORD "$FLOW_PASSWORD"
    success "Dashboard password updated"
  else
    success "Dashboard password unchanged"
  fi
else
  while true; do
    read -rsp "  Dashboard password: " FLOW_PASSWORD; echo
    [ -z "$FLOW_PASSWORD" ] && { error "Password cannot be empty"; continue; }
    read -rsp "  Confirm password: " FLOW_PASSWORD2; echo
    [ "$FLOW_PASSWORD" = "$FLOW_PASSWORD2" ] && break
    error "Passwords do not match — try again"
  done
  _env_set FLOW_PASSWORD "$FLOW_PASSWORD"
  success "Dashboard password set"
fi

# ─── Git ─────────────────────────────────────────────────────────────────────
if $SKIP_GIT; then
  info "Skipping git (--skip-git)"
  OLD_HEAD=$(git rev-parse HEAD)
else
  header "Updating code"
  git fetch origin

  declare -a AHEAD_BRANCHES=()
  declare -a AHEAD_COUNTS=()
  while IFS= read -r remote_branch; do
    branch_ahead=$(git rev-list --count "origin/main..${remote_branch}" 2>/dev/null || echo "0")
    if [[ "$branch_ahead" -gt 0 ]]; then
      AHEAD_BRANCHES+=("${remote_branch#origin/}")
      AHEAD_COUNTS+=("$branch_ahead")
    fi
  done < <(git branch -r --format '%(refname:short)' | grep 'origin/claude/')

  FEATURE_BRANCH=""
  AHEAD=0

  if [[ "${#AHEAD_BRANCHES[@]}" -eq 0 ]]; then
    info "Nothing to merge — deploying current main"
  elif [[ "${#AHEAD_BRANCHES[@]}" -eq 1 ]]; then
    FEATURE_BRANCH="${AHEAD_BRANCHES[0]}"
    AHEAD="${AHEAD_COUNTS[0]}"
  else
    echo ""
    echo -e "${_BOLD}Multiple feature branches are ahead of main:${_R}"
    for i in "${!AHEAD_BRANCHES[@]}"; do
      echo "  $((i+1))) ${AHEAD_BRANCHES[$i]}  (${AHEAD_COUNTS[$i]} commit(s) ahead)"
    done
    echo ""
    read -rp "  Which branch to merge and deploy? [1]: " _CHOICE
    _IDX=$(( ${_CHOICE:-1} - 1 ))
    if [[ "$_IDX" -lt 0 || "$_IDX" -ge "${#AHEAD_BRANCHES[@]}" ]]; then
      error "Invalid selection."; exit 1
    fi
    FEATURE_BRANCH="${AHEAD_BRANCHES[$_IDX]}"
    AHEAD="${AHEAD_COUNTS[$_IDX]}"
  fi

  git checkout main
  OLD_HEAD=$(git rev-parse HEAD)
  git reset --hard origin/main

  if [[ "$AHEAD" -gt 0 ]]; then
    info "Merging ${FEATURE_BRANCH} (${AHEAD} commit(s)) into main..."
    git merge --no-ff "origin/${FEATURE_BRANCH}" -m "Merge ${FEATURE_BRANCH} into main"
    git push
    success "Merged and pushed"
  fi
fi

# ─── Build & start ───────────────────────────────────────────────────────────
header "Building and starting services"

BUILD_FLAGS=""
if git diff --name-only "$OLD_HEAD" HEAD 2>/dev/null | grep -q 'requirements\.txt'; then
  warn "requirements.txt changed — rebuilding without cache..."
  BUILD_FLAGS="--no-cache"
fi

$COMPOSE build $BUILD_FLAGS
$COMPOSE up -d --remove-orphans
success "Services started"

# ─── Health check ────────────────────────────────────────────────────────────
header "Waiting for services to be ready"

_wait_for() {
  local name="$1" cmd="$2" max="${3:-30}"
  local i=0
  info "Waiting for ${name}..."
  while ! eval "$cmd" &>/dev/null 2>&1; do
    i=$((i + 1))
    if [[ $i -ge $max ]]; then
      error "${name} did not become ready after ${max}s"
      exit 1
    fi
    sleep 1
  done
  success "${name} is ready"
}

_wait_for "PostgreSQL" "$COMPOSE exec -T db pg_isready -U flow" 30
_wait_for "Flow app"   "curl -sf http://localhost:8000/api/health" 60

# ─── Done ────────────────────────────────────────────────────────────────────
echo
echo -e "${_BOLD}${_GREEN}╔══════════════════════════════════════╗${_R}"
echo -e "${_BOLD}${_GREEN}║   Flow is ready!                     ║${_R}"
echo -e "${_BOLD}${_GREEN}║   Dashboard: http://localhost:8000   ║${_R}"
echo -e "${_BOLD}${_GREEN}╚══════════════════════════════════════╝${_R}"
echo
info "Logs:  docker compose logs -f app"
info "Stop:  docker compose down"
info "Again: ./setup.sh"
echo
