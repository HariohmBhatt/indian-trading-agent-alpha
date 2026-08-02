#!/usr/bin/env python3
"""Upgrade the configured SQLite database transactionally."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.database_safety import DatabaseNotFound, DatabaseSafetyError
from backend.migrations import (
    CURRENT_SCHEMA_VERSION,
    MigrationError,
    get_schema_version,
    upgrade_database,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run versioned SQLite migrations."
    )
    parser.add_argument(
        "--db-path",
        default=os.getenv("TRADINGAGENTS_DB_PATH"),
        help="Absolute SQLite path (defaults to TRADINGAGENTS_DB_PATH).",
    )
    parser.add_argument(
        "--target-version",
        type=int,
        default=CURRENT_SCHEMA_VERSION,
        help="Target schema version (defaults to the current version).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db_path = args.db_path
    if not db_path:
        home = os.getenv(
            "TRADINGAGENTS_HOME",
            str(Path.home() / ".tradingagents"),
        )
        db_path = str(Path(home) / "trading_agent.db")
    try:
        try:
            previous_version = get_schema_version(db_path)
        except DatabaseNotFound:
            previous_version = 0
        version = upgrade_database(
            db_path,
            target_version=args.target_version,
        )
        print(
            json.dumps(
                {
                    "status": "ok",
                    "db_path": str(Path(db_path).expanduser()),
                    "schema_version": version,
                    "previous_version": previous_version,
                },
                sort_keys=True,
            )
        )
        return 0
    except (DatabaseSafetyError, MigrationError, ValueError) as exc:
        print(f"database migration failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
