#!/usr/bin/env bash
# Run the Phase 10 operator checklist without deploying or changing a host.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHECKER="$ROOT_DIR/scripts/recovery_acceptance.py"

if ! command -v python3 >/dev/null 2>&1; then
  printf '[DEFERRED] python3 is required for recovery acceptance\n' >&2
  exit 2
fi

for argument in "$@"; do
  case "$argument" in
    --apply|--target-dir|--i-understand-disposable|--allow-existing-disposable)
      printf '[FAIL] this operator wrapper is read-only; run the restore drill directly against a disposable target\n' >&2
      exit 1
      ;;
  esac
done

exec python3 "$CHECKER" operator --repo-root "$ROOT_DIR" "$@"
