#!/usr/bin/env python3
"""Create an online SQLite backup locally, remotely, or both."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.backup import (
    BackupError,
    S3BackupStore,
    S3BackupTarget,
    backup_to_s3,
    online_backup,
    rotate_local_backups,
)
from backend.database_safety import DatabaseSafetyError


def _default_db_path() -> str:
    configured = os.getenv("TRADINGAGENTS_DB_PATH")
    if configured:
        return configured
    home = os.getenv(
        "TRADINGAGENTS_HOME",
        str(Path.home() / ".tradingagents"),
    )
    return str(Path(home) / "trading_agent.db")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create and rotate an online SQLite backup."
    )
    parser.add_argument(
        "--db-path",
        default=_default_db_path(),
        help="Absolute source SQLite path.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Absolute directory for a local backup artifact.",
    )
    parser.add_argument(
        "--s3",
        action="store_true",
        help="Publish to the S3 target configured by environment variables.",
    )
    parser.add_argument("--key", help="Explicit S3 object key.")
    parser.add_argument(
        "--keep",
        type=int,
        default=7,
        help="Number of local or remote backups to retain.",
    )
    parser.add_argument(
        "--no-wal-cleanup",
        action="store_true",
        help="Leave source WAL sidecars untouched after the backup.",
    )
    return parser


def _local_backup(
    source: str,
    output_dir: Path,
    *,
    cleanup_source_wal: bool,
) -> Path:
    if not output_dir.is_absolute():
        raise BackupError(
            f"local backup directory must be absolute: {output_dir!s}"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = output_dir / (
        f"trading-agent-{timestamp}-{uuid.uuid4().hex}.sqlite"
    )
    return online_backup(
        source,
        destination,
        cleanup_source_wal=cleanup_source_wal,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.output_dir and not args.s3:
        print(
            "choose --output-dir, --s3, or both",
            file=sys.stderr,
        )
        return 2
    if args.keep < 1:
        print("--keep must be at least one", file=sys.stderr)
        return 2

    try:
        cleanup_source_wal = not args.no_wal_cleanup
        result: dict[str, object] = {"status": "ok"}
        if args.output_dir:
            local_path = _local_backup(
                args.db_path,
                args.output_dir,
                cleanup_source_wal=cleanup_source_wal,
            )
            result["local_path"] = str(local_path)
            result["local_removed"] = [
                str(path)
                for path in rotate_local_backups(
                    args.output_dir,
                    keep=args.keep,
                )
            ]

            if args.s3:
                target = S3BackupTarget.from_env()
                store = S3BackupStore(target)
                result["remote"] = store.upload_file(
                    local_path,
                    key=args.key,
                )
                result["remote_removed"] = store.rotate(keep=args.keep)
        elif args.s3:
            result["remote"] = backup_to_s3(
                args.db_path,
                S3BackupTarget.from_env(),
                key=args.key,
                cleanup_source_wal=cleanup_source_wal,
            )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (DatabaseSafetyError, BackupError, ValueError) as exc:
        print(f"database backup failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
