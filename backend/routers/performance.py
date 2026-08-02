"""Strategy Performance API endpoints — measure historical success rate of strategies."""

from fastapi import APIRouter, Query, HTTPException
from backend.performance import (
    measure_gap_strategy,
    measure_volume_strategy,
    measure_breakout_strategy,
    measure_sr_bounce_strategy,
    measure_all_strategies,
)
from backend.execution import (
    MAX_PERFORMANCE_LOOKBACK_DAYS,
    PERFORMANCE_JOB_TIMEOUT_SECONDS,
    ExecutionRejected,
    ExecutionTimeout,
    run_with_timeout,
)
from backend.observability import log_job_rejected

router = APIRouter(prefix="/api/performance", tags=["performance"])


def _run_measurement(function, *args, universe: str, **kwargs):
    try:
        return run_with_timeout(
            "performance",
            function,
            *args,
            timeout=PERFORMANCE_JOB_TIMEOUT_SECONDS,
            metadata={"universe": universe},
            **kwargs,
        )
    except ExecutionRejected as exc:
        log_job_rejected("performance", reason=str(exc))
        raise HTTPException(
            status_code=429,
            detail="Performance capacity is currently full; retry later.",
            headers={"Retry-After": "30"},
        ) from exc
    except ExecutionTimeout as exc:
        raise HTTPException(
            status_code=504,
            detail="Performance analysis exceeded its execution deadline; retry later.",
        ) from exc


@router.get("/all")
def run_all_strategies(
    universe: str = Query("nifty50", description="nifty50, nifty100, or bse250"),
    lookback_days: int = Query(
        60,
        ge=1,
        le=MAX_PERFORMANCE_LOOKBACK_DAYS,
        description="How many days of history to analyze",
    ),
):
    """Measure performance of all 4 strategies. Takes 30-120 seconds depending on universe."""
    return _run_measurement(
        measure_all_strategies,
        universe,
        lookback_days,
        [1, 3, 5],
        universe=universe,
    )


@router.get("/gap")
def run_gap_strategy(
    universe: str = Query("nifty50"),
    lookback_days: int = Query(60, ge=1, le=MAX_PERFORMANCE_LOOKBACK_DAYS),
    gap_threshold: float = Query(2.0, gt=0, le=20),
):
    return _run_measurement(
        measure_gap_strategy,
        universe,
        lookback_days,
        gap_threshold,
        [1, 3, 5],
        universe=universe,
    )


@router.get("/volume")
def run_volume_strategy(
    universe: str = Query("nifty50"),
    lookback_days: int = Query(60, ge=1, le=MAX_PERFORMANCE_LOOKBACK_DAYS),
    volume_multiplier: float = Query(2.0, gt=0, le=20),
):
    return _run_measurement(
        measure_volume_strategy,
        universe,
        lookback_days,
        volume_multiplier,
        [1, 3, 5],
        universe=universe,
    )


@router.get("/breakout")
def run_breakout_strategy(
    universe: str = Query("nifty50"),
    lookback_days: int = Query(60, ge=1, le=MAX_PERFORMANCE_LOOKBACK_DAYS),
    breakout_window: int = Query(20, ge=1, le=60),
    require_volume: bool = Query(True),
):
    return _run_measurement(
        measure_breakout_strategy,
        universe,
        lookback_days,
        breakout_window,
        [1, 3, 5],
        require_volume,
        universe=universe,
    )


@router.get("/sr-bounce")
def run_sr_bounce_strategy(
    universe: str = Query("nifty50"),
    lookback_days: int = Query(
        90,
        ge=1,
        le=MAX_PERFORMANCE_LOOKBACK_DAYS,
    ),
):
    return _run_measurement(
        measure_sr_bounce_strategy,
        universe,
        lookback_days,
        3,
        [1, 3, 5],
        universe=universe,
    )
