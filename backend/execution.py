"""Shared bounded execution and admission-control primitives.

The application runs several expensive, blocking workloads.  This module keeps
their worker pools bounded and gives callers an explicit rejection/timeout
contract.  A timeout never tries to kill a Python thread: the running work is
left to finish in its bounded pool so it cannot create an unbounded thread
leak.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Callable, Iterable, TypeVar


T = TypeVar("T")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float = 0.1) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


# Input limits are intentionally conservative.  They are shared by direct
# module callers as well as the HTTP routers, so a route cannot be bypassed by
# calling an expensive engine from another local entry point.
MAX_ANALYSIS_ANALYSTS = _env_int("TRADING_AGENT_MAX_ANALYSIS_ANALYSTS", 4)
MAX_ANALYSIS_DEBATE_ROUNDS = _env_int("TRADING_AGENT_MAX_ANALYSIS_DEBATE_ROUNDS", 3)
MAX_ANALYSIS_RISK_ROUNDS = _env_int("TRADING_AGENT_MAX_ANALYSIS_RISK_ROUNDS", 3)
MAX_BACKTEST_DATES = _env_int("TRADING_AGENT_MAX_BACKTEST_DATES", 30)
MAX_SCANNER_STOCKS = _env_int("TRADING_AGENT_MAX_SCANNER_STOCKS", 100)
MAX_RECOMMENDER_STOCKS = _env_int("TRADING_AGENT_MAX_RECOMMENDER_STOCKS", 100)
MAX_PERFORMANCE_STOCKS = _env_int("TRADING_AGENT_MAX_PERFORMANCE_STOCKS", 100)
MAX_PERFORMANCE_LOOKBACK_DAYS = _env_int(
    "TRADING_AGENT_MAX_PERFORMANCE_LOOKBACK_DAYS", 120
)
MAX_RECOMMENDER_BACKTEST_DATES = _env_int(
    "TRADING_AGENT_MAX_RECOMMENDER_BACKTEST_DATES", 30
)


# Explicit network/job deadlines.  These values are configurable for a
# deployment without changing application code.
YFINANCE_TIMEOUT_SECONDS = _env_float("TRADING_AGENT_YFINANCE_TIMEOUT_SECONDS", 15.0)
LLM_TIMEOUT_SECONDS = _env_float("TRADING_AGENT_LLM_TIMEOUT_SECONDS", 180.0)
ANALYSIS_JOB_TIMEOUT_SECONDS = _env_float(
    "TRADING_AGENT_ANALYSIS_TIMEOUT_SECONDS", 900.0
)
BACKTEST_JOB_TIMEOUT_SECONDS = _env_float(
    "TRADING_AGENT_BACKTEST_TIMEOUT_SECONDS", 1800.0
)
SCANNER_JOB_TIMEOUT_SECONDS = _env_float(
    "TRADING_AGENT_SCANNER_TIMEOUT_SECONDS", 180.0
)
RECOMMENDER_JOB_TIMEOUT_SECONDS = _env_float(
    "TRADING_AGENT_RECOMMENDER_TIMEOUT_SECONDS", 180.0
)
PERFORMANCE_JOB_TIMEOUT_SECONDS = _env_float(
    "TRADING_AGENT_PERFORMANCE_TIMEOUT_SECONDS", 300.0
)
RECOMMENDER_BACKTEST_TIMEOUT_SECONDS = _env_float(
    "TRADING_AGENT_RECOMMENDER_BACKTEST_TIMEOUT_SECONDS", 600.0
)


class InputLimitExceeded(ValueError):
    """Raised when a direct engine caller requests more work than allowed."""

    def __init__(self, field: str, requested: int, maximum: int):
        self.field = field
        self.requested = requested
        self.maximum = maximum
        super().__init__(
            f"{field} exceeds the maximum of {maximum} (received {requested})"
        )


class ExecutionRejected(RuntimeError):
    """Raised when an executor has no admission slot available."""

    def __init__(self, workload: str, capacity: int):
        self.workload = workload
        self.capacity = capacity
        super().__init__(
            f"{workload} execution is at capacity ({capacity} queued/running)"
        )


class ExecutionTimeout(TimeoutError):
    """Raised when a bounded job exceeds its caller-visible deadline."""

    def __init__(self, workload: str, timeout: float, job_id: str | None = None):
        self.workload = workload
        self.timeout = timeout
        self.job_id = job_id
        suffix = f" for job {job_id}" if job_id else ""
        super().__init__(
            f"{workload} execution timed out after {timeout:.1f}s{suffix}"
        )


class AdmissionController:
    """A non-blocking semaphore with observable capacity."""

    def __init__(self, name: str, capacity: int):
        if capacity < 1:
            raise ValueError("Admission capacity must be at least one")
        self.name = name
        self.capacity = capacity
        self._slots = threading.BoundedSemaphore(capacity)
        self._lock = threading.Lock()
        self._active = 0
        self._rejected = 0

    def acquire(self) -> None:
        """Acquire a slot or reject immediately rather than queue forever."""
        if not self._slots.acquire(blocking=False):
            with self._lock:
                self._rejected += 1
            raise ExecutionRejected(self.name, self.capacity)
        with self._lock:
            self._active += 1

    def release(self) -> None:
        with self._lock:
            if self._active:
                self._active -= 1
        self._slots.release()

    def snapshot(self) -> dict[str, int | str]:
        with self._lock:
            active = self._active
            rejected = self._rejected
        return {
            "workload": self.name,
            "capacity": self.capacity,
            "active": active,
            "available": self.capacity - active,
            "rejected": rejected,
        }


class BoundedExecutor:
    """Thread pool with a bounded total of queued and running tasks."""

    def __init__(self, name: str, max_workers: int, max_queue: int):
        if max_workers < 1 or max_queue < 0:
            raise ValueError("Executor limits must be positive")
        self.name = name
        self.max_workers = max_workers
        self.max_queue = max_queue
        self.admission = AdmissionController(name, max_workers + max_queue)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"trading-agent-{name}",
        )
        self._lock = threading.Lock()
        self._closed = False
        self._submitted = 0
        self._completed = 0
        self._failed = 0

    @property
    def capacity(self) -> int:
        return self.max_workers + self.max_queue

    def submit(self, fn: Callable[..., T], *args, **kwargs) -> Future[T]:
        """Submit work only when a bounded admission slot is available."""
        with self._lock:
            if self._closed:
                raise RuntimeError(f"{self.name} executor is shut down")
        self.admission.acquire()

        def run() -> T:
            try:
                result = fn(*args, **kwargs)
            except BaseException:
                with self._lock:
                    self._failed += 1
                raise
            else:
                with self._lock:
                    self._completed += 1
                return result
            finally:
                self.admission.release()

        try:
            future = self._executor.submit(run)
        except BaseException:
            self.admission.release()
            raise
        with self._lock:
            self._submitted += 1
        return future

    def snapshot(self) -> dict[str, int | str]:
        with self._lock:
            submitted = self._submitted
            completed = self._completed
            failed = self._failed
            closed = self._closed
        return {
            **self.admission.snapshot(),
            "max_workers": self.max_workers,
            "max_queue": self.max_queue,
            "submitted": submitted,
            "completed": completed,
            "failed": failed,
            "closed": int(closed),
        }

    def shutdown(self, wait: bool = True) -> None:
        """Stop accepting work without cancelling running Python threads."""
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait)


@dataclass(frozen=True)
class WorkloadConfig:
    max_workers: int
    max_queue: int


_WORKLOAD_DEFAULTS: dict[str, WorkloadConfig] = {
    # Job pools limit concurrent expensive requests.
    "analysis": WorkloadConfig(2, 2),
    "backtest": WorkloadConfig(1, 1),
    "scanner": WorkloadConfig(2, 2),
    "recommender": WorkloadConfig(2, 2),
    "performance": WorkloadConfig(2, 2),
    "recommender-backtest": WorkloadConfig(1, 1),
    # Worker pools are shared by all requests of the same workload.  Their
    # queue is large enough for the explicit input cap, but never unbounded.
    "scanner-worker": WorkloadConfig(8, MAX_SCANNER_STOCKS),
    "recommender-worker": WorkloadConfig(8, MAX_RECOMMENDER_STOCKS),
    "performance-worker": WorkloadConfig(8, MAX_PERFORMANCE_STOCKS),
    "recommender-backtest-worker": WorkloadConfig(8, MAX_RECOMMENDER_STOCKS),
}

_EXECUTORS: dict[str, BoundedExecutor] = {}
_EXECUTORS_LOCK = threading.Lock()


def _workload_config(name: str) -> WorkloadConfig:
    defaults = _WORKLOAD_DEFAULTS.get(name, WorkloadConfig(2, 2))
    env_prefix = "TRADING_AGENT_" + name.upper().replace("-", "_")
    max_workers = _env_int(f"{env_prefix}_MAX_WORKERS", defaults.max_workers)
    max_queue = _env_int(
        f"{env_prefix}_MAX_QUEUE", defaults.max_queue, minimum=0
    )
    return WorkloadConfig(max_workers, max_queue)


def get_executor(name: str) -> BoundedExecutor:
    """Return the process-shared bounded executor for a workload."""
    with _EXECUTORS_LOCK:
        executor = _EXECUTORS.get(name)
        if executor is None:
            limits = _workload_config(name)
            executor = BoundedExecutor(
                name,
                max_workers=limits.max_workers,
                max_queue=limits.max_queue,
            )
            _EXECUTORS[name] = executor
        return executor


def submit_job(
    workload: str,
    fn: Callable[..., T],
    *args,
    job_id: str | None = None,
    metadata: dict | None = None,
    **kwargs,
) -> Future[T]:
    """Submit a job and emit structured lifecycle events."""
    from backend.observability import (
        log_job_completed,
        log_job_failure,
        log_job_started,
        request_id_context,
    )

    job_id = job_id or request_id_context.get()

    def run() -> T:
        started = time.monotonic()
        log_job_started(workload, job_id=job_id, metadata=metadata)
        try:
            result = fn(*args, **kwargs)
        except BaseException as exc:
            log_job_failure(
                workload,
                job_id=job_id,
                error=exc,
                duration_ms=round((time.monotonic() - started) * 1000, 1),
                metadata=metadata,
            )
            raise
        log_job_completed(
            workload,
            job_id=job_id,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
            metadata=metadata,
        )
        return result

    return get_executor(workload).submit(run)


def wait_for(
    future: Future[T],
    timeout: float,
    *,
    workload: str,
    job_id: str | None = None,
) -> T:
    """Wait with a deadline; deliberately do not cancel the underlying work."""
    try:
        return future.result(timeout=timeout)
    except FutureTimeout as exc:
        from backend.observability import log_job_timeout

        log_job_timeout(workload, job_id=job_id, timeout=timeout)
        raise ExecutionTimeout(workload, timeout, job_id=job_id) from exc


def run_with_timeout(
    workload: str,
    fn: Callable[..., T],
    *args,
    timeout: float,
    job_id: str | None = None,
    metadata: dict | None = None,
    **kwargs,
) -> T:
    """Run a bounded job and wait for its caller-visible deadline."""
    future = submit_job(
        workload,
        fn,
        *args,
        job_id=job_id,
        metadata=metadata,
        **kwargs,
    )
    return wait_for(future, timeout, workload=workload, job_id=job_id)


def deadline_after(timeout: float) -> float:
    return time.monotonic() + timeout


def check_deadline(
    deadline: float | None,
    *,
    workload: str,
    job_id: str | None = None,
) -> None:
    """Cooperative deadline check for long loops.

    This is intentionally cooperative.  It never attempts to interrupt a
    running Python thread or an in-flight vendor request.
    """
    if deadline is not None and time.monotonic() >= deadline:
        from backend.observability import log_job_timeout

        log_job_timeout(
            workload,
            job_id=job_id,
            timeout=0,
            reason="cooperative deadline reached",
        )
        raise ExecutionTimeout(workload, 0, job_id=job_id)


def bounded_items(items: Iterable[T], maximum: int) -> list[T]:
    """Return a deterministic prefix of an explicitly capped input."""
    return list(items)[:maximum]


def shutdown_executors(wait: bool = True) -> None:
    """Shutdown all shared pools, primarily for orderly process teardown/tests."""
    with _EXECUTORS_LOCK:
        executors = list(_EXECUTORS.values())
        _EXECUTORS.clear()
    for executor in executors:
        executor.shutdown(wait=wait)
