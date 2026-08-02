"""Online SQLite backup, integrity, restore, and S3-compatible storage.

Backups use SQLite's online backup API rather than copying the live database
file.  The resulting artifact is checked before it is published.  S3 uploads
require bucket versioning and request server-side encryption, which keeps
restore points available without placing credentials or plaintext backup
management in the application request path.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
from typing import Any

from backend.database_safety import (
    DatabaseCorruptionError,
    DatabaseSafetyError,
    EmptyDatabaseError,
    InvalidDatabasePath,
    reject_unexpected_empty_database,
    remove_sqlite_sidecars,
    validate_data_path,
)


class BackupError(DatabaseSafetyError):
    """Raised when a backup, restore, or remote publication fails."""


class WALCleanupError(BackupError):
    """Raised when SQLite cannot checkpoint and remove WAL state safely."""


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_integrity(
    db_path: str | os.PathLike[str],
    *,
    quick: bool = False,
) -> bool:
    """Run SQLite's integrity check and raise on corruption."""

    path = reject_unexpected_empty_database(db_path)
    pragma = "quick_check" if quick else "integrity_check"
    try:
        with closing(sqlite3.connect(str(path), timeout=30)) as connection:
            results = connection.execute(f"PRAGMA {pragma}").fetchall()
    except sqlite3.DatabaseError as exc:
        raise DatabaseCorruptionError(
            f"SQLite {pragma} could not read {path!s}"
        ) from exc

    messages = [str(row[0]) for row in results]
    if messages != ["ok"]:
        detail = "; ".join(messages) or "no integrity result"
        raise DatabaseCorruptionError(
            f"SQLite {pragma} failed for {path!s}: {detail}"
        )
    return True


def validate_database(
    db_path: str | os.PathLike[str],
    *,
    require_current_schema: bool = False,
) -> bool:
    """Validate a database file before backup or restore use."""

    path = reject_unexpected_empty_database(db_path)
    check_integrity(path)
    if require_current_schema:
        from backend.migrations import validate_schema

        validate_schema(path)
    return True


def cleanup_wal(db_path: str | os.PathLike[str]) -> list[Path]:
    """Checkpoint a SQLite WAL and remove its sidecars.

    Cleanup is intentionally explicit and fails if another process is holding
    the WAL busy.  Silently deleting a live WAL could lose committed writes.
    """

    path = reject_unexpected_empty_database(db_path)
    try:
        with closing(sqlite3.connect(str(path), timeout=30)) as connection:
            result = connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise WALCleanupError(
            f"SQLite WAL checkpoint failed for {path!s}"
        ) from exc

    if result and int(result[0]) != 0:
        raise WALCleanupError(
            f"SQLite WAL is busy for {path!s}; refusing to remove sidecars"
        )
    return remove_sqlite_sidecars(path)


def _temporary_destination(destination: Path) -> Path:
    return destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )


def online_backup(
    source_path: str | os.PathLike[str],
    destination_path: str | os.PathLike[str],
    *,
    pages: int = 256,
    sleep: float = 0.05,
    cleanup_source_wal: bool = True,
) -> Path:
    """Create an atomic online SQLite backup while the source is live."""

    source = reject_unexpected_empty_database(source_path)
    destination = validate_data_path(
        destination_path,
        allow_missing=True,
        create_parent=True,
    )
    if source.resolve() == destination.resolve():
        raise BackupError("source and destination database paths must differ")
    if pages <= 0:
        raise ValueError("pages must be greater than zero")
    if sleep < 0:
        raise ValueError("sleep must not be negative")

    validate_database(source)
    temporary = _temporary_destination(destination)
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(str(source), timeout=30)
        source_connection.execute("PRAGMA busy_timeout = 30000")
        destination_connection = sqlite3.connect(str(temporary), timeout=30)
        source_connection.backup(
            destination_connection,
            pages=pages,
            sleep=sleep,
        )
        destination_connection.commit()
        destination_connection.close()
        destination_connection = None
        check_integrity(temporary)
        os.replace(temporary, destination)
        # Replacing the main file does not replace stale sidecars left by a
        # previous destination.  The destination is an offline artifact at
        # this point, so remove those only after the new file is in place.
        remove_sqlite_sidecars(destination, missing_ok=True)
    except sqlite3.DatabaseError as exc:
        raise BackupError(
            f"online SQLite backup failed for {source!s}"
        ) from exc
    finally:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            source_connection.close()
        try:
            if temporary.exists():
                remove_sqlite_sidecars(temporary, missing_ok=True)
            temporary.unlink()
        except FileNotFoundError:
            pass

    if cleanup_source_wal:
        cleanup_wal(source)
    return destination


def restore_database(
    backup_path: str | os.PathLike[str],
    destination_path: str | os.PathLike[str],
    *,
    cleanup_source_wal: bool = False,
) -> Path:
    """Restore a validated SQLite backup atomically to a destination path."""

    return online_backup(
        backup_path,
        destination_path,
        cleanup_source_wal=cleanup_source_wal,
    )


def rotate_local_backups(
    directory: str | os.PathLike[str],
    *,
    pattern: str = "*.sqlite",
    keep: int = 7,
) -> list[Path]:
    """Remove older local backup artifacts, keeping the newest ``keep`` files."""

    if keep < 1:
        raise ValueError("keep must be at least one")
    backup_directory = Path(directory).expanduser()
    if not backup_directory.is_absolute():
        raise InvalidDatabasePath(
            f"backup directory must be absolute: {backup_directory!s}"
        )
    if not backup_directory.exists():
        return []
    if not backup_directory.is_dir():
        raise InvalidDatabasePath(
            f"backup path is not a directory: {backup_directory!s}"
        )

    backups = sorted(
        (
            path
            for path in backup_directory.glob(pattern)
            if path.is_file()
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    removed: list[Path] = []
    for path in backups[keep:]:
        path.unlink()
        removed.append(path)
    return removed


@dataclass(frozen=True)
class S3BackupTarget:
    """Configuration for an S3 or S3-compatible backup target."""

    bucket: str
    prefix: str = "trading-agent/database"
    endpoint_url: str | None = None
    region_name: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    session_token: str | None = None
    sse_algorithm: str = "AES256"
    sse_kms_key_id: str | None = None
    require_versioning: bool = True
    auto_enable_versioning: bool = True

    @classmethod
    def from_env(cls) -> "S3BackupTarget":
        bucket = os.getenv("TRADINGAGENTS_BACKUP_S3_BUCKET", "").strip()
        if not bucket:
            raise BackupError(
                "TRADINGAGENTS_BACKUP_S3_BUCKET is required for remote backups"
            )
        sse_algorithm = os.getenv(
            "TRADINGAGENTS_BACKUP_S3_SSE",
            "AES256",
        ).strip()
        if sse_algorithm not in {"AES256", "aws:kms"}:
            raise BackupError(
                "TRADINGAGENTS_BACKUP_S3_SSE must be AES256 or aws:kms"
            )
        kms_key_id = os.getenv(
            "TRADINGAGENTS_BACKUP_S3_SSE_KMS_KEY_ID"
        ) or None
        if sse_algorithm == "aws:kms" and not kms_key_id:
            raise BackupError(
                "TRADINGAGENTS_BACKUP_S3_SSE_KMS_KEY_ID is required for aws:kms"
            )
        return cls(
            bucket=bucket,
            prefix=os.getenv(
                "TRADINGAGENTS_BACKUP_S3_PREFIX",
                "trading-agent/database",
            ).strip("/"),
            endpoint_url=os.getenv(
                "TRADINGAGENTS_BACKUP_S3_ENDPOINT_URL"
            ) or None,
            region_name=os.getenv(
                "TRADINGAGENTS_BACKUP_S3_REGION"
            )
            or os.getenv("AWS_REGION")
            or os.getenv("AWS_DEFAULT_REGION")
            or None,
            access_key_id=os.getenv("AWS_ACCESS_KEY_ID") or None,
            secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY") or None,
            session_token=os.getenv("AWS_SESSION_TOKEN") or None,
            sse_algorithm=sse_algorithm,
            sse_kms_key_id=kms_key_id,
            require_versioning=_env_bool(
                "TRADINGAGENTS_BACKUP_S3_REQUIRE_VERSIONING",
                True,
            ),
            auto_enable_versioning=_env_bool(
                "TRADINGAGENTS_BACKUP_S3_AUTO_ENABLE_VERSIONING",
                True,
            ),
        )

    def create_client(self) -> Any:
        """Create a boto3 client lazily so local SQLite tools need no import."""

        try:
            import boto3
        except ImportError as exc:
            raise BackupError(
                "boto3 is required for S3-compatible database backups"
            ) from exc

        kwargs: dict[str, Any] = {}
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url
        if self.region_name:
            kwargs["region_name"] = self.region_name
        if self.access_key_id:
            kwargs["aws_access_key_id"] = self.access_key_id
        if self.secret_access_key:
            kwargs["aws_secret_access_key"] = self.secret_access_key
        if self.session_token:
            kwargs["aws_session_token"] = self.session_token
        return boto3.client("s3", **kwargs)


class S3BackupStore:
    """Publish and retrieve database artifacts from a versioned S3 target."""

    def __init__(
        self,
        target: S3BackupTarget,
        *,
        client: Any | None = None,
    ) -> None:
        if not target.bucket.strip():
            raise BackupError("S3 backup bucket must not be empty")
        if target.sse_algorithm not in {"AES256", "aws:kms"}:
            raise BackupError(
                "S3 server-side encryption must be AES256 or aws:kms"
            )
        if target.sse_algorithm == "aws:kms" and not target.sse_kms_key_id:
            raise BackupError(
                "S3 KMS encryption requires sse_kms_key_id"
            )
        self.target = target
        self.client = client

    def _client(self) -> Any:
        if self.client is None:
            self.client = self.target.create_client()
        return self.client

    def ensure_versioning(self) -> bool:
        """Require enabled versioning, enabling it only when configured."""

        client = self._client()
        try:
            response = client.get_bucket_versioning(
                Bucket=self.target.bucket
            )
            status = response.get("Status")
            if status != "Enabled" and self.target.auto_enable_versioning:
                client.put_bucket_versioning(
                    Bucket=self.target.bucket,
                    VersioningConfiguration={"Status": "Enabled"},
                )
                response = client.get_bucket_versioning(
                    Bucket=self.target.bucket
                )
                status = response.get("Status")
        except Exception as exc:
            raise BackupError(
                f"could not verify versioning for S3 bucket "
                f"{self.target.bucket!r}"
            ) from exc
        if self.target.require_versioning and status != "Enabled":
            raise BackupError(
                f"S3 bucket {self.target.bucket!r} does not have versioning enabled"
            )
        return status == "Enabled"

    def _key(self, key: str | None = None) -> str:
        if key:
            return key.lstrip("/")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = uuid.uuid4().hex
        prefix = self.target.prefix.strip("/")
        filename = f"trading-agent-{timestamp}-{suffix}.sqlite"
        return f"{prefix}/{filename}" if prefix else filename

    def upload_file(
        self,
        path: str | os.PathLike[str],
        *,
        key: str | None = None,
    ) -> dict[str, Any]:
        """Upload an integrity-checked SQLite artifact with SSE metadata."""

        artifact = reject_unexpected_empty_database(path)
        validate_database(artifact)
        self.ensure_versioning()
        object_key = self._key(key)
        digest = _sha256(artifact)
        extra: dict[str, Any] = {
            "ServerSideEncryption": self.target.sse_algorithm,
            "ContentType": "application/x-sqlite3",
            "Metadata": {
                "sha256": digest,
                "integrity": "sqlite-integrity-check-ok",
            },
        }
        if self.target.sse_algorithm == "aws:kms":
            extra["SSEKMSKeyId"] = self.target.sse_kms_key_id

        try:
            with Path(artifact).open("rb") as stream:
                response = self._client().put_object(
                    Bucket=self.target.bucket,
                    Key=object_key,
                    Body=stream.read(),
                    **extra,
                )
        except Exception as exc:
            raise BackupError(
                f"could not upload SQLite backup to S3 key {object_key!r}"
            ) from exc
        version_id = response.get("VersionId")
        if self.target.require_versioning and not version_id:
            raise BackupError(
                "S3 upload did not return a VersionId; refusing an unversioned backup"
            )
        return {
            "bucket": self.target.bucket,
            "key": object_key,
            "version_id": version_id,
            "sha256": digest,
            "server_side_encryption": self.target.sse_algorithm,
        }

    def download_file(
        self,
        key: str,
        destination_path: str | os.PathLike[str],
        *,
        version_id: str | None = None,
    ) -> Path:
        """Download one object version to an atomic local artifact."""

        destination = validate_data_path(
            destination_path,
            allow_missing=True,
            create_parent=True,
        )
        temporary = _temporary_destination(destination)
        kwargs: dict[str, Any] = {
            "Bucket": self.target.bucket,
            "Key": key.lstrip("/"),
        }
        if version_id:
            kwargs["VersionId"] = version_id
        try:
            response = self._client().get_object(**kwargs)
            body = response["Body"]
            data = body.read() if hasattr(body, "read") else body
            if hasattr(body, "close"):
                body.close()
            if not data:
                raise EmptyDatabaseError(
                    f"S3 object {key!r} is empty"
                )
            with temporary.open("wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            expected_sha = (response.get("Metadata") or {}).get("sha256")
            if expected_sha and _sha256(temporary) != expected_sha:
                raise BackupError(
                    f"S3 object {key!r} failed its SHA-256 verification"
                )
            validate_database(temporary)
            os.replace(temporary, destination)
            return destination
        except (DatabaseSafetyError, BackupError):
            raise
        except Exception as exc:
            raise BackupError(
                f"could not download S3 backup {key!r}"
            ) from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def list_keys(self) -> list[dict[str, Any]]:
        """List backup objects under the configured prefix."""

        client = self._client()
        prefix = self.target.prefix.strip("/")
        prefix = f"{prefix}/" if prefix else ""
        objects: list[dict[str, Any]] = []
        continuation_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "Bucket": self.target.bucket,
                "Prefix": prefix,
            }
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token
            response = client.list_objects_v2(**kwargs)
            objects.extend(response.get("Contents") or [])
            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")
            if not continuation_token:
                raise BackupError(
                    "S3 list response was truncated without a continuation token"
                )
        return objects

    def rotate(self, *, keep: int = 7) -> list[str]:
        """Delete older backup keys while preserving S3 object version history."""

        if keep < 1:
            raise ValueError("keep must be at least one")
        objects = sorted(
            self.list_keys(),
            key=lambda item: (
                item.get("LastModified", ""),
                item.get("Key", ""),
            ),
            reverse=True,
        )
        removed: list[str] = []
        for item in objects[keep:]:
            key = item.get("Key")
            if not key:
                continue
            try:
                self._client().delete_object(
                    Bucket=self.target.bucket,
                    Key=key,
                )
            except Exception as exc:
                raise BackupError(
                    f"could not rotate S3 backup key {key!r}"
                ) from exc
            removed.append(key)
        return removed


def backup_to_s3(
    source_path: str | os.PathLike[str],
    target: S3BackupTarget | S3BackupStore,
    *,
    key: str | None = None,
    cleanup_source_wal: bool = True,
) -> dict[str, Any]:
    """Create an online backup and publish it to the configured S3 target."""

    source = reject_unexpected_empty_database(source_path)
    store = target if isinstance(target, S3BackupStore) else S3BackupStore(target)
    with tempfile.TemporaryDirectory(
        prefix=".trading-agent-db-backup-",
        dir=str(source.parent),
    ) as temporary_directory:
        artifact = Path(temporary_directory) / "database.sqlite"
        online_backup(
            source,
            artifact,
            cleanup_source_wal=cleanup_source_wal,
        )
        result = store.upload_file(artifact, key=key)
    result["source"] = str(source)
    return result


def restore_from_s3(
    target: S3BackupTarget | S3BackupStore,
    key: str,
    destination_path: str | os.PathLike[str],
    *,
    version_id: str | None = None,
) -> Path:
    """Fetch and atomically restore one S3 object version."""

    store = target if isinstance(target, S3BackupStore) else S3BackupStore(target)
    store.ensure_versioning()
    destination = validate_data_path(
        destination_path,
        allow_missing=True,
        create_parent=True,
    )
    with tempfile.TemporaryDirectory(
        prefix=".trading-agent-db-restore-",
        dir=str(destination.parent),
    ) as temporary_directory:
        artifact = Path(temporary_directory) / "database.sqlite"
        store.download_file(key, artifact, version_id=version_id)
        restored = restore_database(
            artifact,
            destination,
            cleanup_source_wal=False,
        )
    cleanup_wal(restored)
    return restored


# Compatibility aliases for operational callers.
online_sqlite_backup = online_backup
backup_database = online_backup
backup_sqlite_database = online_backup
backup_database_to_s3 = backup_to_s3
restore_sqlite_database = restore_database
integrity_check = check_integrity
run_integrity_check = check_integrity
