#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_SCRIPT="$ROOT_DIR/deploy/deploy.sh"
TEMP_DIR="$(mktemp -d)"
FAKE_BIN="$TEMP_DIR/bin"
DOCKER_LOG="$TEMP_DIR/docker.log"
LOCK_FILE="$TEMP_DIR/deploy.lock"
COMPOSE_ENV_FILE="$TEMP_DIR/compose.env"
EXPECTED_SHA="$(git -C "$ROOT_DIR" rev-parse HEAD)"

cleanup() {
  rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

mkdir -p "$FAKE_BIN" "$TEMP_DIR/data"
touch "$TEMP_DIR/prod.env" "$TEMP_DIR/cloudflared-config.yml" \
  "$TEMP_DIR/cloudflared-credentials.json" "$LOCK_FILE"

cat > "$COMPOSE_ENV_FILE" <<EOF
TRADING_AGENT_PROD_ENV_FILE=$TEMP_DIR/prod.env
TRADING_AGENT_PROD_DATA_DIR=$TEMP_DIR/data
CLOUDFLARED_CONFIG_FILE=$TEMP_DIR/cloudflared-config.yml
CLOUDFLARED_CREDENTIALS_FILE=$TEMP_DIR/cloudflared-credentials.json
TRADING_AGENT_DEPLOY_LOCK_FILE=$LOCK_FILE
EOF

cat > "$FAKE_BIN/docker" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

printf '%s\n' "$*" >> "${FAKE_DOCKER_LOG:?}"

case "${1:-}" in
  info)
    exit 0
    ;;
  compose)
    case " $* " in
      *" version "*) exit 0 ;;
      *" config --quiet "*) exit 0 ;;
      *" up "*) exit 42 ;;
    esac
    ;;
esac

exit 1
EOF
chmod +x "$FAKE_BIN/docker"

COMMON_ENV=(
  "PATH=$FAKE_BIN:$PATH"
  "FAKE_DOCKER_LOG=$DOCKER_LOG"
  "TRADING_AGENT_DEPLOY_LOCK_FILE=$LOCK_FILE"
  "TRADING_AGENT_EXPECTED_REF=refs/heads/prod"
)

assert_contains() {
  local output=$1
  local expected=$2

  case "$output" in
    *"$expected"*) ;;
    *)
      printf 'Expected output to contain: %s\n%s\n' "$expected" "$output" >&2
      return 1
      ;;
  esac
}

run_deploy() {
  local compose_env=$1
  local actual_ref=$2
  local expected_sha=$3

  env "${COMMON_ENV[@]}" \
    "TRADING_AGENT_COMPOSE_ENV_FILE=$compose_env" \
    "TRADING_AGENT_EXPECTED_SHA=$expected_sha" \
    "GITHUB_ACTIONS=true" \
    "GITHUB_REF=$actual_ref" \
    bash "$DEPLOY_SCRIPT" --dry-run
}

if ! output=$(run_deploy "$COMPOSE_ENV_FILE" refs/heads/prod "$EXPECTED_SHA" 2>&1); then
  printf 'Dry-run validation failed:\n%s\n' "$output" >&2
  exit 1
fi
assert_contains "$output" "Dry run complete; no production containers were started"
if [[ "$(tr '\n' ' ' < "$DOCKER_LOG")" == *" up "* ]]; then
  printf 'Dry-run unexpectedly called docker compose up.\n' >&2
  exit 1
fi

if output=$(run_deploy "$COMPOSE_ENV_FILE" refs/heads/main "$EXPECTED_SHA" 2>&1); then
  printf 'Wrong-ref validation unexpectedly succeeded.\n' >&2
  exit 1
fi
assert_contains "$output" "Expected prod ref refs/heads/prod but found refs/heads/main"

if output=$(env -u GITHUB_ACTIONS -u GITHUB_REF \
  "${COMMON_ENV[@]}" \
  "TRADING_AGENT_COMPOSE_ENV_FILE=$COMPOSE_ENV_FILE" \
  "TRADING_AGENT_EXPECTED_SHA=$EXPECTED_SHA" \
  bash "$DEPLOY_SCRIPT" --dry-run 2>&1); then
  printf 'Manual wrong-ref validation unexpectedly succeeded.\n' >&2
  exit 1
fi
assert_contains "$output" "Expected prod ref refs/heads/prod but found refs/heads/"

MISSING_PATH_ENV_FILE="$TEMP_DIR/missing-path.env"
cat > "$MISSING_PATH_ENV_FILE" <<EOF
TRADING_AGENT_PROD_ENV_FILE=$TEMP_DIR/no-prod.env
TRADING_AGENT_PROD_DATA_DIR=$TEMP_DIR/data
CLOUDFLARED_CONFIG_FILE=$TEMP_DIR/cloudflared-config.yml
CLOUDFLARED_CREDENTIALS_FILE=$TEMP_DIR/cloudflared-credentials.json
EOF
if output=$(run_deploy "$MISSING_PATH_ENV_FILE" refs/heads/prod "$EXPECTED_SHA" 2>&1); then
  printf 'Missing-path validation unexpectedly succeeded.\n' >&2
  exit 1
fi
assert_contains "$output" "TRADING_AGENT_PROD_ENV_FILE does not point to a readable file"

if output=$(run_deploy "$COMPOSE_ENV_FILE" refs/heads/prod \
  0000000000000000000000000000000000000000 2>&1); then
  printf 'Wrong-SHA validation unexpectedly succeeded.\n' >&2
  exit 1
fi
assert_contains "$output" "Expected prod SHA"

exec {held_lock_fd}>"$LOCK_FILE"
flock -n "$held_lock_fd"
if output=$(run_deploy "$COMPOSE_ENV_FILE" refs/heads/prod "$EXPECTED_SHA" 2>&1); then
  printf 'Lock-contention validation unexpectedly succeeded.\n' >&2
  exit 1
fi
assert_contains "$output" "Another production deployment is already running"
flock -u "$held_lock_fd"
exec {held_lock_fd}>&-

printf 'deployment control tests passed\n'
