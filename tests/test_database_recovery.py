from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from backend.backup import (
    S3BackupStore,
    S3BackupTarget,
    check_integrity,
    cleanup_wal,
    online_backup,
    restore_from_s3,
    rotate_local_backups,
)
from backend.database_safety import (
    DatabaseCorruptionError,
    DatabaseNotFound,
    EmptyDatabaseError,
    InvalidDatabasePath,
)
from backend.migrations import (
    CURRENT_SCHEMA_VERSION,
    Migration,
    MigrationError,
    apply_migrations,
    get_schema_version,
    upgrade_database,
)
from scripts.deployment_preflight import (
    DeploymentPreflightError,
    preflight,
)


@contextmanager
def open_sqlite(path: Path):
    connection = sqlite3.connect(path)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


class MockS3:
    """Small local S3-compatible client seam for backup tests."""

    def __init__(self) -> None:
        self.versioning: dict[str, str] = {}
        self.objects: dict[tuple[str, str, str], dict] = {}
        self.upload_arguments: list[dict] = []
        self.next_version = 0

    def get_bucket_versioning(self, *, Bucket: str) -> dict:
        status = self.versioning.get(Bucket)
        return {"Status": status} if status else {}

    def put_bucket_versioning(self, *, Bucket: str, VersioningConfiguration: dict):
        self.versioning[Bucket] = VersioningConfiguration["Status"]

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, **kwargs):
        self.next_version += 1
        version = f"version-{self.next_version}"
        self.upload_arguments.append(kwargs)
        self.objects[(Bucket, Key, version)] = {
            "body": Body,
            "metadata": kwargs.get("Metadata", {}),
            "last_modified": datetime.now(timezone.utc),
        }
        return {"VersionId": version}

    def get_object(self, *, Bucket: str, Key: str, VersionId: str | None = None):
        candidates = [
            (version, value)
            for (bucket, key, version), value in self.objects.items()
            if bucket == Bucket and key == Key
        ]
        if VersionId:
            candidates = [
                (version, value)
                for version, value in candidates
                if version == VersionId
            ]
        if not candidates:
            raise KeyError(Key)
        version, value = candidates[-1]
        return {
            "Body": io.BytesIO(value["body"]),
            "Metadata": value["metadata"],
            "VersionId": version,
        }

    def list_objects_v2(self, *, Bucket: str, Prefix: str):
        latest: dict[str, dict] = {}
        for (bucket, key, version), value in self.objects.items():
            if bucket == Bucket and key.startswith(Prefix):
                latest[key] = {
                    "Key": key,
                    "LastModified": value["last_modified"],
                    "VersionId": version,
                }
        return {"Contents": list(latest.values())}

    def delete_object(self, *, Bucket: str, Key: str):
        for object_key in list(self.objects):
            if object_key[0] == Bucket and object_key[1] == Key:
                del self.objects[object_key]


class DatabaseRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.data_dir = self.root / "data"
        self.data_dir.mkdir()
        self.db_path = self.data_dir / "trading_agent.db"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _create_database(self) -> Path:
        upgrade_database(self.db_path)
        with open_sqlite(self.db_path) as connection:
            connection.execute(
                "INSERT INTO watchlist (ticker) VALUES (?)",
                ("TEST",),
            )
        return self.db_path

    def test_upgrade_creates_current_schema_and_is_idempotent(self):
        self.assertEqual(
            upgrade_database(self.db_path),
            CURRENT_SCHEMA_VERSION,
        )
        self.assertEqual(
            get_schema_version(self.db_path),
            CURRENT_SCHEMA_VERSION,
        )
        self.assertEqual(upgrade_database(self.db_path), CURRENT_SCHEMA_VERSION)
        with open_sqlite(self.db_path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertIn("positions", tables)
        self.assertIn("earnings_calendar", tables)

    def test_upgrade_preserves_legacy_rows_and_adds_columns(self):
        with open_sqlite(self.db_path) as connection:
            connection.execute(
                "CREATE TABLE watchlist (ticker TEXT PRIMARY KEY)"
            )
            connection.execute(
                "INSERT INTO watchlist (ticker) VALUES ('LEGACY')"
            )
            connection.commit()

        upgrade_database(self.db_path)
        with open_sqlite(self.db_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT ticker FROM watchlist"
                ).fetchone()[0],
                "LEGACY",
            )
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(paper_trades)"
                )
            }
        self.assertTrue(
            {
                "strategy",
                "confidence",
                "triggered_signals",
                "regime_at_entry",
            }.issubset(columns)
        )

    def test_failed_migration_rolls_back_schema_and_version(self):
        connection = sqlite3.connect(self.db_path)

        def failing_migration(conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE should_rollback (id INTEGER)")
            raise RuntimeError("expected failure")

        with self.assertRaises(MigrationError):
            apply_migrations(
                connection,
                target_version=1,
                migrations=(Migration(1, "failure", failing_migration),),
            )
        self.assertEqual(
            connection.execute("PRAGMA user_version").fetchone()[0],
            0,
        )
        self.assertIsNone(
            connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='table' AND name='should_rollback'
                """
            ).fetchone()
        )
        connection.close()

    def test_empty_and_corrupt_databases_are_rejected(self):
        empty = self.root / "empty.sqlite"
        empty.touch()
        with self.assertRaises(EmptyDatabaseError):
            check_integrity(empty)

        schema_less = self.root / "schema-less.sqlite"
        sqlite3.connect(schema_less).close()
        with self.assertRaises(EmptyDatabaseError):
            check_integrity(schema_less)

        corrupt = self.root / "corrupt.sqlite"
        corrupt.write_bytes(b"not a sqlite database")
        with self.assertRaises(DatabaseCorruptionError):
            check_integrity(corrupt)

        with self.assertRaises(InvalidDatabasePath):
            upgrade_database("relative.sqlite")

    def test_online_backup_restore_and_wal_cleanup(self):
        source = self._create_database()
        with open_sqlite(source) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.executemany(
                "INSERT INTO watchlist (ticker) VALUES (?)",
                [(f"WAL{i}",) for i in range(10)],
            )

        backup = self.root / "backup.sqlite"
        online_backup(source, backup)
        self.assertTrue(check_integrity(backup))
        with open_sqlite(backup) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM watchlist WHERE ticker LIKE 'WAL%'"
                ).fetchone()[0],
                10,
            )
        self.assertFalse(Path(f"{source}-wal").exists())
        self.assertFalse(Path(f"{source}-shm").exists())

        restored = self.root / "restored.sqlite"
        restore_path = online_backup(backup, restored, cleanup_source_wal=False)
        self.assertEqual(restore_path, restored)
        self.assertTrue(check_integrity(restored))
        cleanup_wal(restored)

    def test_s3_mock_requires_versioning_and_encryption_and_restores_version(self):
        source = self._create_database()
        mock_s3 = MockS3()
        target = S3BackupTarget(
            bucket="backups",
            prefix="sqlite",
            require_versioning=True,
            auto_enable_versioning=True,
        )
        store = S3BackupStore(target, client=mock_s3)

        uploaded = store.upload_file(source, key="sqlite/current.sqlite")
        self.assertEqual(uploaded["version_id"], "version-1")
        self.assertEqual(
            mock_s3.upload_arguments[0]["ServerSideEncryption"],
            "AES256",
        )
        self.assertEqual(mock_s3.versioning["backups"], "Enabled")

        restored = self.root / "s3-restored.sqlite"
        restore_from_s3(
            store,
            uploaded["key"],
            restored,
            version_id=uploaded["version_id"],
        )
        self.assertTrue(check_integrity(restored))
        with open_sqlite(restored) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT ticker FROM watchlist WHERE ticker='TEST'"
                ).fetchone()[0],
                "TEST",
            )

    def test_local_rotation_keeps_newest_artifacts(self):
        backup_dir = self.root / "backups"
        backup_dir.mkdir()
        paths = []
        for index in range(3):
            path = backup_dir / f"backup-{index}.sqlite"
            path.write_bytes(str(index).encode())
            paths.append(path)
        removed = rotate_local_backups(backup_dir, keep=1)
        self.assertEqual(len(removed), 2)
        self.assertEqual(len(list(backup_dir.glob("*.sqlite"))), 1)

    def test_deployment_preflight_requires_valid_data_and_backup_target(self):
        database = self._create_database()
        report = preflight(
            data_dir=self.data_dir,
            db_path=database,
            runtime_env={
                "TRADINGAGENTS_BACKUP_S3_BUCKET": "backups",
                "TRADINGAGENTS_BACKUP_S3_ENDPOINT_URL": "http://127.0.0.1:9000",
            },
        )
        self.assertEqual(report["status"], "ok")

        with self.assertRaises(DatabaseNotFound):
            preflight(
                data_dir=self.root / "outside",
                db_path=database,
                runtime_env={
                    "TRADINGAGENTS_BACKUP_S3_BUCKET": "backups",
                },
            )
        with self.assertRaises(DeploymentPreflightError):
            preflight(
                data_dir=self.data_dir,
                db_path=database,
                runtime_env={},
            )
        with self.assertRaises(DeploymentPreflightError):
            preflight(
                data_dir=self.data_dir,
                db_path=database,
                runtime_env={
                    "TRADINGAGENTS_BACKUP_S3_BUCKET": "backups",
                    "TRADINGAGENTS_BACKUP_S3_REQUIRE_VERSIONING": "false",
                },
            )


if __name__ == "__main__":
    unittest.main()
