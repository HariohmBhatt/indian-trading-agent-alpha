"""SQLite-backed job status, leases, idempotency, and alert suppression."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.db import get_db


JOB_STATUS_TABLE = "scheduled_job_status"
JOB_LEASE_TABLE = "scheduled_job_leases"
JOB_ALERT_TABLE = "scheduled_job_alerts"


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return _utc(value).isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, default=str)


def ensure_job_tables() -> None:
    """Create only the worker-owned tables.

    This is intentionally additive and local to the scheduler.  It does not
    alter or migrate any existing application table.
    """

    with get_db() as conn:
        conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS {JOB_STATUS_TABLE} (
                job_name TEXT PRIMARY KEY,
                run_key TEXT,
                status TEXT NOT NULL DEFAULT 'never',
                attempt INTEGER NOT NULL DEFAULT 0,
                last_started_at TEXT,
                last_finished_at TEXT,
                last_success_at TEXT,
                next_retry_at TEXT,
                last_result_json TEXT,
                last_error TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS {JOB_LEASE_TABLE} (
                job_name TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                run_key TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                lease_until TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS {JOB_ALERT_TABLE} (
                alert_key TEXT PRIMARY KEY,
                job_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


@dataclass(frozen=True)
class LeaseClaim:
    acquired: bool
    reason: str
    attempt: int
    run_key: str


class JobStateStore:
    """Atomic state transitions for one or more worker processes."""

    def __init__(self) -> None:
        ensure_job_tables()

    def claim(
        self,
        *,
        job_name: str,
        run_key: str,
        owner_id: str,
        now: datetime,
        lease_seconds: int,
        max_attempts: int,
    ) -> LeaseClaim:
        """Claim a scheduled run, atomically enforcing lease and idempotency."""

        now = _utc(now)
        now_text = _iso(now)
        lease_until = now + timedelta(seconds=lease_seconds)

        with get_db() as conn:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("BEGIN IMMEDIATE")

            status_row = conn.execute(
                f"SELECT * FROM {JOB_STATUS_TABLE} WHERE job_name = ?",
                (job_name,),
            ).fetchone()

            previous_attempt = 0
            if status_row and status_row["run_key"] == run_key:
                previous_attempt = int(status_row["attempt"] or 0)
                if status_row["status"] in {"success", "skipped"}:
                    return LeaseClaim(False, "idempotent", previous_attempt, run_key)
                if (
                    status_row["status"] == "failed"
                    and previous_attempt >= max_attempts
                ):
                    return LeaseClaim(False, "attempts_exhausted", previous_attempt, run_key)
                retry_at = _parse(status_row["next_retry_at"])
                if retry_at and retry_at > now:
                    return LeaseClaim(False, "retry_not_due", previous_attempt, run_key)

            lease_row = conn.execute(
                f"SELECT * FROM {JOB_LEASE_TABLE} WHERE job_name = ?",
                (job_name,),
            ).fetchone()
            if lease_row and _parse(lease_row["lease_until"]) > now:
                return LeaseClaim(False, "lease_held", previous_attempt, run_key)

            if status_row and status_row["run_key"] == run_key and previous_attempt >= max_attempts:
                return LeaseClaim(False, "attempts_exhausted", previous_attempt, run_key)

            attempt = previous_attempt + 1
            conn.execute(
                f"""
                INSERT INTO {JOB_STATUS_TABLE}
                    (job_name, run_key, status, attempt, last_started_at,
                     next_retry_at, last_error, updated_at)
                VALUES (?, ?, 'running', ?, ?, NULL, NULL, ?)
                ON CONFLICT(job_name) DO UPDATE SET
                    run_key = excluded.run_key,
                    status = excluded.status,
                    attempt = excluded.attempt,
                    last_started_at = excluded.last_started_at,
                    next_retry_at = excluded.next_retry_at,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (job_name, run_key, attempt, now_text, now_text),
            )
            conn.execute(
                f"""
                INSERT INTO {JOB_LEASE_TABLE}
                    (job_name, owner_id, run_key, acquired_at, lease_until)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_name) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    run_key = excluded.run_key,
                    acquired_at = excluded.acquired_at,
                    lease_until = excluded.lease_until
                """,
                (job_name, owner_id, run_key, now_text, _iso(lease_until)),
            )
            return LeaseClaim(True, "acquired", attempt, run_key)

    def begin_retry(
        self,
        *,
        job_name: str,
        run_key: str,
        attempt: int,
        now: datetime,
    ) -> None:
        """Move a failed attempt back to running while retaining its lease."""

        now_text = _iso(now)
        with get_db() as conn:
            conn.execute(
                f"""
                UPDATE {JOB_STATUS_TABLE}
                   SET status = 'running',
                       attempt = ?,
                       last_started_at = ?,
                       next_retry_at = NULL,
                       updated_at = ?
                 WHERE job_name = ? AND run_key = ?
                """,
                (attempt, now_text, now_text, job_name, run_key),
            )

    def finish(
        self,
        *,
        job_name: str,
        run_key: str,
        owner_id: str,
        status: str,
        result: dict[str, Any],
        now: datetime,
    ) -> None:
        """Persist a terminal result and release only this owner's lease."""

        if status not in {"success", "skipped"}:
            raise ValueError(f"Unsupported terminal job status: {status}")
        now_text = _iso(now)
        success_at = now_text if status == "success" else None
        with get_db() as conn:
            conn.execute(
                f"""
                UPDATE {JOB_STATUS_TABLE}
                   SET status = ?,
                       last_finished_at = ?,
                       last_success_at = COALESCE(?, last_success_at),
                       next_retry_at = NULL,
                       last_result_json = ?,
                       last_error = NULL,
                       updated_at = ?
                 WHERE job_name = ? AND run_key = ?
                """,
                (
                    status,
                    now_text,
                    success_at,
                    _json(result),
                    now_text,
                    job_name,
                    run_key,
                ),
            )
            conn.execute(
                f"DELETE FROM {JOB_LEASE_TABLE} WHERE job_name = ? AND owner_id = ?",
                (job_name, owner_id),
            )

    def fail(
        self,
        *,
        job_name: str,
        run_key: str,
        owner_id: str,
        error: str,
        now: datetime,
        retry_at: datetime | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        """Persist a failed attempt; terminal failures also release the lease."""

        now_text = _iso(now)
        retry_text = _iso(retry_at) if retry_at else None
        with get_db() as conn:
            conn.execute(
                f"""
                UPDATE {JOB_STATUS_TABLE}
                   SET status = 'failed',
                       last_finished_at = ?,
                       next_retry_at = ?,
                       last_result_json = ?,
                       last_error = ?,
                       updated_at = ?
                 WHERE job_name = ? AND run_key = ?
                """,
                (
                    now_text,
                    retry_text,
                    _json(result),
                    error[:2000],
                    now_text,
                    job_name,
                    run_key,
                ),
            )
            if retry_at is None:
                conn.execute(
                    f"DELETE FROM {JOB_LEASE_TABLE} WHERE job_name = ? AND owner_id = ?",
                    (job_name, owner_id),
                )

    def get_status(self, job_name: str) -> dict[str, Any] | None:
        with get_db() as conn:
            row = conn.execute(
                f"SELECT * FROM {JOB_STATUS_TABLE} WHERE job_name = ?",
                (job_name,),
            ).fetchone()
        if not row:
            return None
        result = dict(row)
        if result.get("last_result_json"):
            try:
                result["last_result"] = json.loads(result["last_result_json"])
            except (TypeError, ValueError):
                result["last_result"] = None
        else:
            result["last_result"] = None
        return result

    def list_status(self) -> list[dict[str, Any]]:
        with get_db() as conn:
            rows = conn.execute(
                f"SELECT * FROM {JOB_STATUS_TABLE} ORDER BY job_name"
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_alert(self, *, alert_key: str, job_name: str, now: datetime) -> bool:
        """Return true exactly once for an alert key across worker processes."""

        with get_db() as conn:
            cursor = conn.execute(
                f"""
                INSERT OR IGNORE INTO {JOB_ALERT_TABLE}
                    (alert_key, job_name, created_at)
                VALUES (?, ?, ?)
                """,
                (alert_key, job_name, _iso(now)),
            )
            return cursor.rowcount == 1
