"""Fast read-only Kite equity portfolio review."""

import uuid
from collections import defaultdict
from datetime import datetime, date

from backend.db import save_equity_portfolio_review


ACTION_HOLD = "HOLD"
ACTION_WATCH = "WATCH"
ACTION_REVIEW = "REVIEW"
ACTION_TRIM = "TRIM_CONSIDER"
ACTION_EXIT = "EXIT_REVIEW"


def _safe_pct(numerator: float, denominator: float) -> float:
    return (numerator / denominator * 100) if denominator else 0.0


def _sector_for(ticker: str) -> str:
    try:
        from backend.concentration import get_sector_for_ticker
        return get_sector_for_ticker(ticker)
    except Exception:
        return "Other"


def _recommendation_for(ticker: str) -> dict:
    try:
        from backend.recommender import _analyze_stock
        result = _analyze_stock(ticker)
        if not result:
            return {"available": False, "reason": "No market signal available"}
        return {
            "available": True,
            "direction": result.get("direction"),
            "score": result.get("score"),
            "confidence": result.get("confidence"),
            "success_probability": result.get("success_probability"),
            "signals": result.get("signals", [])[:5],
        }
    except Exception as exc:
        return {"available": False, "reason": str(exc)[:160]}


def _choose_action(holding: dict, recommendation: dict) -> tuple[str, list[str]]:
    reasons = []
    pnl_pct = float(holding.get("pnl_pct") or 0)
    allocation_pct = float(holding.get("allocation_pct") or 0)
    quantity = float(holding.get("quantity") or 0)
    direction = (recommendation.get("direction") or "").upper() if recommendation.get("available") else ""
    rec_score = float(recommendation.get("score") or 0)

    if quantity <= 0:
        return ACTION_WATCH, ["Zero quantity row; keep only as a watch item."]

    if direction == "STRONG SELL" and pnl_pct <= -8:
        reasons.append("Market signal is strongly bearish while position is in drawdown.")
        return ACTION_EXIT, reasons

    if direction in {"SELL", "STRONG SELL"}:
        reasons.append(f"Current market signal is {direction}.")
        return ACTION_REVIEW, reasons

    if pnl_pct <= -15:
        reasons.append(f"Drawdown is {pnl_pct:.1f}%.")
        return ACTION_REVIEW, reasons

    if allocation_pct >= 30:
        reasons.append(f"Single holding is {allocation_pct:.1f}% of portfolio.")
        return ACTION_TRIM, reasons

    if allocation_pct >= 20 and pnl_pct >= 20:
        reasons.append("Large winner with meaningful portfolio weight.")
        return ACTION_TRIM, reasons

    if direction in {"BUY", "STRONG BUY"} and rec_score >= 2:
        reasons.append(f"Market signal supports holding ({direction}).")
        return ACTION_HOLD, reasons

    if abs(pnl_pct) < 3 and not direction:
        reasons.append("Position is near cost and lacks a fresh market signal.")
        return ACTION_WATCH, reasons

    reasons.append("No urgent risk flag detected.")
    return ACTION_HOLD, reasons


def build_equity_portfolio_review(holdings: list[dict], enrich: bool = True, max_enriched: int = 25) -> dict:
    """Build a persisted review payload from normalized Kite holdings."""
    holdings = list(holdings or [])
    total_invested = round(sum(float(h.get("invested_value") or 0) for h in holdings), 2)
    total_current = round(sum(float(h.get("current_value") or 0) for h in holdings), 2)
    total_pnl = round(sum(float(h.get("pnl") or 0) for h in holdings), 2)
    total_day_pnl = round(sum(float(h.get("day_change") or 0) * float(h.get("quantity") or 0) for h in holdings), 2)
    total_pnl_pct = round(_safe_pct(total_pnl, total_invested), 2)
    day_pnl_pct = round(_safe_pct(total_day_pnl, total_current - total_day_pnl), 2)

    by_sector: dict[str, dict] = defaultdict(lambda: {"value": 0.0, "count": 0, "holdings": []})
    enriched = []

    sorted_for_enrichment = sorted(holdings, key=lambda h: abs(float(h.get("current_value") or 0)), reverse=True)
    enrich_symbols = {h.get("tradingsymbol") for h in sorted_for_enrichment[:max_enriched]} if enrich else set()

    for holding in holdings:
        ticker = (holding.get("tradingsymbol") or "").upper()
        current_value = float(holding.get("current_value") or 0)
        allocation_pct = round(_safe_pct(current_value, total_current), 2)
        sector = _sector_for(ticker)

        recommendation = (
            _recommendation_for(ticker)
            if enrich and ticker in enrich_symbols and current_value > 0
            else {"available": False, "reason": "Enrichment skipped for fast review"}
        )

        row = {
            **holding,
            "tradingsymbol": ticker,
            "sector": sector,
            "allocation_pct": allocation_pct,
            "recommendation": recommendation,
        }
        action, reasons = _choose_action(row, recommendation)
        row["action"] = action
        row["reasons"] = reasons
        enriched.append(row)

        by_sector[sector]["value"] += current_value
        by_sector[sector]["count"] += 1
        by_sector[sector]["holdings"].append(ticker)

    sector_allocation = []
    for sector, data in by_sector.items():
        sector_allocation.append({
            "sector": sector,
            "value": round(data["value"], 2),
            "allocation_pct": round(_safe_pct(data["value"], total_current), 2),
            "count": data["count"],
            "holdings": sorted(data["holdings"]),
        })
    sector_allocation.sort(key=lambda x: -x["allocation_pct"])

    top_winners = sorted(enriched, key=lambda h: float(h.get("pnl_pct") or 0), reverse=True)[:5]
    top_losers = sorted(enriched, key=lambda h: float(h.get("pnl_pct") or 0))[:5]
    high_risk = [h for h in enriched if h["action"] in {ACTION_REVIEW, ACTION_TRIM, ACTION_EXIT}]
    concentration_warnings = []
    for h in enriched:
        if float(h.get("allocation_pct") or 0) >= 20:
            concentration_warnings.append(
                f"{h['tradingsymbol']} is {h['allocation_pct']:.1f}% of portfolio."
            )
    for s in sector_allocation:
        if s["allocation_pct"] >= 35:
            concentration_warnings.append(
                f"{s['sector']} sector is {s['allocation_pct']:.1f}% of portfolio."
            )

    insights = {
        "portfolio_status": "EMPTY" if not enriched else ("REVIEW_NEEDED" if high_risk else "STABLE"),
        "plain_summary": _plain_summary(total_current, total_pnl, total_pnl_pct, len(high_risk)),
        "action_counts": {
            ACTION_HOLD: sum(1 for h in enriched if h["action"] == ACTION_HOLD),
            ACTION_WATCH: sum(1 for h in enriched if h["action"] == ACTION_WATCH),
            ACTION_REVIEW: sum(1 for h in enriched if h["action"] == ACTION_REVIEW),
            ACTION_TRIM: sum(1 for h in enriched if h["action"] == ACTION_TRIM),
            ACTION_EXIT: sum(1 for h in enriched if h["action"] == ACTION_EXIT),
        },
        "high_risk_holdings": [
            {
                "tradingsymbol": h["tradingsymbol"],
                "action": h["action"],
                "pnl_pct": h["pnl_pct"],
                "allocation_pct": h["allocation_pct"],
                "reasons": h["reasons"],
            }
            for h in high_risk
        ],
        "concentration_warnings": concentration_warnings,
    }

    review = {
        "review_id": str(uuid.uuid4())[:12],
        "review_date": date.today().isoformat(),
        "holdings": enriched,
        "summary": {
            "total_holdings": len(enriched),
            "total_invested": total_invested,
            "total_current": total_current,
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl_pct,
            "total_day_pnl": total_day_pnl,
            "day_pnl_pct": day_pnl_pct,
            "sector_allocation": sector_allocation,
            "top_winners": _compact_holdings(top_winners),
            "top_losers": _compact_holdings(top_losers),
        },
        "insights": insights,
        "model_metadata": {
            "mode": "fast_summary",
            "engine": "kite_holdings_plus_local_recommender",
            "expensive_deep_analysis": False,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        },
    }
    return review


def _compact_holdings(holdings: list[dict]) -> list[dict]:
    return [
        {
            "tradingsymbol": h.get("tradingsymbol"),
            "pnl": h.get("pnl"),
            "pnl_pct": h.get("pnl_pct"),
            "allocation_pct": h.get("allocation_pct"),
            "action": h.get("action"),
        }
        for h in holdings
    ]


def _plain_summary(total_current: float, total_pnl: float, total_pnl_pct: float, risk_count: int) -> str:
    if total_current <= 0:
        return "No equity holdings found in Kite."
    direction = "up" if total_pnl >= 0 else "down"
    risk_text = "No urgent position-level review flags." if risk_count == 0 else f"{risk_count} holding(s) need review."
    return f"Portfolio is {direction} {abs(total_pnl_pct):.2f}% overall. {risk_text}"


def create_and_save_review(holdings: list[dict], enrich: bool = True) -> dict:
    review = build_equity_portfolio_review(holdings, enrich=enrich)
    save_equity_portfolio_review(review)
    return review
