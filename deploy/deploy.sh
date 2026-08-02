#!/usr/bin/env bash
# Deploy pipeline: promote committed `main` from the dev checkout to prod.
#
#   dev:  /home/hariohm/indian-trading-agent       (./start.sh -> dellg15:3000)
#   prod: /home/hariohm/indian-trading-agent-prod  (systemd :8100/:3100 -> trade.hariohm.in)
#
# Usage:  ./deploy/deploy.sh
#
# Only COMMITTED state on `main` is deployed. Uncommitted/untracked changes
# stay in dev. On health-check failure, prod auto-rolls back to the
# previous commit.
set -euo pipefail

DEV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROD_DIR="/home/hariohm/indian-trading-agent-prod"
BRANCH="main"
BACKEND_PORT=8100
FRONTEND_PORT=3100
SERVICES=(trading-agent-prod-backend trading-agent-prod-frontend)

log()  { echo "[deploy] $*"; }
ok()   { echo "[ok]     $*"; }
warn() { echo "[warn]   $*"; }
err()  { echo "[err]    $*" 1>&2; }

# --- Pre-flight -----------------------------------------------------------
[[ -d "$PROD_DIR/.git" ]] || { err "Prod checkout missing at $PROD_DIR"; exit 1; }

cd "$DEV_DIR"
TARGET_COMMIT="$(git rev-parse "$BRANCH")"
SHORT="$(git rev-parse --short "$TARGET_COMMIT")"

if [[ -n "$(git status --porcelain)" ]]; then
  warn "Dev working tree has uncommitted/untracked changes — they will NOT be deployed."
fi

PREV_COMMIT="$(git -C "$PROD_DIR" rev-parse HEAD)"
if [[ "$PREV_COMMIT" == "$TARGET_COMMIT" ]] && [[ "${1:-}" != "--force" ]]; then
  ok "Prod already at $SHORT. Nothing to do (use --force to redeploy)."
  exit 0
fi

log "Deploying $BRANCH@$SHORT to prod (previous: $(git rev-parse --short "$PREV_COMMIT"))..."

# --- Sync code ------------------------------------------------------------
git -C "$PROD_DIR" fetch --quiet origin "$BRANCH"
git -C "$PROD_DIR" reset --hard --quiet "$TARGET_COMMIT"

restart_and_verify() {
  sudo -n systemctl reset-failed "${SERVICES[@]}" 2>/dev/null || true
  sudo -n systemctl restart "${SERVICES[@]}"
  local i
  for i in $(seq 1 30); do
    if curl -sf -m 2 "http://localhost:${BACKEND_PORT}/api/health" >/dev/null 2>&1 \
       && curl -sf -o /dev/null -m 2 "http://localhost:${FRONTEND_PORT}" 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

build_and_restart() {
  # Python deps only when pyproject changed
  if ! git -C "$PROD_DIR" diff --quiet "$PREV_COMMIT" "$TARGET_COMMIT" -- pyproject.toml 2>/dev/null; then
    log "pyproject.toml changed — reinstalling Python deps..."
    "$PROD_DIR/venv/bin/pip" install --quiet -e "$PROD_DIR"
    "$PROD_DIR/venv/bin/pip" install --quiet fastapi uvicorn websockets aiosqlite numpy feedparser
  fi

  # npm deps only when lockfile changed; build always (code changes need it)
  if ! git -C "$PROD_DIR" diff --quiet "$PREV_COMMIT" "$TARGET_COMMIT" -- frontend/package-lock.json 2>/dev/null; then
    log "package-lock.json changed — reinstalling npm deps..."
    (cd "$PROD_DIR/frontend" && npm ci --no-audit --no-fund)
  fi
  log "Building frontend..."
  (cd "$PROD_DIR/frontend" && npm run build)

  log "Restarting prod services..."
  restart_and_verify
}

if build_and_restart; then
  ok "Deployed $SHORT."
  ok "Prod:  https://trade.hariohm.in  (backend :${BACKEND_PORT}, frontend :${FRONTEND_PORT})"
  ok "Dev:   http://dellg15:3000       (start with ./start.sh)"
else
  err "Health check failed after deploy. Rolling back to $(git rev-parse --short "$PREV_COMMIT")..."
  git -C "$PROD_DIR" reset --hard --quiet "$PREV_COMMIT"
  (cd "$PROD_DIR/frontend" && npm run build)
  if restart_and_verify; then
    err "Rollback succeeded — prod is back on the previous commit."
    err "Investigate: journalctl -u trading-agent-prod-backend -n 50"
  else
    err "ROLLBACK ALSO FAILED. Check: journalctl -u trading-agent-prod-backend -u trading-agent-prod-frontend -n 100"
  fi
  exit 1
fi
