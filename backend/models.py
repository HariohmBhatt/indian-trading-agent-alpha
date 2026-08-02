"""Pydantic models for the API."""

from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from backend.execution import (
    MAX_ANALYSIS_ANALYSTS,
    MAX_ANALYSIS_DEBATE_ROUNDS,
    MAX_ANALYSIS_RISK_ROUNDS,
)


class AnalysisRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=32)
    trade_date: str = Field(min_length=10, max_length=10)
    analysts: list[str] = Field(
        default_factory=lambda: ["market", "social", "news", "fundamentals"],
        min_length=1,
        max_length=MAX_ANALYSIS_ANALYSTS,
    )
    max_debate_rounds: int = Field(
        default=1,
        ge=1,
        le=MAX_ANALYSIS_DEBATE_ROUNDS,
    )
    max_risk_discuss_rounds: int = Field(
        default=1,
        ge=1,
        le=MAX_ANALYSIS_RISK_ROUNDS,
    )
    output_language: str = Field(default="English", max_length=32)


class AnalysisResponse(BaseModel):
    task_id: str
    status: str
    ticker: str
    trade_date: str


class AnalysisResult(BaseModel):
    task_id: str
    ticker: str
    trade_date: str
    signal: str
    market_report: Optional[str] = None
    sentiment_report: Optional[str] = None
    news_report: Optional[str] = None
    fundamentals_report: Optional[str] = None
    investment_plan: Optional[str] = None
    trader_investment_plan: Optional[str] = None
    final_trade_decision: Optional[str] = None
    bull_history: Optional[str] = None
    bear_history: Optional[str] = None
    risk_aggressive_history: Optional[str] = None
    risk_conservative_history: Optional[str] = None
    risk_neutral_history: Optional[str] = None
    stats: Optional[dict] = None
    created_at: Optional[str] = None
    duration_seconds: Optional[float] = None


class WatchlistItem(BaseModel):
    ticker: str
    exchange: str = "NSE"
    name: Optional[str] = None
    added_at: Optional[str] = None


class QuoteResponse(BaseModel):
    ticker: str
    name: str
    price: float
    change: float
    change_percent: float
    volume: int
    high: float
    low: float
    open: float
    prev_close: float
    market_cap: Optional[float] = None
    pe_ratio: Optional[float] = None
    fifty_two_week_high: Optional[float] = None
    fifty_two_week_low: Optional[float] = None


class ChartDataPoint(BaseModel):
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: int


class ConfigUpdate(BaseModel):
    llm_provider: Optional[str] = None
    deep_think_llm: Optional[str] = None
    quick_think_llm: Optional[str] = None
    trading_style: Optional[str] = None
    max_debate_rounds: Optional[int] = None
    max_risk_discuss_rounds: Optional[int] = None
    dry_run: Optional[bool] = None
    max_position_value: Optional[float] = None
    max_loss_per_trade: Optional[float] = None
    max_daily_loss: Optional[float] = None
