"""Versioned, transactional SQLite schema migrations.

Every schema change belongs in this module.  The migration runner deliberately
uses individual ``Connection.execute`` calls inside one explicit transaction;
``executescript`` is not used because it can commit around DDL and leave a
partially upgraded database after a failure.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from backend.database_safety import (
    DatabaseCorruptionError,
    DatabaseNotFound,
    EmptyDatabaseError,
    InvalidDatabasePath,
    reject_unexpected_empty_database,
    remove_sqlite_sidecars,
    validate_data_path,
)


class MigrationError(RuntimeError):
    """Raised when a database cannot be upgraded atomically."""


MigrationFunction = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class Migration:
    """One forward-only schema migration."""

    version: int
    name: str
    apply: MigrationFunction


def _execute_statements(
    connection: sqlite3.Connection,
    statements: Iterable[str],
) -> None:
    for statement in statements:
        connection.execute(statement)


def _table_columns(
    connection: sqlite3.Connection,
    table_name: str,
) -> set[str]:
    return {
        row[1]
        for row in connection.execute(
            f"PRAGMA table_info({table_name})"
        ).fetchall()
    }


def _add_column_if_missing(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    declaration: str,
) -> None:
    if column_name not in _table_columns(connection, table_name):
        connection.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {declaration}"
        )


def _migration_1_core(connection: sqlite3.Connection) -> None:
    _execute_statements(
        connection,
        (
            """
            CREATE TABLE IF NOT EXISTS watchlist (
                ticker TEXT PRIMARY KEY,
                exchange TEXT DEFAULT 'NSE',
                name TEXT,
                added_at TEXT DEFAULT (datetime('now'))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS analysis_history (
                task_id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                signal TEXT,
                market_report TEXT,
                sentiment_report TEXT,
                news_report TEXT,
                fundamentals_report TEXT,
                investment_plan TEXT,
                trader_investment_plan TEXT,
                final_trade_decision TEXT,
                bull_history TEXT,
                bear_history TEXT,
                risk_aggressive_history TEXT,
                risk_conservative_history TEXT,
                risk_neutral_history TEXT,
                stats TEXT,
                duration_seconds REAL,
                entry_price REAL,
                exit_price REAL,
                pnl_amount REAL,
                pnl_pct REAL,
                pnl_status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now'))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS backtest_runs (
                backtest_id TEXT PRIMARY KEY,
                ticker TEXT NOT NULL,
                initial_capital REAL DEFAULT 100000,
                position_size_pct REAL DEFAULT 10,
                enable_learning BOOLEAN DEFAULT 0,
                total_trades INTEGER DEFAULT 0,
                winning_trades INTEGER DEFAULT 0,
                losing_trades INTEGER DEFAULT 0,
                total_return_pct REAL DEFAULT 0,
                max_drawdown_pct REAL DEFAULT 0,
                final_portfolio_value REAL,
                status TEXT DEFAULT 'running',
                created_at TEXT DEFAULT (datetime('now'))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS backtest_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                backtest_id TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                signal TEXT,
                entry_price REAL,
                exit_price REAL,
                pnl_amount REAL,
                pnl_pct REAL,
                cumulative_pnl REAL,
                portfolio_value REAL,
                duration_seconds REAL,
                FOREIGN KEY (backtest_id) REFERENCES backtest_runs(backtest_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """,
        ),
    )


def _migration_2_paper_trading(connection: sqlite3.Connection) -> None:
    _execute_statements(
        connection,
        (
            """
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                source TEXT,
                direction TEXT,
                signal TEXT,
                score REAL,
                success_probability INTEGER,
                entry_price REAL NOT NULL,
                entry_date TEXT DEFAULT (date('now')),
                entry_datetime TEXT DEFAULT (datetime('now')),
                price_1d REAL,
                price_3d REAL,
                price_5d REAL,
                price_10d REAL,
                pnl_1d_pct REAL,
                pnl_3d_pct REAL,
                pnl_5d_pct REAL,
                pnl_10d_pct REAL,
                status TEXT DEFAULT 'active',
                notes TEXT,
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS recommender_backtests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                signal TEXT,
                score REAL,
                confidence TEXT,
                success_probability INTEGER,
                entry_price REAL,
                return_1d REAL,
                return_3d REAL,
                return_5d REAL,
                return_10d REAL,
                outcome_1d TEXT,
                outcome_5d TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
            """,
        ),
    )


def _migration_3_paper_trade_metadata(connection: sqlite3.Connection) -> None:
    for column_name, declaration in (
        ("strategy", "TEXT"),
        ("confidence", "TEXT"),
        ("triggered_signals", "TEXT"),
        ("regime_at_entry", "TEXT"),
    ):
        _add_column_if_missing(
            connection,
            "paper_trades",
            column_name,
            declaration,
        )


def _migration_4_verdict_history(connection: sqlite3.Connection) -> None:
    _execute_statements(
        connection,
        (
            """
            CREATE TABLE IF NOT EXISTS verdict_history (
                snapshot_date TEXT PRIMARY KEY,
                verdict TEXT NOT NULL,
                label TEXT,
                action TEXT,
                caution_count INTEGER,
                favorable_count INTEGER,
                caution_flags TEXT,
                favorable_flags TEXT,
                position_size_pct REAL,
                max_trades_today INTEGER,
                min_conviction TEXT,
                nifty_close REAL,
                nifty_close_1d REAL,
                nifty_close_3d REAL,
                nifty_close_5d REAL,
                nifty_return_1d_pct REAL,
                nifty_return_3d_pct REAL,
                nifty_return_5d_pct REAL,
                outcome_1d TEXT,
                outcome_3d TEXT,
                outcome_5d TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """,
        ),
    )


def _migration_5_shadow_trades(connection: sqlite3.Connection) -> None:
    _execute_statements(
        connection,
        (
            """
            CREATE TABLE IF NOT EXISTS shadow_trades (
                ticker TEXT NOT NULL,
                signal_date TEXT NOT NULL,
                signal TEXT,
                score REAL,
                confidence TEXT,
                success_probability INTEGER,
                triggered_signals TEXT,
                regime_at_entry TEXT,
                entry_price REAL NOT NULL,
                price_1d REAL,
                price_3d REAL,
                price_5d REAL,
                price_10d REAL,
                pnl_1d_pct REAL,
                pnl_3d_pct REAL,
                pnl_5d_pct REAL,
                pnl_10d_pct REAL,
                user_tracked INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (ticker, signal_date)
            )
            """,
        ),
    )


def _migration_6_external_calendars(connection: sqlite3.Connection) -> None:
    _execute_statements(
        connection,
        (
            """
            CREATE TABLE IF NOT EXISTS fii_dii_history (
                date TEXT PRIMARY KEY,
                fii_buy REAL,
                fii_sell REAL,
                fii_net REAL,
                dii_buy REAL,
                dii_sell REAL,
                dii_net REAL,
                source TEXT,
                fetched_at TEXT DEFAULT (datetime('now'))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS earnings_calendar (
                ticker TEXT NOT NULL,
                event_date TEXT NOT NULL,
                event_type TEXT DEFAULT 'earnings',
                description TEXT,
                fetched_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (ticker, event_date, event_type)
            )
            """,
        ),
    )


def _migration_7_equity_reviews(connection: sqlite3.Connection) -> None:
    _execute_statements(
        connection,
        (
            """
            CREATE TABLE IF NOT EXISTS equity_portfolio_reviews (
                review_id TEXT PRIMARY KEY,
                review_date TEXT NOT NULL,
                holdings_json TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                insights_json TEXT NOT NULL,
                model_metadata_json TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
            """,
        ),
    )


def _migration_8_positions(connection: sqlite3.Connection) -> None:
    _execute_statements(
        connection,
        (
            """
            CREATE TABLE IF NOT EXISTS positions (
                tradingsymbol TEXT NOT NULL,
                exchange TEXT NOT NULL DEFAULT 'NSE',
                isin TEXT,
                product TEXT,
                quantity REAL NOT NULL DEFAULT 0,
                t1_quantity REAL DEFAULT 0,
                average_price REAL NOT NULL DEFAULT 0,
                last_price REAL,
                close_price REAL,
                invested_value REAL,
                current_value REAL,
                pnl REAL,
                pnl_pct REAL,
                day_change REAL,
                day_change_pct REAL,
                source TEXT NOT NULL DEFAULT 'manual',
                notes TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (tradingsymbol, exchange)
            )
            """,
        ),
    )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "core tables", _migration_1_core),
    Migration(2, "paper trading and recommender backtests", _migration_2_paper_trading),
    Migration(3, "paper trade metadata", _migration_3_paper_trade_metadata),
    Migration(4, "verdict history", _migration_4_verdict_history),
    Migration(5, "shadow trades", _migration_5_shadow_trades),
    Migration(6, "external market calendars", _migration_6_external_calendars),
    Migration(7, "equity portfolio reviews", _migration_7_equity_reviews),
    Migration(8, "positions", _migration_8_positions),
)

CURRENT_SCHEMA_VERSION = MIGRATIONS[-1].version

REQUIRED_TABLES: tuple[str, ...] = (
    "watchlist",
    "analysis_history",
    "backtest_runs",
    "backtest_trades",
    "settings",
    "paper_trades",
    "recommender_backtests",
    "verdict_history",
    "shadow_trades",
    "fii_dii_history",
    "earnings_calendar",
    "equity_portfolio_reviews",
    "positions",
)

REQUIRED_COLUMNS: dict[str, tuple[str, ...]] = {
    "paper_trades": (
        "strategy",
        "confidence",
        "triggered_signals",
        "regime_at_entry",
    ),
    "positions": ("tradingsymbol", "exchange", "source"),
}


def _read_user_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def _set_user_version(connection: sqlite3.Connection, version: int) -> None:
    if version < 0:
        raise MigrationError(f"invalid schema version: {version}")
    connection.execute(f"PRAGMA user_version = {version}")


def _validate_schema_connection(connection: sqlite3.Connection) -> None:
    existing_tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    missing_tables = [
        table for table in REQUIRED_TABLES if table not in existing_tables
    ]
    if missing_tables:
        raise MigrationError(
            f"schema is missing required tables: {', '.join(missing_tables)}"
        )

    for table_name, columns in REQUIRED_COLUMNS.items():
        missing_columns = set(columns) - _table_columns(connection, table_name)
        if missing_columns:
            raise MigrationError(
                f"table {table_name} is missing required columns: "
                f"{', '.join(sorted(missing_columns))}"
            )


def apply_migrations(
    connection: sqlite3.Connection,
    *,
    target_version: int = CURRENT_SCHEMA_VERSION,
    migrations: Iterable[Migration] = MIGRATIONS,
) -> int:
    """Apply migrations to an open connection in one transaction.

    This lower-level entry point is intentionally public so migration
    transaction behavior can be tested without touching a configured data
    path.  The connection must not already have an open transaction.
    """

    if target_version < 0 or target_version > CURRENT_SCHEMA_VERSION:
        raise MigrationError(
            f"target schema version must be between 0 and "
            f"{CURRENT_SCHEMA_VERSION}, got {target_version}"
        )
    if connection.in_transaction:
        raise MigrationError(
            "migration connection already has an open transaction"
        )

    migration_list = tuple(migrations)
    versions = [migration.version for migration in migration_list]
    if versions != sorted(set(versions)):
        raise MigrationError("migration versions must be unique and ascending")

    current_version = _read_user_version(connection)
    if current_version > target_version:
        raise MigrationError(
            f"database schema {current_version} is newer than target "
            f"{target_version}"
        )

    by_version = {migration.version: migration for migration in migration_list}
    missing_versions = [
        version
        for version in range(current_version + 1, target_version + 1)
        if version not in by_version
    ]
    if missing_versions:
        raise MigrationError(
            f"missing migrations for versions: {', '.join(map(str, missing_versions))}"
        )

    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        for version in range(current_version + 1, target_version + 1):
            migration = by_version[version]
            try:
                migration.apply(connection)
                _set_user_version(connection, version)
            except Exception as exc:
                raise MigrationError(
                    f"migration {version} ({migration.name}) failed"
                ) from exc

        if target_version == CURRENT_SCHEMA_VERSION:
            _validate_schema_connection(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return target_version


def upgrade_database(
    db_path: str | os.PathLike[str] | None = None,
    *,
    target_version: int = CURRENT_SCHEMA_VERSION,
) -> int:
    """Upgrade a configured or explicit database path and return its version."""

    if db_path is None:
        # Import lazily to preserve the existing DB_PATH override seam used by
        # tests and by the local CLI.
        from backend.db import DB_PATH

        db_path = DB_PATH

    path = validate_data_path(db_path, allow_missing=True, create_parent=True)
    existed = path.exists()
    if existed:
        reject_unexpected_empty_database(path)

    connection: sqlite3.Connection | None = None
    completed = False
    try:
        connection = sqlite3.connect(str(path), timeout=30)
        connection.row_factory = sqlite3.Row
        apply_migrations(connection, target_version=target_version)
        completed = True
        return target_version
    except (EmptyDatabaseError, DatabaseNotFound, InvalidDatabasePath):
        raise
    except sqlite3.DatabaseError as exc:
        raise DatabaseCorruptionError(
            f"database migration could not open {path!s}"
        ) from exc
    except MigrationError:
        raise
    except Exception as exc:
        raise MigrationError(f"database migration failed for {path!s}") from exc
    finally:
        if connection is not None:
            connection.close()
        if not existed and not completed:
            # A failed bootstrap must not leave a schema-less file that a
            # later process could mistake for a valid database.
            try:
                for suffix in ("-wal", "-shm"):
                    Path(f"{path}{suffix}").unlink(missing_ok=True)
                path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass


def get_schema_version(
    db_path: str | os.PathLike[str] | None = None,
) -> int:
    """Read ``PRAGMA user_version`` after applying path safety checks."""

    if db_path is None:
        from backend.db import DB_PATH

        db_path = DB_PATH
    path = reject_unexpected_empty_database(db_path)
    try:
        with closing(sqlite3.connect(str(path), timeout=5)) as connection:
            return _read_user_version(connection)
    except sqlite3.DatabaseError as exc:
        raise DatabaseCorruptionError(
            f"could not read schema version from {path!s}"
        ) from exc


def validate_schema(
    db_path: str | os.PathLike[str] | None = None,
) -> bool:
    """Validate that a database has the complete current application schema."""

    if db_path is None:
        from backend.db import DB_PATH

        db_path = DB_PATH
    path = reject_unexpected_empty_database(db_path)
    try:
        with closing(sqlite3.connect(str(path), timeout=5)) as connection:
            _validate_schema_connection(connection)
    except MigrationError:
        raise
    except sqlite3.DatabaseError as exc:
        raise DatabaseCorruptionError(
            f"could not validate schema for {path!s}"
        ) from exc
    return True


# Compatibility aliases for callers and operational scripts.
migrate = upgrade_database
run_migrations = upgrade_database
upgrade = upgrade_database
