"""Long-running scheduled worker for IST freshness jobs."""

from __future__ import annotations

import argparse
import inspect
import json
import os
import time as time_module
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from backend.jobs.alerts import TelegramFreshnessAlertSink
from backend.jobs.definitions import JobSpec, build_job_specs
from backend.jobs.schedules import ScheduleSlot
from backend.jobs.state import JobStateStore, LeaseClaim


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class ScheduledWorker:
    """Poll schedules and execute each concrete slot at most once."""

    def __init__(
        self,
        *,
        specs: Iterable[JobSpec] | None = None,
        store: JobStateStore | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        alert_sink: Any | None = None,
        owner_id: str | None = None,
    ) -> None:
        self.specs = list(specs) if specs is not None else build_job_specs()
        self.store = store or JobStateStore()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep or time_module.sleep
        self.alert_sink = alert_sink or TelegramFreshnessAlertSink()
        self.owner_id = owner_id or f"worker-{uuid.uuid4().hex[:12]}"

    def run_once(
        self,
        *,
        now: datetime | None = None,
        only_jobs: set[str] | None = None,
    ) -> dict[str, Any]:
        """Run due slots and perform the corresponding freshness check."""

        now = _utc(now or self.clock())
        results: list[dict[str, Any]] = []
        for spec in self.specs:
            if only_jobs and spec.name not in only_jobs:
                continue
            slot = spec.schedule.slot_for(now)
            if slot is None:
                continue
            claim = self.store.claim(
                job_name=spec.name,
                run_key=slot.run_key,
                owner_id=self.owner_id,
                now=now,
                lease_seconds=spec.lease_seconds,
                max_attempts=spec.max_attempts,
            )
            if not claim.acquired:
                results.append(
                    {
                        "job_name": spec.name,
                        "run_key": slot.run_key,
                        "status": claim.reason,
                        "attempt": claim.attempt,
                    }
                )
                continue
            results.append(self._execute(spec, slot, claim, now))

        return {
            "observed_at": now.isoformat(timespec="seconds"),
            "owner_id": self.owner_id,
            "jobs": results,
            "alerts": self.check_freshness(now=now, only_jobs=only_jobs),
        }

    def _execute(
        self,
        spec: JobSpec,
        slot: ScheduleSlot,
        claim: LeaseClaim,
        now: datetime,
    ) -> dict[str, Any]:
        attempt = claim.attempt
        while True:
            try:
                result = self._invoke(spec.function, now=now, run_key=slot.run_key)
                if not isinstance(result, dict):
                    result = {"value": result}
                status = result.get("status", "success")
                if status not in {"success", "skipped"}:
                    raise RuntimeError(
                        result.get("reason") or f"Job returned status {status!r}"
                    )
                self.store.finish(
                    job_name=spec.name,
                    run_key=slot.run_key,
                    owner_id=self.owner_id,
                    status=status,
                    result=result,
                    now=now,
                )
                return {
                    "job_name": spec.name,
                    "run_key": slot.run_key,
                    "status": status,
                    "attempt": attempt,
                    "result": result,
                }
            except Exception as exc:
                retryable = attempt < spec.max_attempts
                retry_at = (
                    now + timedelta(seconds=spec.retry_delay_seconds * attempt)
                    if retryable
                    else None
                )
                self.store.fail(
                    job_name=spec.name,
                    run_key=slot.run_key,
                    owner_id=self.owner_id,
                    error=str(exc),
                    now=now,
                    retry_at=retry_at,
                )
                if retryable:
                    self.sleep(spec.retry_delay_seconds * attempt)
                    attempt += 1
                    self.store.begin_retry(
                        job_name=spec.name,
                        run_key=slot.run_key,
                        attempt=attempt,
                        now=now,
                    )
                    continue

                alert = self._emit_alert(
                    spec=spec,
                    slot=slot,
                    now=now,
                    status="failed",
                    detail=str(exc),
                )
                return {
                    "job_name": spec.name,
                    "run_key": slot.run_key,
                    "status": "failed",
                    "attempt": attempt,
                    "error": str(exc),
                    "alert": alert,
                }

    @staticmethod
    def _invoke(
        function: Callable[..., dict[str, Any]],
        *,
        now: datetime,
        run_key: str,
    ) -> dict[str, Any]:
        """Pass only supported keywords to simple test/service callables."""

        try:
            parameters = inspect.signature(function).parameters
        except (TypeError, ValueError):
            parameters = {}
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        kwargs = {}
        if accepts_kwargs or "now" in parameters:
            kwargs["now"] = now
        if accepts_kwargs or "run_key" in parameters:
            kwargs["run_key"] = run_key
        return function(**kwargs)

    def check_freshness(
        self,
        *,
        now: datetime | None = None,
        only_jobs: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Alert once when a due slot has no fresh successful completion."""

        now = _utc(now or self.clock())
        alerts: list[dict[str, Any]] = []
        for spec in self.specs:
            if only_jobs and spec.name not in only_jobs:
                continue
            slot = spec.schedule.slot_for(now)
            if slot is None or now < slot.scheduled_at + spec.freshness_grace:
                continue

            status = self.store.get_status(spec.name)
            if status and status.get("run_key") == slot.run_key:
                if status.get("status") == "success":
                    continue
                if status.get("status") == "failed":
                    # The terminal failure path already emitted a failure alert.
                    continue
                last_result = status.get("last_result") or {}
                if (
                    status.get("status") == "skipped"
                    and last_result.get("reason") == "kite_token_missing"
                ):
                    # Missing daily Kite tokens are expected and must not page.
                    continue

            detail = (
                "No successful completion recorded for the current scheduled slot"
                if not status
                else f"Last status was {status.get('status', 'unknown')}"
            )
            alert = self._emit_alert(
                spec=spec,
                slot=slot,
                now=now,
                status="stale",
                detail=detail,
            )
            alerts.append(
                {
                    "job_name": spec.name,
                    "run_key": slot.run_key,
                    "status": "stale",
                    "alert": alert,
                }
            )
        return alerts

    def _emit_alert(
        self,
        *,
        spec: JobSpec,
        slot: ScheduleSlot,
        now: datetime,
        status: str,
        detail: str,
    ) -> dict[str, Any]:
        alert_key = f"{spec.name}:{slot.run_key}:{status}"
        if not self.store.claim_alert(
            alert_key=alert_key,
            job_name=spec.name,
            now=now,
        ):
            return {"status": "suppressed", "reason": "already_alerted"}
        try:
            return self.alert_sink.alert(
                job_name=spec.name,
                status=status,
                scheduled_at=slot.scheduled_at,
                now=now,
                detail=detail,
            )
        except Exception as exc:
            # Notification is secondary to persisted job state.
            return {"status": "error", "reason": str(exc)[:500]}

    def run_forever(self, *, poll_seconds: float = 30.0) -> None:
        """Run until the container receives a termination signal."""

        while True:
            self.run_once()
            self.sleep(poll_seconds)


def run_worker_once(
    *,
    now: datetime | None = None,
    specs: Iterable[JobSpec] | None = None,
    store: JobStateStore | None = None,
    alert_sink: Any | None = None,
    only_jobs: set[str] | None = None,
) -> dict[str, Any]:
    """Convenience entry point for smoke tests and operator diagnostics."""

    return ScheduledWorker(
        specs=specs,
        store=store,
        alert_sink=alert_sink,
    ).run_once(now=now, only_jobs=only_jobs)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run scheduled freshness jobs")
    parser.add_argument("--once", action="store_true", help="run due jobs once and exit")
    parser.add_argument("--job", action="append", help="limit a one-shot run to a job name")
    parser.add_argument(
        "--at",
        dest="observed_at",
        help="test/operator clock in ISO-8601 format",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=float(os.getenv("TRADING_AGENT_SCHEDULER_POLL_SECONDS", "30")),
    )
    args = parser.parse_args(argv)

    from backend.db import ensure_db

    ensure_db()
    worker = ScheduledWorker()
    only_jobs = set(args.job or []) or None
    if args.once:
        result = worker.run_once(
            now=_parse_datetime(args.observed_at),
            only_jobs=only_jobs,
        )
        print(json.dumps(result, sort_keys=True, default=str), flush=True)
        return 1 if any(job.get("status") == "failed" for job in result["jobs"]) else 0

    worker.run_forever(poll_seconds=args.poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
