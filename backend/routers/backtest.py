"""Backtest endpoints — run historical backtests with P&L tracking."""

import uuid
import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel, Field
from typing import Optional
from backend.ws import manager
from backend.db import (
    save_backtest_run,
    save_backtest_trade,
    get_backtest_run,
    get_backtest_trades,
    get_backtest_history,
)
from backend.backtest_engine import run_backtest, get_trading_dates
from tradingagents.utils.ticker import normalize_ticker
from tradingagents.default_config import DEFAULT_CONFIG
from backend.execution import (
    BACKTEST_JOB_TIMEOUT_SECONDS,
    MAX_BACKTEST_DATES,
    ExecutionRejected,
    InputLimitExceeded,
    deadline_after,
    submit_job,
)
from backend.observability import log_job_failure, log_job_rejected

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class BacktestRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=32)
    start_date: str = Field(min_length=10, max_length=10)
    end_date: str = Field(min_length=10, max_length=10)
    interval_days: int = Field(default=5, ge=1, le=20)
    initial_capital: float = Field(default=100000, gt=0, le=100_000_000)
    position_size_pct: float = Field(default=10, gt=0, le=100)
    enable_learning: bool = False


def _run_backtest_thread(backtest_id: str, req: BacktestRequest):
    """Run backtest in a background thread, streaming progress via WebSocket."""
    loop = asyncio.new_event_loop()
    ticker = normalize_ticker(req.ticker)
    try:
        # Generate and validate trading dates before admitting expensive work.
        dates = get_trading_dates(req.start_date, req.end_date, req.interval_days)
        if len(dates) > MAX_BACKTEST_DATES:
            raise InputLimitExceeded("backtest dates", len(dates), MAX_BACKTEST_DATES)
        if not dates:
            loop.run_until_complete(manager.send_event(backtest_id, {
                "type": "error", "message": "No trading dates found in the given range",
            }))
            return

        save_backtest_run(backtest_id, {
            "ticker": ticker,
            "initial_capital": req.initial_capital,
            "position_size_pct": req.position_size_pct,
            "enable_learning": req.enable_learning,
            "status": "running",
        })

        loop.run_until_complete(manager.send_event(backtest_id, {
            "type": "status",
            "message": f"Starting backtest: {ticker} | {len(dates)} dates | ₹{req.initial_capital:,.0f} capital",
            "total_dates": len(dates),
        }))

        config = DEFAULT_CONFIG.copy()

        def on_trade(trade):
            save_backtest_trade(backtest_id, trade)
            loop.run_until_complete(manager.send_event(backtest_id, {
                "type": "trade",
                **trade,
            }))

        def on_status(msg):
            loop.run_until_complete(manager.send_event(backtest_id, {
                "type": "status", "message": msg,
            }))

        summary, trades = run_backtest(
            ticker=req.ticker,
            dates=dates,
            initial_capital=req.initial_capital,
            position_size_pct=req.position_size_pct,
            enable_learning=req.enable_learning,
            config=config,
            on_trade_complete=on_trade,
            on_status=on_status,
            deadline=deadline_after(BACKTEST_JOB_TIMEOUT_SECONDS),
        )

        # Update run with final stats
        save_backtest_run(backtest_id, summary)

        loop.run_until_complete(manager.send_event(backtest_id, {
            "type": "complete",
            **summary,
        }))

    except Exception as e:
        try:
            save_backtest_run(backtest_id, {
                "ticker": ticker,
                "initial_capital": req.initial_capital,
                "position_size_pct": req.position_size_pct,
                "enable_learning": req.enable_learning,
                "status": "error",
                "error": str(e),
            })
        except Exception:
            pass
        log_job_failure(
            "backtest",
            job_id=backtest_id,
            error=e,
            metadata={"ticker": ticker},
        )
        loop.run_until_complete(manager.send_event(backtest_id, {
            "type": "error",
            "message": str(e),
            "failure_type": type(e).__name__,
        }))
    finally:
        loop.close()


@router.post("/run")
def start_backtest(req: BacktestRequest):
    """Start a backtest. Returns backtest_id for WebSocket streaming."""
    backtest_id = str(uuid.uuid4())[:8]
    try:
        dates = get_trading_dates(req.start_date, req.end_date, req.interval_days)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="start_date and end_date must use YYYY-MM-DD.",
        ) from exc
    if len(dates) > MAX_BACKTEST_DATES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Backtest range produces {len(dates)} dates; "
                f"maximum is {MAX_BACKTEST_DATES}."
            ),
        )

    try:
        submit_job(
            "backtest",
            _run_backtest_thread,
            backtest_id,
            req,
            job_id=backtest_id,
            metadata={"ticker": normalize_ticker(req.ticker)},
        )
    except ExecutionRejected as exc:
        log_job_rejected("backtest", job_id=backtest_id, reason=str(exc))
        raise HTTPException(
            status_code=429,
            detail="Backtest capacity is currently full; retry later.",
            headers={"Retry-After": "30"},
        ) from exc

    return {
        "backtest_id": backtest_id,
        "status": "started",
        "ticker": normalize_ticker(req.ticker),
        "total_dates": len(dates),
        "dates": dates,
    }


@router.websocket("/ws/{backtest_id}")
async def backtest_websocket(websocket: WebSocket, backtest_id: str):
    """WebSocket for streaming backtest progress."""
    await manager.connect(websocket, backtest_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, backtest_id)


@router.get("/{backtest_id}")
def get_backtest_result(backtest_id: str):
    """Get backtest results."""
    run = get_backtest_run(backtest_id)
    if not run:
        return {"error": "Backtest not found"}

    trades = get_backtest_trades(backtest_id)
    return {**run, "trades": trades}


@router.get("/history/list")
def list_backtests(limit: int = 20):
    """List past backtest runs."""
    return get_backtest_history(limit)
