#!/usr/bin/env bash

# Shared, non-secret deployment helpers. This file is sourced by the deploy
# scripts and must not be executed directly.

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/deploy/docker-compose.prod.yml}"
COMPOSE_ENV_FILE="${COMPOSE_ENV_FILE:-${TRADING_AGENT_COMPOSE_ENV_FILE:-$HOME/.config/indian-trading-agent/compose.env}}"
DOCKER_BIN="${DOCKER_BIN:-docker}"

log() { printf '[deploy] %s\n' "$*"; }
ok() { printf '[ok]     %s\n' "$*"; }
err() { printf '[err]    %s\n' "$*" >&2; }

die() {
  err "$*"
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

require_compose_env() {
  [[ -f "$COMPOSE_ENV_FILE" ]] || die "Missing $COMPOSE_ENV_FILE; create it from deploy/compose.env.example."
}

compose() {
  "$DOCKER_BIN" compose --env-file "$COMPOSE_ENV_FILE" -f "$COMPOSE_FILE" "$@"
}

docker_inspect() {
  "$DOCKER_BIN" inspect "$@"
}

read_compose_env_value() {
  local key="$1"
  local line value

  [[ -f "$COMPOSE_ENV_FILE" ]] || return 0
  line="$(awk -v key="$key" '
    $0 !~ /^[[:space:]]*#/ && $0 ~ "^[[:space:]]*" key "[[:space:]]*=" {
      print
      exit
    }
  ' "$COMPOSE_ENV_FILE")"
  [[ -n "$line" ]] || return 0

  value="${line#*=}"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  if [[ "$value" == \"*\" && "$value" == *\" ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "$value" == \'*\' && "$value" == *\' ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "$value"
}

release_dir() {
  if [[ -n "${TRADING_AGENT_RELEASE_DIR:-}" ]]; then
    printf '%s' "$TRADING_AGENT_RELEASE_DIR"
    return
  fi

  local configured
  configured="$(read_compose_env_value TRADING_AGENT_RELEASE_DIR)"
  if [[ -n "$configured" ]]; then
    printf '%s' "$configured"
  else
    printf '%s/.local/state/indian-trading-agent' "$HOME"
  fi
}

ensure_release_dir() {
  local dir="$1"
  [[ "$dir" = /* ]] || die "Release manifests must use an absolute host path: $dir"
  case "$dir" in
    "$ROOT_DIR"|"$ROOT_DIR"/*)
      die "Release manifests must live outside the repository: $dir"
      ;;
  esac
  mkdir -p "$dir/history"
  chmod 700 "$dir"
}

container_id() {
  local service="$1"
  compose ps -q "$service" | awk 'NF { print; exit }'
}

require_sha() {
  local sha="$1"
  [[ "$sha" =~ ^[[:alnum:]_.-]{7,128}$ ]] || die "Invalid release SHA: $sha"
}

manifest_service_digest() {
  local manifest="$1"
  local service="$2"
  jq -er --arg service "$service" '.services[$service].image_digest' "$manifest"
}

manifest_service_ref() {
  local manifest="$1"
  local service="$2"
  jq -er --arg service "$service" '.services[$service].image_ref' "$manifest"
}
