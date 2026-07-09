"""Equity portfolio review routes backed by Kite holdings."""

from fastapi import APIRouter, HTTPException, Query

from backend.brokers.kite import KiteAuthExpired, KiteConfigError, fetch_equity_holdings
from backend.db import (
    get_equity_portfolio_review,
    get_latest_equity_portfolio_review,
    list_equity_portfolio_reviews,
)
from backend.equity_portfolio import create_and_save_review


router = APIRouter(prefix="/api/equity-portfolio", tags=["equity-portfolio"])


def _kite_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KiteAuthExpired):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, KiteConfigError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


@router.get("/holdings")
def holdings():
    try:
        data = fetch_equity_holdings()
        return {"holdings": data, "count": len(data)}
    except Exception as exc:
        raise _kite_error(exc)


@router.post("/reviews")
def create_review():
    try:
        holdings_data = fetch_equity_holdings()
        return create_and_save_review(holdings_data, enrich=True)
    except Exception as exc:
        raise _kite_error(exc)


@router.get("/reviews/latest")
def latest_review():
    review = get_latest_equity_portfolio_review()
    return {"review": review, "found": bool(review)}


@router.get("/reviews")
def review_history(limit: int = Query(30, ge=1, le=100)):
    reviews = list_equity_portfolio_reviews(limit=limit)
    return {"reviews": reviews, "count": len(reviews)}


@router.get("/reviews/{review_id}")
def review_detail(review_id: str):
    review = get_equity_portfolio_review(review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    return review

