#!/usr/bin/env bash
# Deploy the production Docker stack.
#
# GitHub Actions calls this same command on the local self-hosted runner.
# It is also safe to run manually:
#
#   ./deploy/deploy.sh
#
# Runtime secrets and host paths live outside the repository in:
#   ~/.config/indian-trading-agent/compose.env
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/deploy/docker-compose.prod.yml"
COMPOSE_ENV_FILE="${TRADING_AGENT_COMPOSE_ENV_FILE:-$HOME/.config/indian-trading-agent/compose.env}"

log() { printf '[deploy] %s\n' "$*"; }
ok() { printf '[ok]     %s\n' "$*"; }
err() { printf '[err]    %s\n' "$*" >&2; }

if [[ ! -f "$COMPOSE_ENV_FILE" ]]; then
  err "Missing $COMPOSE_ENV_FILE"
  err "Create it from deploy/compose.env.example before deploying."
  exit 1
fi

compose() {
  docker compose --env-file "$COMPOSE_ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

validate_immutable_images() {
  local image
  local images

  images="$(compose config --images)"
  if [[ -z "$images" ]]; then
    err "Production Compose configuration contains no images."
    exit 1
  fi

  while IFS= read -r image; do
    if [[ ! "$image" =~ @sha256:[0-9a-f]{64}$ ]]; then
      err "Production image is not digest-qualified: $image"
      exit 1
    fi
  done <<< "$images"
}

cd "$ROOT_DIR"
log "Validating immutable production image references..."
validate_immutable_images

log "Pulling the immutable production Docker stack..."
compose pull

log "Starting the production Docker stack..."
compose up -d --remove-orphans

log "Waiting for application health..."
for attempt in $(seq 1 30); do
  if compose exec -T backend python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)" \
    >/dev/null 2>&1 \
    && compose exec -T frontend node -e \
    "fetch('http://127.0.0.1:3000').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))" \
    >/dev/null 2>&1; then
    ok "Production containers are healthy."
    compose ps
    exit 0
  fi
  if [[ "$attempt" == 30 ]]; then
    err "Production health checks failed."
    compose ps
    compose logs --no-color --tail=100 backend frontend cloudflared || true
    exit 1
  fi
  sleep 2
done
