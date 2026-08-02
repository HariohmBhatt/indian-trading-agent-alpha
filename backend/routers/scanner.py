"""Market Scanner API endpoints."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from backend.scanner import run_scan, UNIVERSES
from backend.execution import (
    SCANNER_JOB_TIMEOUT_SECONDS,
    ExecutionRejected,
    ExecutionTimeout,
    run_with_timeout,
)
from backend.observability import log_job_rejected

router = APIRouter(prefix="/api/scanner", tags=["scanner"])


class ScanRequest(BaseModel):
    universe: str = Field(default="nifty50", max_length=32)
    strategies: list[str] = Field(
        default_factory=lambda: ["gap", "volume", "breakout"],
        min_length=1,
        max_length=3,
    )
    gap_threshold: float = Field(default=2.0, gt=0, le=20)
    volume_multiplier: float = Field(default=2.0, gt=0, le=20)
    breakout_lookback: int = Field(default=20, ge=5, le=60)


@router.post("/run")
def run_scanner(req: ScanRequest):
    """Run a market scan synchronously. Returns results directly."""
    try:
        return run_with_timeout(
            "scanner",
            run_scan,
            universe=req.universe,
            strategies=req.strategies,
            gap_threshold=req.gap_threshold,
            volume_multiplier=req.volume_multiplier,
            breakout_lookback=req.breakout_lookback,
            timeout=SCANNER_JOB_TIMEOUT_SECONDS,
            metadata={"universe": req.universe},
        )
    except ExecutionRejected as exc:
        log_job_rejected("scanner", reason=str(exc))
        raise HTTPException(
            status_code=429,
            detail="Scanner capacity is currently full; retry later.",
            headers={"Retry-After": "30"},
        ) from exc
    except ExecutionTimeout as exc:
        raise HTTPException(
            status_code=504,
            detail="Scanner exceeded its execution deadline; retry later.",
        ) from exc


@router.get("/universes/list")
def list_universes():
    """List available stock universes."""
    return {
        name: {"count": len(stocks), "sample": stocks[:5]}
        for name, stocks in UNIVERSES.items()
    }
