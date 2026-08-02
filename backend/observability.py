"""Structured request and background-job observability."""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware


logger = logging.getLogger("indian_trading_agent")
request_id_context: ContextVar[str | None] = ContextVar(
    "trading_agent_request_id", default=None
)


def _error_fields(error: Any) -> dict[str, str]:
    if error is None:
        return {}
    if isinstance(error, BaseException):
        return {
            "error_type": type(error).__name__,
            "error": str(error)[:500],
        }
    return {"error": str(error)[:500]}


def log_structured(
    event: str,
    *,
    level: int = logging.INFO,
    **fields,
) -> dict[str, Any]:
    """Emit one JSON log record and return the payload for direct callers/tests."""
    payload: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
    }
    request_id = request_id_context.get()
    if request_id and "request_id" not in fields:
        fields["request_id"] = request_id
    payload.update({key: value for key, value in fields.items() if value is not None})
    logger.log(
        level,
        json.dumps(payload, sort_keys=True, default=str),
        extra=payload,
    )
    return payload


def log_request_failure(
    *,
    request_id: str | None,
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    error: Any = None,
) -> dict[str, Any]:
    return log_structured(
        "request_failed",
        level=logging.ERROR if status_code >= 500 else logging.WARNING,
        request_id=request_id,
        method=method,
        path=path,
        status_code=status_code,
        duration_ms=duration_ms,
        **_error_fields(error),
    )


def log_job_started(
    workload: str,
    *,
    job_id: str | None = None,
    metadata: dict | None = None,
) -> dict[str, Any]:
    return log_structured(
        "job_started",
        workload=workload,
        job_id=job_id,
        metadata=metadata,
    )


def log_job_completed(
    workload: str,
    *,
    job_id: str | None = None,
    duration_ms: float | None = None,
    metadata: dict | None = None,
) -> dict[str, Any]:
    return log_structured(
        "job_completed",
        workload=workload,
        job_id=job_id,
        duration_ms=duration_ms,
        metadata=metadata,
    )


def log_job_failure(
    workload: str,
    *,
    job_id: str | None = None,
    error: Any = None,
    duration_ms: float | None = None,
    metadata: dict | None = None,
) -> dict[str, Any]:
    return log_structured(
        "job_failed",
        level=logging.ERROR,
        workload=workload,
        job_id=job_id,
        duration_ms=duration_ms,
        metadata=metadata,
        **_error_fields(error),
    )


def log_job_timeout(
    workload: str,
    *,
    job_id: str | None = None,
    timeout: float,
    reason: str | None = None,
) -> dict[str, Any]:
    return log_structured(
        "job_timed_out",
        level=logging.WARNING,
        workload=workload,
        job_id=job_id,
        timeout_seconds=timeout,
        reason=reason,
    )


def log_job_rejected(
    workload: str,
    *,
    job_id: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    return log_structured(
        "job_rejected",
        level=logging.WARNING,
        workload=workload,
        job_id=job_id,
        reason=reason,
    )


class RequestObservabilityMiddleware(BaseHTTPMiddleware):
    """Attach request IDs and log every failed HTTP response."""

    async def dispatch(self, request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        token = request_id_context.set(request_id)
        started = time.monotonic()
        try:
            try:
                response = await call_next(request)
            except Exception as exc:
                log_request_failure(
                    request_id=request_id,
                    method=request.method,
                    path=request.url.path,
                    status_code=500,
                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                    error=exc,
                )
                raise

            response.headers["X-Request-ID"] = request_id
            if response.status_code >= 400:
                log_request_failure(
                    request_id=request_id,
                    method=request.method,
                    path=request.url.path,
                    status_code=response.status_code,
                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                )
            return response
        finally:
            request_id_context.reset(token)
