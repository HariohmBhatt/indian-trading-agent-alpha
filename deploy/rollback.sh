#!/usr/bin/env bash
#
# Roll back application containers to a previously validated image manifest.
#
# The default is a dry run. Apply explicitly:
#   ./deploy/rollback.sh --apply
#
# This script never removes or replaces the persistent data directory. A
# rollback changes image identities only; database state and schema changes
# are deliberately not reverted automatically.
set -Eeuo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/deploy/docker-compose.prod.yml"
COMPOSE_ENV_FILE="${TRADING_AGENT_COMPOSE_ENV_FILE:-$HOME/.config/indian-trading-agent/compose.env}"
source "$ROOT_DIR/deploy/lib.sh"

APPLY=0
PUBLIC_CHECK=0
MANIFEST=""

usage() {
  printf 'Usage: %s [--dry-run|--apply] [--public] [--manifest FILE] [--compose-env-file FILE]\n' "$0"
}

while (($#)); do
  case "$1" in
    --dry-run)
      APPLY=0
      shift
      ;;
    --apply)
      APPLY=1
      shift
      ;;
    --public)
      PUBLIC_CHECK=1
      shift
      ;;
    --manifest)
      [[ $# -ge 2 ]] || die "--manifest requires a path"
      MANIFEST="$2"
      shift 2
      ;;
    --compose-env-file)
      [[ $# -ge 2 ]] || die "--compose-env-file requires a path"
      COMPOSE_ENV_FILE="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "Unknown option: $1"
      ;;
  esac
done

require_command "$DOCKER_BIN"
require_command jq
require_compose_env

RELEASE_DIR="$(release_dir)"
if [[ -z "$MANIFEST" ]]; then
  MANIFEST="$RELEASE_DIR/last-known-good.json"
fi
[[ -f "$MANIFEST" ]] || die "Last-known-good manifest not found: $MANIFEST"
jq -e '.schema_version == 1 and (.release_sha | type == "string") and (.services | type == "object")' \
  "$MANIFEST" >/dev/null || die "Invalid release manifest: $MANIFEST"

release_sha="$(jq -er '.release_sha' "$MANIFEST")"
require_sha "$release_sha"

backend_digest="$(manifest_service_digest "$MANIFEST" backend)"
frontend_digest="$(manifest_service_digest "$MANIFEST" frontend)"
cloudflared_digest="$(jq -er '.services.cloudflared.image_digest // empty' "$MANIFEST" 2>/dev/null || true)"
[[ -n "$cloudflared_digest" ]] || log "Manifest has no cloudflared digest; preserving configured tunnel image"

for digest in "$backend_digest" "$frontend_digest"; do
  [[ "$digest" =~ ^sha256:[[:xdigit:]]+$ ]] ||
    die "Manifest contains a non-content-digest image identity"
done
if [[ -n "$cloudflared_digest" ]]; then
  [[ "$cloudflared_digest" =~ ^sha256:[[:xdigit:]]+$ ]] ||
    die "Manifest contains a non-content-digest cloudflared identity"
fi

backend_tag="indian-trading-agent-backend:rollback-${release_sha}"
frontend_tag="indian-trading-agent-frontend:rollback-${release_sha}"
cloudflared_tag="cloudflare/cloudflared:rollback-${release_sha}"

log "Rollback target release: $release_sha"
log "Backend image digest: $backend_digest"
log "Frontend image digest: $frontend_digest"
if [[ -n "$cloudflared_digest" ]]; then
  log "Cloudflared image digest: $cloudflared_digest"
fi

if (( APPLY == 0 )); then
  ok "Dry run complete; no containers, images, or database state changed."
  exit 0
fi

ensure_release_dir "$RELEASE_DIR"

"$DOCKER_BIN" image inspect "$backend_digest" >/dev/null ||
  die "Previous backend image digest is not available locally"
"$DOCKER_BIN" image inspect "$frontend_digest" >/dev/null ||
  die "Previous frontend image digest is not available locally"
if [[ -n "$cloudflared_digest" ]]; then
  "$DOCKER_BIN" image inspect "$cloudflared_digest" >/dev/null ||
    die "Previous cloudflared image digest is not available locally"
fi

"$DOCKER_BIN" tag "$backend_digest" "$backend_tag"
"$DOCKER_BIN" tag "$frontend_digest" "$frontend_tag"
if [[ -n "$cloudflared_digest" ]]; then
  "$DOCKER_BIN" tag "$cloudflared_digest" "$cloudflared_tag"
fi

export TRADING_AGENT_RELEASE_SHA="$release_sha"
export TRADING_AGENT_PROD_BACKEND_IMAGE="$backend_tag"
export TRADING_AGENT_PROD_FRONTEND_IMAGE="$frontend_tag"
if [[ -n "$cloudflared_digest" ]]; then
  export CLOUDFLARED_IMAGE="$cloudflared_tag"
fi

compose config --quiet >/dev/null
compose up -d --no-build --remove-orphans

validate_args=(
  --compose-env-file "$COMPOSE_ENV_FILE"
  --expected-sha "$release_sha"
  --manifest "$MANIFEST"
)
if (( PUBLIC_CHECK == 1 )); then
  validate_args+=(--public)
fi
"$ROOT_DIR/deploy/validate-prod.sh" "${validate_args[@]}"

current_tmp="$(mktemp "$RELEASE_DIR/current.json.XXXXXX")"
jq --arg applied_at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  '. + {rollback_applied_at: $applied_at}' "$MANIFEST" >"$current_tmp"
chmod 600 "$current_tmp"
mv -f "$current_tmp" "$RELEASE_DIR/current.json"

ok "Rollback applied to release $release_sha; persistent database state was retained."
