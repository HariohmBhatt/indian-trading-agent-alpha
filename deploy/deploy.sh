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

LOCK_FILE="${TRADING_AGENT_DEPLOY_LOCK_FILE:-}"
EXPECTED_REF="${TRADING_AGENT_EXPECTED_REF:-}"
EXPECTED_SHA="${TRADING_AGENT_EXPECTED_SHA:-}"
PUBLIC_CHECK=0
DRY_RUN=0
LOCK_ACQUIRED=0
HEALTH_TIMEOUT="${TRADING_AGENT_HEALTH_TIMEOUT:-60}"

usage() {
  printf 'Usage: %s [--dry-run] [--public] [--compose-env-file FILE]\n' "$0"
}

while (($#)); do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
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

resolve_lock_file() {
  local configured_lock

  if [[ -z "$LOCK_FILE" ]]; then
    configured_lock="$(read_compose_env_value TRADING_AGENT_DEPLOY_LOCK_FILE)"
    LOCK_FILE="${configured_lock:-/home/hariohm/.config/indian-trading-agent/deploy.lock}"
  fi
}

configured_compose_value() {
  local key="$1"
  if [[ -v "$key" ]]; then
    printf '%s' "${!key}"
  else
    read_compose_env_value "$key"
  fi
}

validate_referenced_path() {
  local label="$1"
  local kind="$2"
  local path="$3"

  [[ -n "$path" ]] || die "$label is not set in $COMPOSE_ENV_FILE"
  [[ "$path" = /* ]] || die "$label must be an absolute host path: $path"

  case "$kind" in
    file)
      [[ -f "$path" && -r "$path" ]] ||
        die "$label does not point to a readable file: $path"
      ;;
    directory)
      [[ -d "$path" && -r "$path" && -x "$path" ]] ||
        die "$label does not point to an accessible directory: $path"
      ;;
    *)
      die "Unsupported path validation kind: $kind"
      ;;
  esac
}

validate_referenced_paths() {
  local key kind label path
  while IFS=: read -r key kind label; do
    path="$(configured_compose_value "$key")"
    validate_referenced_path "$label" "$kind" "$path"
  done <<'EOF'
TRADING_AGENT_PROD_ENV_FILE:file:TRADING_AGENT_PROD_ENV_FILE
TRADING_AGENT_PROD_DATA_DIR:directory:TRADING_AGENT_PROD_DATA_DIR
CLOUDFLARED_CONFIG_FILE:file:CLOUDFLARED_CONFIG_FILE
CLOUDFLARED_CREDENTIALS_FILE:file:CLOUDFLARED_CREDENTIALS_FILE
EOF
}

validate_immutable_images() {
  local image
  local images

  images="$(compose config --images)"
  [[ -n "$images" ]] || die "Production Compose configuration contains no images"
  while IFS= read -r image; do
    [[ "$image" =~ @sha256:[0-9a-f]{64}$ ]] ||
      die "Production image is not digest-qualified: $image"
  done <<< "$images"
}

validate_revision() {
  local actual_ref actual_sha current_branch expected_ref expected_sha

  [[ -n "$EXPECTED_REF" ]] ||
    die "TRADING_AGENT_EXPECTED_REF is required and must identify prod"
  case "$EXPECTED_REF" in
    prod|refs/heads/prod)
      expected_ref=refs/heads/prod
      ;;
    *)
      die "TRADING_AGENT_EXPECTED_REF must be prod or refs/heads/prod: $EXPECTED_REF"
      ;;
  esac

  [[ "$EXPECTED_SHA" =~ ^[[:xdigit:]]{40}$ ]] ||
    die "TRADING_AGENT_EXPECTED_SHA must be a full 40-character commit SHA"
  expected_sha="${EXPECTED_SHA,,}"

  if [[ "${GITHUB_ACTIONS:-}" == true ]]; then
    actual_ref="${GITHUB_REF:-}"
    [[ -n "$actual_ref" ]] || die "GITHUB_REF is missing in GitHub Actions"
  else
    current_branch="$(git -C "$ROOT_DIR" symbolic-ref --quiet --short HEAD)" ||
      die "Manual deployment requires a checked-out branch"
    actual_ref="refs/heads/$current_branch"
  fi
  [[ "$actual_ref" == "$expected_ref" ]] ||
    die "Expected prod ref $expected_ref but found $actual_ref"

  actual_sha="$(git -C "$ROOT_DIR" rev-parse --verify 'HEAD^{commit}')"
  actual_sha="${actual_sha,,}"
  [[ "$actual_sha" == "$expected_sha" ]] ||
    die "Expected prod SHA $expected_sha but found $actual_sha"
}

acquire_deployment_lock() {
  local lock_directory

  require_command flock
  [[ "$LOCK_FILE" = /* ]] ||
    die "Deployment lock file must be an absolute host path: $LOCK_FILE"
  lock_directory="$(dirname -- "$LOCK_FILE")"
  [[ -d "$lock_directory" ]] ||
    die "Deployment lock directory does not exist: $lock_directory"
  exec 9>"$LOCK_FILE" ||
    die "Deployment lock file is not writable: $LOCK_FILE"
  flock -n 9 ||
    die "Another production deployment is already running (lock: $LOCK_FILE)"
  LOCK_ACQUIRED=1
  log "Acquired deployment lock: $LOCK_FILE"
}

release_deployment_lock() {
  if (( LOCK_ACQUIRED == 1 )); then
    flock -u 9 || true
    exec 9>&-
    LOCK_ACQUIRED=0
  fi
}

require_command "$DOCKER_BIN"
require_command git
require_command jq
require_compose_env
resolve_lock_file
acquire_deployment_lock
trap release_deployment_lock EXIT

RELEASE_DIR="$(release_dir)"
ensure_release_dir "$RELEASE_DIR"

release_sha="${TRADING_AGENT_RELEASE_SHA:-${GITHUB_SHA:-}}"
if [[ -z "$release_sha" ]]; then
  release_sha="$(git -C "$ROOT_DIR" rev-parse HEAD)"
fi
require_sha "$release_sha"
export TRADING_AGENT_RELEASE_SHA="$release_sha"
validate_revision

last_known_good="$RELEASE_DIR/last-known-good.json"
if [[ -f "$last_known_good" ]]; then
  jq -e '.schema_version == 1 and (.release_sha | type == "string") and (.services | type == "object")' \
    "$last_known_good" >/dev/null ||
    die "Invalid last-known-good manifest: $last_known_good"
fi

cd "$ROOT_DIR"
validate_referenced_paths
"$DOCKER_BIN" info >/dev/null
compose config --quiet >/dev/null
validate_immutable_images

if (( DRY_RUN == 1 )); then
  ok "Dry run complete; no production containers were started or replaced."
  exit 0
fi

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

log "Pulling immutable production release $release_sha..."
if ! compose pull; then
  err "Production image pull failed before validation."
  automatic_rollback
  exit 1
fi

log "Starting production release $release_sha..."
if ! compose up -d --remove-orphans; then
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
