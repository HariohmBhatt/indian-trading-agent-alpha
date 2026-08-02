#!/usr/bin/env bash
# Run the isolated development Docker stack.
#
#   ./deploy/dev.sh              # build if needed and start dev
#   ./deploy/dev.sh down         # stop dev containers
#   ./deploy/dev.sh logs         # follow dev logs
#   ./deploy/dev.sh rebuild      # rebuild both dev images
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/deploy/docker-compose.dev.yml"

cd "$ROOT_DIR"

case "${1:-up}" in
  up)
    docker compose -f "$COMPOSE_FILE" up -d --build
    docker compose -f "$COMPOSE_FILE" ps
    ;;
  rebuild)
    docker compose -f "$COMPOSE_FILE" build --no-cache
    docker compose -f "$COMPOSE_FILE" up -d
    docker compose -f "$COMPOSE_FILE" ps
    ;;
  down|logs|ps)
    docker compose -f "$COMPOSE_FILE" "$@"
    ;;
  *)
    printf 'Usage: %s [up|rebuild|down|logs|ps]\n' "$0" >&2
    exit 2
    ;;
esac
