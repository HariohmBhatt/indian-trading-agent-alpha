"""Small interfaces shared by scheduled jobs and their worker.

The scheduler deliberately depends on these contracts instead of importing a
future portfolio-freshness implementation.  Phase 07 can provide a stronger
portfolio snapshot provider without changing the worker's lease, retry, or
schedule behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class PortfolioSnapshot:
    """Result returned by a portfolio source before review generation."""

    status: str
    positions: list[dict[str, Any]] = field(default_factory=list)
    synced_at: str | None = None
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class PortfolioSnapshotProvider(Protocol):
    """Boundary for current and future portfolio freshness implementations."""

    def fetch(self, *, now: datetime) -> PortfolioSnapshot:
        """Return a confirmed snapshot, or a safe non-ready result."""


class FreshnessAlertSink(Protocol):
    """Destination for job failure and stale-freshness alerts."""

    def alert(
        self,
        *,
        job_name: str,
        status: str,
        scheduled_at: datetime,
        now: datetime,
        detail: str,
    ) -> dict[str, Any]:
        """Emit an alert without making the worker fail if delivery is absent."""
