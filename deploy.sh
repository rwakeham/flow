#!/usr/bin/env bash
# deploy.sh — build, start and health-check Flow from this checkout.
#
# Deploys the code on disk, as it is: it never fetches, merges, resets or pushes.
# Update the checkout first (git pull), then run it. It needs no terminal, so a
# scheduled job or other unattended tool can run it the same way.
#
# Usage: ./deploy.sh [--no-cache]
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
  error "Deploy failed during '${STAGE}' at ${BASH_SOURCE[0]}:${line} (exit ${code})"
  echo "  The deployment is in whatever state that line left it — nothing was rolled back." >&2
  exit "$code"
}
trap 'on_err $LINENO' ERR

# deploy.sh deploys this checkout as it is. Getting the right code onto disk
# (git pull, or git checkout v<version>) happens before it runs.
usage() { echo "Usage: ./${SELF} [--no-cache]"; }

NO_CACHE=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-cache) NO_CACHE=true; shift ;;
    # Accepted and ignored: older versions took it to mean "leave git alone", which is
    # now always so.
    --skip-git) shift ;;
    --branch)
      error "${SELF} does not merge branches: it deploys this checkout as it is."
      echo "  Update the checkout first (git pull or git checkout), then run ./${SELF}." >&2
      exit 1 ;;
    -h|--help) usage; exit 0 ;;
    *) error "Unknown argument: $1"; usage >&2; exit 1 ;;
  esac
done

# ─── Compose detection ───────────────────────────────────────────────────────
if docker compose version &>/dev/null; then
  COMPOSE="docker compose"
else
  error "Docker Compose v2 (docker compose) not found"; exit 1
fi

# ─── Preflight ───────────────────────────────────────────────────────────────
# Compose silently substitutes a blank string for any variable it can't find, so
# a missing .env doesn't fail the deploy — it starts the app with an empty
# database password, an empty signing key, and the default port, which then
# collides with whatever else is on 8000. Catch that here instead.

# setup.sh writes values double-quoted with \ " and $ escaped, the form Docker Compose
# reads back exactly. This is the same function as setup.sh's: keep the two identical
# (tests/test_deploy_script.py checks). It also reads single-quoted values and the
# unquoted ones older versions wrote. The `|| true` is load-bearing: grep exits 1 when
# the key is absent, and under `set -e` that would abort instead of returning "not set".
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

# Whether a dependency manifest changed since DEPLOY_OLD_HEAD, the commit the running
# app was built from. Whatever updated the checkout may set it (DEPLOY_OLD_HEAD=$(git
# rev-parse HEAD) before a git pull); without it, or outside a git checkout, there is
# no baseline and Docker's layer cache decides.
deps_changed() {
  local old="${DEPLOY_OLD_HEAD:-}"
  [[ -n "$old" ]] || return 1
  git rev-parse -q --verify "${old}^{commit}" >/dev/null 2>&1 || return 1
  # grep reads to the end: `grep -q` would exit at the first match, git would die of
  # SIGPIPE, and under pipefail a match on a large diff reads as a miss.
  git diff --name-only "$old" HEAD 2>/dev/null \
    | grep -E '(^|/)(requirements[^/]*\.txt|pyproject\.toml|uv\.lock|poetry\.lock|Pipfile(\.lock)?|package(-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|bun\.lockb?|go\.(mod|sum)|Cargo\.(toml|lock)|Gemfile(\.lock)?|composer\.(json|lock))$' >/dev/null
}

# The version this checkout deploys (backend/VERSION, the one place it is written).
# A diagnostic only: a missing file deploys as "unknown".
FLOW_VERSION="$(head -1 backend/VERSION 2>/dev/null | tr -d '[:space:]' || true)"
FLOW_VERSION="${FLOW_VERSION:-unknown}"

header "Preflight"
info "Deploying Flow ${FLOW_VERSION}"

if [[ ! -f .env ]]; then
  error ".env not found in ${REPO_ROOT}"
  error "Without it Compose falls back to blank credentials and port 8000."
  error "Run ./setup.sh — it reuses the existing database volume."
  exit 1
fi

MISSING=""
for _key in POSTGRES_PASSWORD SECRET_KEY; do
  [[ -n "$(_env_get "$_key")" ]] || MISSING="${MISSING} ${_key}"
done
if [[ -n "${MISSING// /}" ]]; then
  error "Blank or missing in .env:${MISSING}"
  error "Run ./setup.sh to fill them in."
  exit 1
fi

# The dashboard password and the Anthropic API key live in Flow's database once it has
# started (set under Settings → Account and Settings → AI in Flow). FLOW_PASSWORD in
# .env is needed for the first start only, and resets the password while it stays there;
# ANTHROPIC_API_KEY likewise sets the key. Whether this is a first start: no database volume
# yet (found by Compose's labels, as setup.sh does; the project name follows the directory).
_PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$REPO_ROOT" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')}"
_PG_VOLUMES="$(docker volume ls -q --filter "label=com.docker.compose.project=${_PROJECT}" \
                 --filter "label=com.docker.compose.volume=pgdata" 2>/dev/null || true)"
_pw="$(_env_get FLOW_PASSWORD)"
if [[ -z "$_pw" && -z "$_PG_VOLUMES" ]]; then
  error "No database yet and FLOW_PASSWORD is blank in .env: the first start needs a dashboard password."
  error "Run ./setup.sh (it asks for one)."
  exit 1
fi

# The app refuses these at startup too (backend/auth.py check_settings); catching
# them here says why before anything is built. Keep both lists in step.
_lower() { tr '[:upper:]' '[:lower:]'; }
WEAK=""
_secret="$(_env_get SECRET_KEY)"
case "$(printf %s "$_secret" | _lower)" in replacewithsecretrandomhex|changeme) WEAK="${WEAK} SECRET_KEY" ;; esac
(( ${#_secret} >= 32 )) || [[ "$WEAK" == *SECRET_KEY* ]] || WEAK="${WEAK} SECRET_KEY(shorter than 32 characters)"
case "$(printf %s "$_pw" | _lower)" in mysecretpassword|changeme|password) WEAK="${WEAK} FLOW_PASSWORD" ;; esac
if [[ -n "${WEAK// /}" ]]; then
  error "Placeholder or weak values in .env:${WEAK}"
  error "Run ./setup.sh to generate a key and set a password."
  exit 1
fi

if [[ -n "$_PG_VOLUMES" ]]; then
  _KEPT=""
  for _key in FLOW_PASSWORD ANTHROPIC_API_KEY FLOW_AI_MODEL FLOW_AI_MODEL_CATEGORIZE FLOW_AI_MODEL_ASK FLOW_AI_ENABLED FLOW_AI_CARDS FLOW_AI_CASH FLOW_AI_BALANCES FLOW_AI_REDACT FLOW_UPDATE_CHECK FLOW_BACKUP_DAILY FLOW_BACKUP_KEEP FLOW_BACKUP_HOUR; do
    [[ -n "$(_env_get "$_key")" ]] && _KEPT="${_KEPT} ${_key}"
  done
  if [[ -n "${_KEPT// /}" ]]; then
    info "Still in .env, so copied into Flow at every start and winning while there:${_KEPT}"
    info "Flow keeps these itself (Settings → AI and Settings → Account in Flow): ./setup.sh offers to remove them."
  fi
fi

# .env holds the passwords, the signing key and any API key. deploy.sh changes no files,
# so it only says so; ./setup.sh (or chmod 600 .env) fixes it.
_ENV_MODE="$(stat -c %a .env 2>/dev/null || stat -f %Lp .env 2>/dev/null || true)"
if [[ "$_ENV_MODE" =~ ^[0-7]{3,4}$ ]] && (( 8#${_ENV_MODE} & 8#077 )); then
  warn ".env is readable by other users on this machine (mode ${_ENV_MODE}): run chmod 600 .env"
fi

APP_PORT="$(_env_get APP_PORT)"
APP_PORT="${APP_PORT:-8000}"
success ".env present, credentials set, APP_PORT=${APP_PORT}"

# ─── Build & Deploy ──────────────────────────────────────────────────────────
header "Building & Deploying"

BUILD_FLAGS=""
if $NO_CACHE; then
  BUILD_FLAGS="--no-cache"
elif deps_changed; then
  warn "Dependencies changed — rebuilding without cache..."
  BUILD_FLAGS="--no-cache"
fi

$COMPOSE build $BUILD_FLAGS
$COMPOSE up -d --remove-orphans
success "Containers started"

# ─── Health check ────────────────────────────────────────────────────────────
# "Containers started" only means Compose handed them to Docker. The app runs
# its migrations in the FastAPI lifespan handler, so a bad DATABASE_URL exits
# the process and `restart: unless-stopped` quietly loops it — which used to be
# reported as a successful deploy.
header "Waiting for Flow to be ready"

wait_for() {
  local name="$1" cmd="$2" max="${3:-30}"
  local i=0
  info "Waiting for ${name}..."
  while ! eval "$cmd" &>/dev/null; do
    i=$((i + 1))
    if [[ $i -ge $max ]]; then
      error "${name} did not become ready after ${max}s"
      exit 1
    fi
    sleep 1
  done
  success "${name} is ready"
}

# Each check runs in a subshell so a timeout (wait_for's `exit 1`) comes back here,
# where the app's log tail is printed before the deploy fails. The tail is a
# diagnostic only: failing to read it changes nothing.
app_logs() {
  error "Last 30 lines from the app container:"
  $COMPOSE logs --tail=30 app >&2 || warn "Could not fetch the app logs"
}
(wait_for "PostgreSQL" "$COMPOSE exec -T db pg_isready -U flow -d flow" 30) || { app_logs; exit 1; }
(wait_for "Flow app" "curl -sf http://localhost:${APP_PORT}/api/health" 60) || { app_logs; exit 1; }

# ─── Done ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}✓ Flow ${FLOW_VERSION} is running!${NC}"
echo ""
echo -e "  ${BOLD}Dashboard${NC}  http://localhost:${APP_PORT}"
echo ""
echo -e "  ${BOLD}Management${NC}"
echo "  Logs:      docker compose logs -f app"
echo "  Stop:      docker compose down"
echo "  Redeploy:  git pull && ./deploy.sh"
echo ""
