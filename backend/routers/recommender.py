"""Recommendation Engine API — combines all strategies into ranked trade ideas."""

from fastapi import APIRouter, Query, HTTPException
from backend.recommender import recommend, _analyze_stock
from backend.execution import (
    RECOMMENDER_JOB_TIMEOUT_SECONDS,
    ExecutionRejected,
    ExecutionTimeout,
    run_with_timeout,
)
from backend.observability import log_job_rejected

router = APIRouter(prefix="/api/recommend", tags=["recommend"])


@router.get("/")
def get_recommendations(
    universe: str = Query(
        "nifty100",
        max_length=32,
        description="nifty50, nifty100, or bse250",
    ),
    min_signals: int = Query(
        2,
        ge=1,
        le=10,
        description="Min aligned signals to recommend",
    ),
):
    """Get ranked trade recommendations combining all strategies."""
    try:
        return run_with_timeout(
            "recommender",
            recommend,
            universe,
            min_signals,
            timeout=RECOMMENDER_JOB_TIMEOUT_SECONDS,
            metadata={"universe": universe},
        )
    except ExecutionRejected as exc:
        log_job_rejected("recommender", reason=str(exc))
        raise HTTPException(
            status_code=429,
            detail="Recommendation capacity is currently full; retry later.",
            headers={"Retry-After": "30"},
        ) from exc
    except ExecutionTimeout as exc:
        raise HTTPException(
            status_code=504,
            detail="Recommendation scan exceeded its execution deadline; retry later.",
        ) from exc


@router.get("/stock/{ticker}")
def analyze_single_stock(ticker: str):
    """Get recommendation for a single stock."""
    try:
        result = run_with_timeout(
            "recommender",
            _analyze_stock,
            ticker,
            timeout=RECOMMENDER_JOB_TIMEOUT_SECONDS,
            metadata={"ticker": ticker},
        )
    except ExecutionRejected as exc:
        log_job_rejected("recommender", reason=str(exc))
        raise HTTPException(
            status_code=429,
            detail="Recommendation capacity is currently full; retry later.",
            headers={"Retry-After": "30"},
        ) from exc
    except ExecutionTimeout as exc:
        raise HTTPException(
            status_code=504,
            detail="Stock analysis exceeded its execution deadline; retry later.",
        ) from exc
    if not result:
        return {"error": f"Could not analyze {ticker}"}
    return result
