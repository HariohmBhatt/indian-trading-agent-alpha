"""The four scheduled freshness jobs and their IST operating windows."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Callable

from backend.jobs.schedules import LocalSchedule, WEEKDAYS, timezone_name


PORTFOLIO_JOB_NAME = "portfolio_freshness"
VERDICT_JOB_NAME = "verdict_freshness"
OUTCOME_JOB_NAME = "outcome_freshness"
CALENDAR_JOB_NAME = "calendar_freshness"


@dataclass(frozen=True)
class JobSpec:
    """Executable job metadata shared by the scheduler and state store."""

    name: str
    schedule: LocalSchedule
    function: Callable[..., dict[str, Any]]
    max_attempts: int = 3
    retry_delay_seconds: float = 5.0
    lease_seconds: int = 900
    freshness_max_age: timedelta = timedelta(hours=26)
    freshness_grace: timedelta = timedelta(minutes=30)


def build_job_specs(
    *,
    timezone_name_value: str | None = None,
    service_functions: dict[str, Callable[..., dict[str, Any]]] | None = None,
) -> list[JobSpec]:
    """Build fresh specs so tests can inject service-level fakes."""

    selected_timezone = timezone_name(timezone_name_value)
    service_functions = service_functions or {}

    from backend.jobs import services

    def service(name: str, default: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        return service_functions.get(name, default)

    lease_seconds = int(
        os.getenv("TRADING_AGENT_SCHEDULER_LEASE_SECONDS", "900")
    )

    return [
        JobSpec(
            name=PORTFOLIO_JOB_NAME,
            schedule=LocalSchedule(
                at=time(16, 0),
                weekdays=WEEKDAYS,
                timezone_name=selected_timezone,
            ),
            function=service(
                PORTFOLIO_JOB_NAME,
                services.run_portfolio_freshness_job,
            ),
            lease_seconds=max(60, lease_seconds),
            freshness_max_age=timedelta(hours=26),
        ),
        JobSpec(
            name=VERDICT_JOB_NAME,
            schedule=LocalSchedule(
                at=time(15, 45),
                weekdays=WEEKDAYS,
                timezone_name=selected_timezone,
            ),
            function=service(
                VERDICT_JOB_NAME,
                services.run_verdict_freshness_job,
            ),
            lease_seconds=max(60, min(lease_seconds, 900)),
            freshness_max_age=timedelta(hours=26),
        ),
        JobSpec(
            name=OUTCOME_JOB_NAME,
            schedule=LocalSchedule(
                at=time(16, 30),
                weekdays=WEEKDAYS,
                timezone_name=selected_timezone,
            ),
            function=service(
                OUTCOME_JOB_NAME,
                services.run_outcome_freshness_job,
            ),
            lease_seconds=max(60, min(lease_seconds, 900)),
            freshness_max_age=timedelta(hours=72),
        ),
        JobSpec(
            name=CALENDAR_JOB_NAME,
            schedule=LocalSchedule(
                at=time(8, 0),
                weekdays=frozenset({0}),
                timezone_name=selected_timezone,
            ),
            function=service(
                CALENDAR_JOB_NAME,
                services.run_calendar_freshness_job,
            ),
            lease_seconds=max(60, lease_seconds),
            freshness_max_age=timedelta(days=8),
            freshness_grace=timedelta(hours=2),
        ),
    ]


JOB_SPECS = build_job_specs()
