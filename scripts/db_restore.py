#!/usr/bin/env python3
"""Restore one local or S3-versioned SQLite backup atomically."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.backup import (
    BackupError,
    S3BackupStore,
    S3BackupTarget,
    restore_database,
    restore_from_s3,
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
        description="Restore a validated SQLite backup."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--input",
        type=Path,
        help="Local SQLite backup artifact.",
    )
    source.add_argument(
        "--s3-key",
        help="S3 object key to restore.",
    )
    parser.add_argument(
        "--version-id",
        help="S3 object version ID (required when selecting a historical version).",
    )
    parser.add_argument(
        "--db-path",
        default=_default_db_path(),
        help="Absolute destination SQLite path.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version_id and not args.s3_key:
        print("--version-id requires --s3-key", file=sys.stderr)
        return 2
    try:
        if args.input:
            restored = restore_database(args.input, args.db_path)
        else:
            store = S3BackupStore(S3BackupTarget.from_env())
            restored = restore_from_s3(
                store,
                args.s3_key,
                args.db_path,
                version_id=args.version_id,
            )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "restored_path": str(restored),
                },
                sort_keys=True,
            )
        )
        return 0
    except (DatabaseSafetyError, BackupError, ValueError) as exc:
        print(f"database restore failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
