#!/usr/bin/env bash
# Deploy the production Docker stack behind readiness, smoke, and identity
# gates. A failed deployment is automatically rolled back to the previous
# last-known-good image manifest when one exists.
set -Eeuo pipefail
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/deploy/docker-compose.prod.yml"
COMPOSE_ENV_FILE="${TRADING_AGENT_COMPOSE_ENV_FILE:-$HOME/.config/indian-trading-agent/compose.env}"
source "$ROOT_DIR/deploy/lib.sh"

PUBLIC_CHECK=0
HEALTH_TIMEOUT="${TRADING_AGENT_HEALTH_TIMEOUT:-60}"

usage() {
  printf 'Usage: %s [--public] [--compose-env-file FILE]\n' "$0"
}

while (($#)); do
  case "$1" in
    --public)
      PUBLIC_CHECK=1
      shift
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

[[ "$HEALTH_TIMEOUT" =~ ^[0-9]+$ && "$HEALTH_TIMEOUT" -gt 0 ]] ||
  die "TRADING_AGENT_HEALTH_TIMEOUT must be a positive integer"

require_command "$DOCKER_BIN"
require_command git
require_command jq
require_compose_env

RELEASE_DIR="$(release_dir)"
ensure_release_dir "$RELEASE_DIR"

release_sha="${TRADING_AGENT_RELEASE_SHA:-${GITHUB_SHA:-}}"
if [[ -z "$release_sha" ]]; then
  release_sha="$(git -C "$ROOT_DIR" rev-parse HEAD)"
fi
require_sha "$release_sha"
export TRADING_AGENT_RELEASE_SHA="$release_sha"

last_known_good="$RELEASE_DIR/last-known-good.json"
if [[ -f "$last_known_good" ]]; then
  jq -e '.schema_version == 1 and (.release_sha | type == "string") and (.services | type == "object")' \
    "$last_known_good" >/dev/null ||
    die "Invalid last-known-good manifest: $last_known_good"
fi

cd "$ROOT_DIR"
compose config --quiet >/dev/null
log "Building and starting production release $release_sha..."

automatic_rollback() {
  if [[ -f "$last_known_good" ]]; then
    err "Attempting automatic rollback to the last-known-good manifest."
    if "$ROOT_DIR/deploy/rollback.sh" --apply \
      --manifest "$last_known_good" \
      --compose-env-file "$COMPOSE_ENV_FILE"; then
      ok "Automatic rollback completed; database state was retained."
    else
      err "Automatic rollback failed; inspect the stack without changing its data volume."
    fi
  else
    err "No last-known-good manifest exists; no image rollback was attempted."
  fi
  compose ps || true
}

if ! compose up -d --build --remove-orphans; then
  err "Production stack update failed before validation."
  automatic_rollback
  exit 1
fi

validate_args=(
  --compose-env-file "$COMPOSE_ENV_FILE"
  --expected-sha "$release_sha"
  --quiet
)
if (( PUBLIC_CHECK == 1 )); then
  validate_args+=(--public)
fi

validated=0
attempts="$(( (HEALTH_TIMEOUT + 1) / 2 ))"
log "Waiting for readiness, tunnel, and release identity gates..."
for ((attempt = 1; attempt <= attempts; attempt++)); do
  if "$ROOT_DIR/deploy/validate-prod.sh" "${validate_args[@]}"; then
    validated=1
    break
  fi
  sleep 2
done

if (( validated == 0 )); then
  err "Production validation failed for release $release_sha."
  automatic_rollback
  exit 1
fi

service_manifest() {
  local service="$1"
  local id image_ref image_digest image_revision

  id="$(container_id "$service")"
  [[ -n "$id" ]] || die "Cannot identify the running $service container"
  image_ref="$(docker_inspect --format '{{.Config.Image}}' "$id")"
  image_digest="$(docker_inspect --format '{{.Image}}' "$id")"
  [[ "$image_digest" =~ ^sha256:[[:xdigit:]]+$ ]] ||
    die "$service did not report a content-addressed image identity"
  image_revision="$(
    docker_inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$id" 2>/dev/null ||
      true
  )"
  [[ -n "$image_revision" && "$image_revision" != "<no value>" ]] || image_revision="unknown"

  jq -n \
    --arg image_ref "$image_ref" \
    --arg image_digest "$image_digest" \
    --arg image_revision "$image_revision" \
    '{image_ref: $image_ref, image_digest: $image_digest, image_revision: $image_revision}'
}

manifest_tmp="$(mktemp "$RELEASE_DIR/.manifest.XXXXXX")"
trap 'rm -f "${manifest_tmp:-}"' EXIT

backend_json="$(service_manifest backend)"
frontend_json="$(service_manifest frontend)"
cloudflared_json="$(service_manifest cloudflared)"
jq -n \
  --arg release_sha "$release_sha" \
  --arg deployed_at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  --arg compose_file "deploy/docker-compose.prod.yml" \
  --argjson backend "$backend_json" \
  --argjson frontend "$frontend_json" \
  --argjson cloudflared "$cloudflared_json" \
  '{
    schema_version: 1,
    release_sha: $release_sha,
    deployed_at: $deployed_at,
    compose_file: $compose_file,
    services: {
      backend: $backend,
      frontend: $frontend,
      cloudflared: $cloudflared
    }
  }' >"$manifest_tmp"
chmod 600 "$manifest_tmp"

atomic_install() {
  local source="$1"
  local destination="$2"
  local temporary="${destination}.tmp.$$"
  install -m 600 "$source" "$temporary"
  mv -f "$temporary" "$destination"
}

history_manifest="$RELEASE_DIR/history/${release_sha}.json"
atomic_install "$manifest_tmp" "$history_manifest"
atomic_install "$manifest_tmp" "$RELEASE_DIR/current.json"
atomic_install "$manifest_tmp" "$last_known_good"
rm -f "$manifest_tmp"
trap - EXIT

ok "Production release $release_sha passed all gates."
ok "Last-known-good manifest retained at $last_known_good"
compose ps
