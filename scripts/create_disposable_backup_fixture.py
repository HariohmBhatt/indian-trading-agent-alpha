#!/usr/bin/env python3
"""Create a secret-free SQLite backup fixture for the Phase 10 restore drill."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_created_at(value: str) -> str:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--created-at must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat()


def safe_output(path: Path) -> None:
    resolved = path.expanduser().resolve(strict=False)
    protected = {
        (Path.home() / ".tradingagents").resolve(strict=False),
        (Path.home() / ".tradingagents-prod").resolve(strict=False),
    }
    if resolved in protected or any(item in resolved.parents for item in protected):
        raise ValueError("fixture output cannot be inside a production data directory")
    if resolved.exists():
        raise ValueError(f"fixture output already exists: {resolved}")


def create_fixture(output: Path, created_at: str) -> None:
    safe_output(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="phase10-fixture-") as work_dir:
        root = Path(work_dir)
        database = root / "data/trading_agent.db"
        database.parent.mkdir(parents=True)
        connection = sqlite3.connect(database)
        try:
            connection.execute(
                "CREATE TABLE recovery_fixture (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO recovery_fixture (value) VALUES (?)",
                ("disposable-fixture",),
            )
            connection.commit()
        finally:
            connection.close()

        memory = root / "memory/fixture.json"
        memory.parent.mkdir(parents=True)
        memory.write_text(
            json.dumps(
                {
                    "fixture": True,
                    "purpose": "phase-10-restore-drill",
                    "contains_secrets": False,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        files = []
        for path in sorted(root.rglob("*")):
            if path.is_file():
                files.append(
                    {
                        "path": path.relative_to(root).as_posix(),
                        "size": path.stat().st_size,
                        "sha256": sha256_file(path),
                    }
                )
        manifest = {
            "schema_version": 1,
            "created_at": created_at,
            "database": "data/trading_agent.db",
            "files": files,
        }
        (root / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with tarfile.open(output, "w:gz") as archive:
            for path in sorted(root.iterdir()):
                archive.add(path, arcname=path.name)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--created-at",
        type=parse_created_at,
        default=dt.datetime.now(dt.timezone.utc).isoformat(),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        create_fixture(args.output.expanduser(), args.created_at)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"fixture creation failed: {exc}", file=sys.stderr)
        return 1
    print(args.output.expanduser().resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
