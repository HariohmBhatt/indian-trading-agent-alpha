"""Shared safety checks for SQLite data paths and database files.

The application historically let SQLite create whatever path it was given.
That is dangerous during deployment: a relative path or a newly-created empty
file can look like a successful database open while silently hiding the real
data directory.  This module keeps those checks small and dependency-free so
the migration, backup, and deployment preflight tools use the same rules.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path


class DatabaseSafetyError(RuntimeError):
    """Base class for database safety failures."""


class InvalidDatabasePath(DatabaseSafetyError):
    """Raised when a configured SQLite path is unsafe or unusable."""


class DatabaseNotFound(DatabaseSafetyError):
    """Raised when an operation requires a database that is not present."""


class EmptyDatabaseError(DatabaseSafetyError):
    """Raised for an unexpected empty SQLite file or schema."""


class DatabaseCorruptionError(DatabaseSafetyError):
    """Raised when SQLite cannot read or validate a database."""


def validate_data_path(
    path: str | os.PathLike[str],
    *,
    allow_missing: bool = True,
    create_parent: bool = False,
) -> Path:
    """Validate and normalize a database path.

    Database paths must be absolute and point to a regular file (or a
    not-yet-created file).  The parent may be created explicitly by callers
    that are bootstrapping a new database.  Existing files are never replaced
    by this function.
    """

    if path is None:
        raise InvalidDatabasePath("database path is required")

    raw_path = os.fspath(path)
    if not raw_path or not raw_path.strip():
        raise InvalidDatabasePath("database path must not be empty")

    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        raise InvalidDatabasePath(
            f"database path must be absolute, got {candidate!s}"
        )
    if candidate == candidate.root or not candidate.name:
        raise InvalidDatabasePath(
            f"database path must name a file, got {candidate!s}"
        )

    if candidate.exists():
        if candidate.is_dir():
            raise InvalidDatabasePath(
                f"database path points to a directory: {candidate!s}"
            )
        if not candidate.is_file():
            raise InvalidDatabasePath(
                f"database path is not a regular file: {candidate!s}"
            )
    elif not allow_missing:
        raise DatabaseNotFound(f"database file does not exist: {candidate!s}")

    parent = candidate.parent
    if parent.exists() and not parent.is_dir():
        raise InvalidDatabasePath(
            f"database parent is not a directory: {parent!s}"
        )
    if not parent.exists():
        if not allow_missing:
            raise DatabaseNotFound(
                f"database parent directory does not exist: {parent!s}"
            )
        if create_parent:
            parent.mkdir(parents=True, exist_ok=True)

    # A database directly under / is almost always a deployment typo.  Keep
    # /tmp and mounted data directories valid while rejecting that foot-gun.
    if parent == Path("/"):
        raise InvalidDatabasePath(
            f"database path must be under a data directory: {candidate!s}"
        )

    return candidate


def reject_unexpected_empty_database(
    path: str | os.PathLike[str],
    *,
    require_exists: bool = True,
) -> Path:
    """Reject empty, schema-less, or missing database files.

    A missing file is allowed only when ``require_exists`` is false.  A file
    created by ``sqlite3.connect`` without any tables is also rejected; it is
    indistinguishable from a lost volume during deployment.
    """

    candidate = validate_data_path(path, allow_missing=not require_exists)
    if not candidate.exists():
        if require_exists:
            raise DatabaseNotFound(f"database file does not exist: {candidate!s}")
        return candidate

    if candidate.stat().st_size == 0:
        raise EmptyDatabaseError(f"database file is empty: {candidate!s}")

    try:
        with closing(sqlite3.connect(str(candidate), timeout=5)) as connection:
            table_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            ).fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise DatabaseCorruptionError(
            f"database cannot be opened: {candidate!s}"
        ) from exc

    if table_count == 0:
        raise EmptyDatabaseError(
            f"database has no application tables: {candidate!s}"
        )
    return candidate


def remove_sqlite_sidecars(
    path: str | os.PathLike[str],
    *,
    missing_ok: bool = True,
) -> list[Path]:
    """Remove SQLite WAL and shared-memory sidecars after a safe checkpoint."""

    candidate = validate_data_path(path, allow_missing=False)
    removed: list[Path] = []
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{candidate}{suffix}")
        try:
            sidecar.unlink()
        except FileNotFoundError:
            if not missing_ok:
                raise
        else:
            removed.append(sidecar)
    return removed
