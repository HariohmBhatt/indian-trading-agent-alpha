#!/usr/bin/env bash
set -Eeuo pipefail

: "${RUNNER_REPO_URL:?RUNNER_REPO_URL is required}"

RUNNER_NAME="${RUNNER_NAME:-dellg15-prod-runner}"
RUNNER_LABELS="${RUNNER_LABELS:-trading-agent-prod}"
RUNNER_HOME="${RUNNER_HOME:-/home/runner}"
RUNNER_DIR="${RUNNER_DIR:-/opt/actions-runner}"

if [[ -S /var/run/docker.sock ]]; then
  docker_gid="$(stat -c '%g' /var/run/docker.sock)"
  if ! getent group "$docker_gid" >/dev/null 2>&1; then
    groupadd --gid "$docker_gid" host-docker
  fi
  docker_group="$(getent group "$docker_gid" | cut -d: -f1)"
  usermod --append --groups "$docker_group" runner
else
  echo "warning: /var/run/docker.sock is not mounted; deployments cannot run" >&2
fi

mkdir -p "$RUNNER_HOME/_work"
chown -R runner:runner "$RUNNER_HOME"

cd "$RUNNER_DIR"

# Keep the runner registration in the persistent home volume. Registration
# tokens expire quickly, but the registered runner can reconnect after a
# reboot without a new token.
if [[ ! -f .runner ]]; then
  : "${RUNNER_TOKEN:?RUNNER_TOKEN is required on the first start}"
  ./config.sh \
    --unattended \
    --url "$RUNNER_REPO_URL" \
    --token "$RUNNER_TOKEN" \
    --name "$RUNNER_NAME" \
    --labels "$RUNNER_LABELS" \
    --work "$RUNNER_HOME/_work" \
    --replace
fi

# Registration credentials must not reach the long-lived runner process.
unset RUNNER_TOKEN

chown -R runner:runner "$RUNNER_DIR"
exec runuser --user runner -- ./run.sh
