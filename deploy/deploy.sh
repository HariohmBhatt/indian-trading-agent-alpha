#!/usr/bin/env bash
# Deploy the production Docker stack.
#
# GitHub Actions calls this same command on the local self-hosted runner.
# It is also safe to run manually:
#
#   TRADING_AGENT_EXPECTED_REF=refs/heads/prod \
#   TRADING_AGENT_EXPECTED_SHA="$(git rev-parse HEAD)" \
#   ./deploy/deploy.sh
#
# Runtime secrets and host paths live outside the repository in:
#   ~/.config/indian-trading-agent/compose.env
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/deploy/docker-compose.prod.yml"
COMPOSE_ENV_FILE="${TRADING_AGENT_COMPOSE_ENV_FILE:-$HOME/.config/indian-trading-agent/compose.env}"
LOCK_FILE="${TRADING_AGENT_DEPLOY_LOCK_FILE:-/home/hariohm/.config/indian-trading-agent/deploy.lock}"
EXPECTED_REF="${TRADING_AGENT_EXPECTED_REF:-}"
EXPECTED_SHA="${TRADING_AGENT_EXPECTED_SHA:-}"

DRY_RUN=0
LOCK_ACQUIRED=0

log() { printf '[deploy] %s\n' "$*"; }
ok() { printf '[ok]     %s\n' "$*"; }
err() { printf '[err]    %s\n' "$*" >&2; }

usage() {
  cat <<'EOF'
Usage: deploy.sh [--dry-run]

Required environment:
  TRADING_AGENT_EXPECTED_REF  prod or refs/heads/prod
  TRADING_AGENT_EXPECTED_SHA  full 40-character commit SHA

Optional environment:
  TRADING_AGENT_COMPOSE_ENV_FILE  host-only Compose env file
  TRADING_AGENT_DEPLOY_LOCK_FILE  shared host-visible lock file
EOF
}

compose() {
  docker compose --env-file "$COMPOSE_ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

trim() {
  local value=$1

  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

# Read only the named, non-secret path settings without sourcing the file.
read_compose_env_value() {
  local key=$1
  local line name value

  while IFS= read -r line || [[ -n "$line" ]]; do
    line=$(trim "$line")
    [[ -z "$line" || ${line:0:1} == "#" ]] && continue

    if [[ "$line" == export[[:space:]]* ]]; then
      line=$(trim "${line#export}")
    fi
    [[ "$line" == *=* ]] || continue

    name=$(trim "${line%%=*}")
    [[ "$name" == "$key" ]] || continue

    value=$(trim "${line#*=}")
    case "$value" in
      \"*\")
        value=${value#\"}
        value=${value%\"}
        ;;
      \'*\')
        value=${value#\'}
        value=${value%\'}
        ;;
    esac
    printf '%s' "$value"
    return 0
  done < "$COMPOSE_ENV_FILE"

  return 1
}

# Match Compose's shell-environment-over-env-file precedence for path values.
compose_env_value() {
  local key=$1

  case "$key" in
    TRADING_AGENT_PROD_ENV_FILE)
      if [[ -n ${TRADING_AGENT_PROD_ENV_FILE+x} ]]; then
        printf '%s' "$TRADING_AGENT_PROD_ENV_FILE"
        return 0
      fi
      ;;
    TRADING_AGENT_PROD_DATA_DIR)
      if [[ -n ${TRADING_AGENT_PROD_DATA_DIR+x} ]]; then
        printf '%s' "$TRADING_AGENT_PROD_DATA_DIR"
        return 0
      fi
      ;;
    CLOUDFLARED_CONFIG_FILE)
      if [[ -n ${CLOUDFLARED_CONFIG_FILE+x} ]]; then
        printf '%s' "$CLOUDFLARED_CONFIG_FILE"
        return 0
      fi
      ;;
    CLOUDFLARED_CREDENTIALS_FILE)
      if [[ -n ${CLOUDFLARED_CREDENTIALS_FILE+x} ]]; then
        printf '%s' "$CLOUDFLARED_CREDENTIALS_FILE"
        return 0
      fi
      ;;
    *)
      err "Unsupported Compose env key: $key"
      return 1
      ;;
  esac

  read_compose_env_value "$key"
}

resolve_lock_file() {
  local configured_lock

  if [[ -n ${TRADING_AGENT_DEPLOY_LOCK_FILE+x} ]]; then
    return 0
  fi
  if [[ -r "$COMPOSE_ENV_FILE" ]] \
    && configured_lock=$(read_compose_env_value TRADING_AGENT_DEPLOY_LOCK_FILE) \
    && [[ -n "$configured_lock" ]]; then
    LOCK_FILE=$configured_lock
  fi
}

validate_compose_env_file() {
  if [[ ! -f "$COMPOSE_ENV_FILE" || ! -r "$COMPOSE_ENV_FILE" ]]; then
    err "Missing or unreadable Compose env file: $COMPOSE_ENV_FILE"
    err "Create it from deploy/compose.env.example before deploying."
    return 1
  fi

  if [[ ! -f "$COMPOSE_FILE" || ! -r "$COMPOSE_FILE" ]]; then
    err "Missing or unreadable Compose file: $COMPOSE_FILE"
    return 1
  fi
}

validate_referenced_path() {
  local label=$1
  local kind=$2
  local path=$3

  if [[ -z "$path" ]]; then
    err "$label is not set in $COMPOSE_ENV_FILE"
    return 1
  fi
  if [[ "$path" != /* ]]; then
    err "$label must be an absolute host path: $path"
    return 1
  fi

  case "$kind" in
    file)
      if [[ ! -f "$path" || ! -r "$path" ]]; then
        err "$label does not point to a readable file: $path"
        return 1
      fi
      ;;
    directory)
      if [[ ! -d "$path" || ! -r "$path" || ! -x "$path" ]]; then
        err "$label does not point to an accessible directory: $path"
        return 1
      fi
      ;;
    *)
      err "Unsupported path validation kind: $kind"
      return 1
      ;;
  esac

  ok "Validated $label."
}

validate_referenced_paths() {
  local had_error=0
  local key kind label path

  while IFS=: read -r key kind label; do
    if ! path=$(compose_env_value "$key"); then
      err "$key is not set in $COMPOSE_ENV_FILE"
      had_error=1
      continue
    fi
    if ! validate_referenced_path "$label" "$kind" "$path"; then
      had_error=1
    fi
  done <<'EOF'
TRADING_AGENT_PROD_ENV_FILE:file:TRADING_AGENT_PROD_ENV_FILE
TRADING_AGENT_PROD_DATA_DIR:directory:TRADING_AGENT_PROD_DATA_DIR
CLOUDFLARED_CONFIG_FILE:file:CLOUDFLARED_CONFIG_FILE
CLOUDFLARED_CREDENTIALS_FILE:file:CLOUDFLARED_CREDENTIALS_FILE
EOF

  if (( had_error != 0 )); then
    return 1
  fi
}

validate_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    err "Docker is required but was not found on PATH."
    return 1
  fi
  if ! docker compose version >/dev/null 2>&1; then
    err "Docker Compose v2 is required."
    return 1
  fi
  if ! docker info >/dev/null 2>&1; then
    err "The Docker daemon is unavailable or the current user cannot access it."
    return 1
  fi

  ok "Docker and Docker Compose are available."
}

validate_compose_config() {
  if ! compose config --quiet >/dev/null 2>&1; then
    err "docker compose config rejected the production configuration."
    return 1
  fi

  ok "Docker Compose configuration is valid."
}

validate_immutable_images() {
  local image
  local images

  images="$(compose config --images)"
  if [[ -z "$images" ]]; then
    err "Production Compose configuration contains no images."
    return 1
  fi

  while IFS= read -r image; do
    if [[ ! "$image" =~ @sha256:[0-9a-f]{64}$ ]]; then
      err "Production image is not digest-qualified: $image"
      return 1
    fi
  done <<< "$images"

  ok "Validated immutable production image references."
}

validate_preflight() {
  validate_compose_env_file || return 1
  validate_docker || return 1
  validate_referenced_paths || return 1
  validate_compose_config || return 1
  validate_immutable_images || return 1
}

validate_revision() {
  local actual_ref actual_sha current_branch expected_ref expected_sha

  if [[ -z "$EXPECTED_REF" ]]; then
    err "TRADING_AGENT_EXPECTED_REF is required and must identify prod."
    return 1
  fi
  case "$EXPECTED_REF" in
    prod|refs/heads/prod)
      expected_ref=refs/heads/prod
      ;;
    *)
      err "TRADING_AGENT_EXPECTED_REF must be prod or refs/heads/prod: $EXPECTED_REF"
      return 1
      ;;
  esac

  if [[ -z "$EXPECTED_SHA" ]]; then
    err "TRADING_AGENT_EXPECTED_SHA is required."
    return 1
  fi
  if [[ ! "$EXPECTED_SHA" =~ ^[[:xdigit:]]{40}$ ]]; then
    err "TRADING_AGENT_EXPECTED_SHA must be a full 40-character commit SHA."
    return 1
  fi
  expected_sha=${EXPECTED_SHA,,}

  if [[ ${GITHUB_ACTIONS:-} == true ]]; then
    actual_ref=${GITHUB_REF:-}
    if [[ -z "$actual_ref" ]]; then
      err "GITHUB_REF is missing in the GitHub Actions environment."
      return 1
    fi
  else
    if ! current_branch=$(git -C "$ROOT_DIR" symbolic-ref --quiet --short HEAD); then
      err "Manual deployment requires a checked-out branch; detached HEAD is not allowed."
      return 1
    fi
    actual_ref="refs/heads/$current_branch"
  fi

  if [[ "$actual_ref" != "$expected_ref" ]]; then
    err "Expected prod ref $expected_ref but found $actual_ref."
    return 1
  fi

  if ! actual_sha=$(git -C "$ROOT_DIR" rev-parse --verify 'HEAD^{commit}'); then
    err "Unable to resolve the checked-out commit SHA."
    return 1
  fi
  actual_sha=${actual_sha,,}
  if [[ "$actual_sha" != "$expected_sha" ]]; then
    err "Expected prod SHA $expected_sha but found $actual_sha."
    return 1
  fi

  ok "Validated prod ref $actual_ref at $actual_sha."
}

acquire_deployment_lock() {
  local lock_directory

  if ! command -v flock >/dev/null 2>&1; then
    err "flock is required for deployment serialization."
    return 1
  fi

  lock_directory=$(dirname -- "$LOCK_FILE")
  if [[ "$LOCK_FILE" != /* ]]; then
    err "Deployment lock file must be an absolute host path: $LOCK_FILE"
    return 1
  fi
  if [[ ! -d "$lock_directory" ]]; then
    err "Deployment lock directory does not exist: $lock_directory"
    return 1
  fi
  if ! exec 9>"$LOCK_FILE"; then
    err "Deployment lock file is not writable: $LOCK_FILE"
    return 1
  fi
  if ! flock -n 9; then
    err "Another production deployment is already running (lock: $LOCK_FILE)."
    exec 9>&-
    return 1
  fi

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

deploy_stack() {
  log "Pulling the immutable production Docker stack..."
  compose pull

  log "Starting the production Docker stack..."
  compose up -d --remove-orphans

  log "Waiting for application health..."
  for ((attempt = 1; attempt <= 30; attempt++)); do
    if compose exec -T backend python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)" \
      >/dev/null 2>&1 \
      && compose exec -T frontend node -e \
      "fetch('http://127.0.0.1:3000').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))" \
      >/dev/null 2>&1; then
      ok "Production containers are healthy."
      compose ps
      return 0
    fi
    if (( attempt == 30 )); then
      err "Production health checks failed."
      compose ps
      compose logs --no-color --tail=100 backend frontend cloudflared || true
      return 1
    fi
    sleep 2
  done
}

main() {
  case "$#" in
    0)
      ;;
    1)
      case "$1" in
        --dry-run)
          DRY_RUN=1
          ;;
        --help|-h)
          usage
          return 0
          ;;
        *)
          err "Unknown argument: $1"
          usage >&2
          return 2
          ;;
      esac
      ;;
    *)
      err "Expected no arguments or --dry-run."
      usage >&2
      return 2
      ;;
  esac

  cd "$ROOT_DIR"
  resolve_lock_file
  acquire_deployment_lock || return 1
  trap release_deployment_lock EXIT

  validate_revision || return 1
  validate_preflight || return 1

  if (( DRY_RUN == 1 )); then
    ok "Dry run complete; no production containers were started or replaced."
    return 0
  fi

  deploy_stack
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
