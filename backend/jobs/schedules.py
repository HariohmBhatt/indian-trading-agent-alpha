"""Timezone-aware local schedules used by the freshness worker."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, date, time, timedelta, timezone
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "Asia/Kolkata"
WEEKDAYS = frozenset(range(5))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@dataclass(frozen=True)
class ScheduleSlot:
    """A concrete occurrence of a local schedule."""

    scheduled_at: datetime
    run_key: str


@dataclass(frozen=True)
class LocalSchedule:
    """One local wall-clock time on selected weekdays."""

    at: time
    weekdays: frozenset[int] = WEEKDAYS
    timezone_name: str = DEFAULT_TIMEZONE

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def slot_for(self, now: datetime) -> ScheduleSlot | None:
        """Return today's slot once its local time has been reached."""

        local_now = _aware(now).astimezone(self.timezone)
        if local_now.weekday() not in self.weekdays:
            return None

        candidate = datetime.combine(
            local_now.date(),
            self.at,
            tzinfo=self.timezone,
        )
        if local_now < candidate:
            return None
        return ScheduleSlot(
            scheduled_at=candidate,
            run_key=candidate.isoformat(timespec="minutes"),
        )

    def next_after(self, now: datetime) -> datetime:
        """Return the next occurrence, preserving the configured timezone."""

        local_now = _aware(now).astimezone(self.timezone)
        for offset in range(0, 8):
            candidate_date = local_now.date() + timedelta(days=offset)
            if candidate_date.weekday() not in self.weekdays:
                continue
            candidate = datetime.combine(
                candidate_date,
                self.at,
                tzinfo=self.timezone,
            )
            if candidate > local_now:
                return candidate
        raise RuntimeError("Unable to find the next scheduled occurrence")


def timezone_name(value: str | None = None) -> str:
    """Read and validate the worker timezone without relying on host TZ."""

    selected = value or os.getenv(
        "TRADING_AGENT_SCHEDULER_TIMEZONE",
        DEFAULT_TIMEZONE,
    )
    ZoneInfo(selected)
    return selected


def local_date(now: datetime, timezone_name_value: str = DEFAULT_TIMEZONE) -> date:
    """Return a date in the configured schedule timezone."""

    return _aware(now).astimezone(ZoneInfo(timezone_name_value)).date()
