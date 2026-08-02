"""Service-level functions executed by the scheduled worker.

These functions call existing domain services directly.  They do not make
HTTP requests to the local FastAPI app, which keeps the worker useful during
backend restarts and makes each job straightforward to test with fakes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from backend.jobs.interfaces import PortfolioSnapshot, PortfolioSnapshotProvider


def _utc_now(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class CurrentKitePortfolioProvider:
    """Adapter for the current Kite/positions APIs.

    The token check intentionally happens before any holdings or positions
    call.  A future Phase 07 provider can implement the same protocol and be
    injected into ``run_portfolio_freshness_job``.
    """

    def fetch(self, *, now: datetime) -> PortfolioSnapshot:
        from backend.brokers.kite import get_kite_status

        kite_status = get_kite_status()
        if not kite_status.get("connected_today"):
            return PortfolioSnapshot(
                status="skipped",
                reason="kite_token_missing",
                metadata={"kite_status": kite_status},
            )

        from backend.positions import get_positions_view, sync_positions_from_kite

        sync_result = sync_positions_from_kite()
        view = get_positions_view()
        return PortfolioSnapshot(
            status="ready",
            positions=view.get("positions", []),
            synced_at=sync_result.get("synced_at"),
            metadata={
                "sync": sync_result,
                "kite_status": kite_status,
                "snapshot_at": _utc_now(now).isoformat(timespec="seconds"),
            },
        )


def notify_portfolio_review(review: dict[str, Any]) -> dict[str, Any]:
    """Send a review when Telegram is configured; otherwise record a skip."""

    from backend.notifications.telegram import (
        build_portfolio_review_message,
        get_app_url,
        get_telegram_status,
        portfolio_keyboard,
        send_html_message_with_optional_buttons,
    )

    if not get_telegram_status().get("enabled"):
        return {"status": "skipped", "reason": "telegram_not_configured"}

    result = send_html_message_with_optional_buttons(
        build_portfolio_review_message(review, app_url=get_app_url()),
        reply_markup=portfolio_keyboard(),
    )
    return {
        "status": "sent",
        "message_id": result.get("result", {}).get("message_id"),
    }


def run_portfolio_freshness_job(
    *,
    now: datetime | None = None,
    run_key: str | None = None,
    provider: PortfolioSnapshotProvider | None = None,
    review_builder: Callable[[list[dict[str, Any]]], dict[str, Any]] | None = None,
    notifier: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Synchronize, persist, and notify one portfolio freshness snapshot."""

    now = _utc_now(now)
    provider = provider or CurrentKitePortfolioProvider()
    snapshot = provider.fetch(now=now)

    if snapshot.status == "skipped":
        return {
            "status": "skipped",
            "reason": snapshot.reason or "portfolio_source_skipped",
            "freshness": "not_applicable",
            "positions": 0,
        }
    if snapshot.status != "ready":
        raise RuntimeError(snapshot.reason or "Portfolio snapshot is not ready")

    if review_builder is None:
        from backend.equity_portfolio import create_and_save_review

        review_builder = lambda positions: create_and_save_review(positions, enrich=True)

    review = review_builder(snapshot.positions)
    notification = (
        notifier(review) if notifier is not None else notify_portfolio_review(review)
    )
    return {
        "status": "success",
        "review_id": review.get("review_id"),
        "positions": len(snapshot.positions),
        "synced_at": snapshot.synced_at,
        "run_key": run_key,
        "notification": notification,
    }


def run_verdict_freshness_job(
    *,
    now: datetime | None = None,
    run_key: str | None = None,
    snapshotter: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Capture the daily verdict through the existing idempotent service."""

    del now
    if snapshotter is None:
        from backend.verdict_calibration import snapshot_today

        snapshotter = snapshot_today
    snapshot = snapshotter()
    if snapshot.get("status") == "error":
        raise RuntimeError(snapshot.get("reason") or "Verdict snapshot failed")
    return {
        "status": "success",
        "run_key": run_key,
        "snapshot": snapshot,
    }


def run_outcome_freshness_job(
    *,
    now: datetime | None = None,
    run_key: str | None = None,
    backfiller: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Backfill ripe verdict outcomes through the existing service."""

    del now
    if backfiller is None:
        from backend.verdict_calibration import backfill_outcomes

        backfiller = backfill_outcomes
    result = backfiller()
    if result.get("status") == "error":
        raise RuntimeError(result.get("reason") or "Outcome backfill failed")
    return {
        "status": "success",
        "run_key": run_key,
        "outcomes": result,
    }


def run_calendar_freshness_job(
    *,
    now: datetime | None = None,
    run_key: str | None = None,
    tickers: list[str] | None = None,
    refresher: Callable[[list[str]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Refresh the cached earnings calendar for the configured universe."""

    del now
    if tickers is None:
        from backend.scanner import UNIVERSES

        tickers = list(UNIVERSES.get("nifty100", []))
    if refresher is None:
        from backend.calendar_data import refresh_earnings_calendar

        refresher = refresh_earnings_calendar
    result = refresher(tickers)
    if result.get("status") == "error":
        raise RuntimeError(result.get("reason") or "Calendar refresh failed")
    return {
        "status": "success",
        "run_key": run_key,
        "calendar": result,
        "tickers": len(tickers),
    }
