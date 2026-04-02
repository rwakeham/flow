#!/usr/bin/env bash
# deploy.sh — Pull latest code (optionally merge a claude/ branch) and redeploy.
#
# Usage:
#   ./deploy.sh              # auto-detect & merge any pending claude/ branch
#   ./deploy.sh --skip-git   # skip git, just rebuild and restart

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}▶${NC} $*"; }
success() { echo -e "${GREEN}✓${NC} $*"; }
warn()    { echo -e "${YELLOW}⚠${NC} $*"; }
error()   { echo -e "${RED}✗${NC} $*" >&2; }
header()  { echo -e "\n${BOLD}═══ $* ═══${NC}"; }

# ─── Compose detection ───────────────────────────────────────────────────────
if docker compose version &>/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose &>/dev/null; then
  COMPOSE="docker-compose"
else
  error "docker compose not found"; exit 1
fi

# ─── Arguments ───────────────────────────────────────────────────────────────
SKIP_GIT=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-git) SKIP_GIT=true; shift ;;
    *)
      error "Unknown argument: $1"
      echo "Usage: ./deploy.sh [--skip-git]"
      exit 1
      ;;
  esac
done

# ─── Git ─────────────────────────────────────────────────────────────────────
if $SKIP_GIT; then
  info "Skipping git (--skip-git)"
  OLD_HEAD=$(git rev-parse HEAD)
else
  header "Updating Code"
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
    echo -e "${BOLD}Multiple feature branches are ahead of main:${NC}"
    for i in "${!AHEAD_BRANCHES[@]}"; do
      echo "  $((i+1))) ${AHEAD_BRANCHES[$i]}  (${AHEAD_COUNTS[$i]} commit(s) ahead)"
    done
    echo ""
    read -rp "Which branch to merge and deploy? [1]: " _CHOICE
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

# ─── Build & Deploy ──────────────────────────────────────────────────────────
header "Building & Deploying"

BUILD_FLAGS=""
if git diff --name-only "$OLD_HEAD" HEAD 2>/dev/null | grep -q 'requirements\.txt'; then
  warn "requirements.txt changed — rebuilding without cache..."
  BUILD_FLAGS="--no-cache"
fi

$COMPOSE up --build $BUILD_FLAGS -d --remove-orphans
success "Containers started"

# ─── Done ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}✓ Flow is running!${NC}"
echo ""
echo -e "  ${BOLD}Dashboard${NC}  http://localhost:8000"
echo ""
echo -e "  ${BOLD}Management${NC}"
echo "  Logs:      docker compose logs -f app"
echo "  Stop:      docker compose down"
echo "  Redeploy:  ./deploy.sh"
echo ""
