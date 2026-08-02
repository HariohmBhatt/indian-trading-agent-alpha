#!/usr/bin/env bash
#
# Validate the production stack without changing it.
#
# Local container probes always run. Public probes are opt-in:
#
#   ./deploy/validate-prod.sh --public
#
# Public Cloudflare Access credentials must be supplied through host-only,
# mode-600 files:
#   TRADING_AGENT_ACCESS_CLIENT_ID_FILE
#   TRADING_AGENT_ACCESS_CLIENT_SECRET_FILE
set -Eeuo pipefail
# Do not allow a caller's inherited xtrace setting to expose Access secrets.
set +x
umask 077

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/deploy/docker-compose.prod.yml"
COMPOSE_ENV_FILE="${TRADING_AGENT_COMPOSE_ENV_FILE:-$HOME/.config/indian-trading-agent/compose.env}"
source "$ROOT_DIR/deploy/lib.sh"

PUBLIC_CHECK=0
QUIET=0
EXPECTED_SHA="${TRADING_AGENT_RELEASE_SHA:-}"
MANIFEST=""

usage() {
  printf 'Usage: %s [--public] [--expected-sha SHA] [--manifest FILE] [--compose-env-file FILE] [--quiet]\n' "$0"
}

while (($#)); do
  case "$1" in
    --public)
      PUBLIC_CHECK=1
      shift
      ;;
    --expected-sha)
      [[ $# -ge 2 ]] || die "--expected-sha requires a value"
      EXPECTED_SHA="$2"
      shift 2
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
    --quiet)
      QUIET=1
      shift
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

if [[ -z "$EXPECTED_SHA" ]]; then
  EXPECTED_SHA="$(git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null || true)"
fi
[[ -n "$EXPECTED_SHA" ]] || die "Cannot determine the expected release SHA"
require_sha "$EXPECTED_SHA"

require_command "$DOCKER_BIN"
require_command curl
require_command jq
require_compose_env

if [[ -n "$MANIFEST" ]]; then
  [[ -f "$MANIFEST" ]] || die "Manifest not found: $MANIFEST"
  jq -e '.schema_version == 1 and (.release_sha | type == "string") and (.services | type == "object")' \
    "$MANIFEST" >/dev/null || die "Invalid release manifest: $MANIFEST"
  manifest_sha="$(jq -er '.release_sha' "$MANIFEST")"
  [[ "$manifest_sha" == "$EXPECTED_SHA" ]] || die "Manifest release SHA does not match expected SHA"
fi

compose config --quiet >/dev/null

report() {
  (( QUIET == 1 )) || log "$*"
}

check_container() {
  local service="$1"
  local id state health

  id="$(container_id "$service")"
  [[ -n "$id" ]] || die "$service container is not running"
  state="$(docker_inspect --format '{{.State.Status}}' "$id")"
  [[ "$state" == "running" ]] || die "$service container state is $state"
  health="$(docker_inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id")"
  [[ "$health" == "healthy" ]] || die "$service container health is $health"
}

check_release_identity() {
  local service="$1"
  local id runtime_sha image_digest image_revision

  id="$(container_id "$service")"
  runtime_sha="$(
    docker_inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$id" |
      awk -F= '$1 == "TRADING_AGENT_RELEASE_SHA" { print substr($0, index($0, "=") + 1); exit }'
  )"
  [[ "$runtime_sha" == "$EXPECTED_SHA" ]] ||
    die "$service runtime release identity does not match expected SHA"

  image_digest="$(docker_inspect --format '{{.Image}}' "$id")"
  [[ "$image_digest" =~ ^sha256:[[:xdigit:]]+$ ]] ||
    die "$service image identity is not a content digest"

  image_revision="$(
    docker_inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$id" 2>/dev/null ||
      true
  )"
  if [[ -n "$image_revision" && "$image_revision" != "<no value>" ]]; then
    [[ "$image_revision" == "$EXPECTED_SHA" ]] ||
      die "$service image revision label does not match expected SHA"
  fi

  if [[ -n "$MANIFEST" ]]; then
    manifest_digest="$(manifest_service_digest "$MANIFEST" "$service")"
    [[ "$manifest_digest" == "$image_digest" ]] ||
      die "$service image digest does not match the supplied manifest"
  fi

  report "$service release identity verified"
}

check_backend() {
  compose exec -T backend python -c \
    "import urllib.request; [urllib.request.urlopen('http://127.0.0.1:8000' + path, timeout=3).read() for path in ('/api/health', '/api/ready')]" \
    >/dev/null
  report "backend liveness and DB readiness passed"
}

check_frontend() {
  compose exec -T frontend node -e \
    "fetch('http://127.0.0.1:3000/health').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))" \
    >/dev/null
  report "frontend health route passed"
}

metrics_port="${CLOUDFLARED_METRICS_PORT:-}"
if [[ -z "$metrics_port" ]]; then
  metrics_port="$(read_compose_env_value CLOUDFLARED_METRICS_PORT)"
fi
metrics_port="${metrics_port:-20241}"
[[ "$metrics_port" =~ ^[0-9]+$ ]] || die "Invalid CLOUDFLARED_METRICS_PORT"

check_tunnel() {
  compose exec -T cloudflared cloudflared tunnel --metrics 127.0.0.1:2000 ready >/dev/null
  curl --fail --silent --show-error --max-time 5 \
    "http://127.0.0.1:${metrics_port}/ready" >/dev/null

  local metrics
  metrics="$(curl --fail --silent --show-error --max-time 5 \
    "http://127.0.0.1:${metrics_port}/metrics")"
  awk '/^cloudflared_/ { found = 1 } END { exit found ? 0 : 1 }' <<<"$metrics" ||
    die "Cloudflared metrics endpoint returned no cloudflared metrics"
  report "cloudflared readiness and metrics passed"
}

read_credential() {
  local file="$1"
  local value mode

  [[ "$file" = /* ]] || die "Public credential paths must be absolute host paths"
  case "$file" in
    "$ROOT_DIR"|"$ROOT_DIR"/*)
      die "Public credential files must stay outside the repository"
      ;;
  esac
  [[ -f "$file" ]] || die "Public credential file not found"
  mode="$(stat -c '%a' "$file")"
  (( (8#$mode & 077) == 0 )) || die "Public credential file must not be group/world-readable"
  value="$(<"$file")"
  [[ -n "$value" ]] || die "Public credential file is empty"
  case "$value" in
    *\"*|*\\*|*$'\n'*|*$'\r'*)
      die "Public credential contains unsupported characters"
      ;;
  esac
  printf '%s' "$value"
}

public_request() {
  local base_url="$1"
  local path="$2"
  local curl_config="$3"
  local body_file status

  body_file="$(mktemp "${TMPDIR:-/tmp}/indian-trading-agent-smoke.XXXXXX")"
  status="$(
    curl --config "$curl_config" --fail --silent --show-error --location \
      --max-time 10 --output "$body_file" --write-out '%{http_code}' \
      "${base_url}${path}"
  )" || {
    rm -f "$body_file"
    die "Public smoke request failed for $path"
  }
  rm -f "$body_file"
  [[ "$status" == "200" ]] || die "Public smoke request returned HTTP $status for $path"
}

configured_value() {
  local name="$1"
  local value=""

  if [[ -v "$name" ]]; then
    value="${!name}"
  fi
  if [[ -z "$value" ]]; then
    value="$(read_compose_env_value "$name")"
  fi
  printf '%s' "$value"
}

check_public() {
  local base_url id_file secret_file bearer_file
  local id="" secret="" bearer="" curl_config

  base_url="$(configured_value TRADING_AGENT_PUBLIC_BASE_URL)"
  [[ -n "$base_url" ]] || base_url="$(configured_value PROD_PUBLIC_BASE_URL)"
  id_file="$(configured_value TRADING_AGENT_ACCESS_CLIENT_ID_FILE)"
  [[ -n "$id_file" ]] || id_file="$(configured_value CLOUDFLARE_ACCESS_CLIENT_ID_FILE)"
  secret_file="$(configured_value TRADING_AGENT_ACCESS_CLIENT_SECRET_FILE)"
  [[ -n "$secret_file" ]] || secret_file="$(configured_value CLOUDFLARE_ACCESS_CLIENT_SECRET_FILE)"
  bearer_file="$(configured_value TRADING_AGENT_PUBLIC_TOKEN_FILE)"

  [[ -n "$base_url" ]] || die "Public smoke checks require TRADING_AGENT_PUBLIC_BASE_URL"
  [[ "$base_url" == http://* || "$base_url" == https://* ]] ||
    die "TRADING_AGENT_PUBLIC_BASE_URL must use HTTP(S)"

  if [[ -n "$id_file" || -n "$secret_file" ]]; then
    [[ -n "$id_file" && -n "$secret_file" ]] ||
      die "Cloudflare Access client ID and secret files must be provided together"
    id="$(read_credential "$id_file")"
    secret="$(read_credential "$secret_file")"
  elif [[ -n "$bearer_file" ]]; then
    bearer="$(read_credential "$bearer_file")"
  else
    die "Public smoke checks require host-only Access credentials"
  fi

  curl_config="$(mktemp "${TMPDIR:-/tmp}/indian-trading-agent-curl.XXXXXX")"
  chmod 600 "$curl_config"
  trap 'rm -f "${curl_config:-}"' EXIT
  if [[ -n "$id" ]]; then
    printf 'header = "CF-Access-Client-Id: %s"\n' "$id" >"$curl_config"
    printf 'header = "CF-Access-Client-Secret: %s"\n' "$secret" >>"$curl_config"
  else
    printf 'header = "Authorization: Bearer %s"\n' "$bearer" >"$curl_config"
  fi

  base_url="${base_url%/}"
  public_request "$base_url" "/health" "$curl_config"
  public_request "$base_url" "/api/health" "$curl_config"
  public_request "$base_url" "/api/ready" "$curl_config"
  rm -f "$curl_config"
  trap - EXIT
  ok "Access-aware public smoke checks passed"
}

report "checking healthy containers"
check_container backend
check_container frontend
check_container cloudflared
check_release_identity backend
check_release_identity frontend
check_backend
check_frontend
check_tunnel

if (( PUBLIC_CHECK == 1 )); then
  check_public
else
  report "public smoke checks skipped (use --public with host-only credentials)"
fi

ok "Production validation passed for release $EXPECTED_SHA"
