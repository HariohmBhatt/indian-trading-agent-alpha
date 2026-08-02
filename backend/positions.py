"""Local positions store — source of truth for the portfolio view.

Positions live in the `positions` table and are updated on demand:
- "Sync from Kite" pulls live holdings and upserts them (source='kite')
- Manual add/edit/delete for positions tracked outside Kite (source='manual')

Nothing here talks to Kite implicitly; syncing is always an explicit action.
"""

import os
from datetime import datetime, timezone
from typing import Any

from backend.db import (
    delete_position,
    get_setting,
    list_positions,
    replace_kite_positions,
    set_setting,
    upsert_position,
)

POSITIONS_LAST_SYNC = "positions_last_sync_at"
POSITIONS_SYNC_STATUS = "positions_sync_status"
POSITIONS_SYNC_ERROR = "positions_sync_error"
POSITIONS_LAST_SUCCESS = "positions_last_success_at"
POSITIONS_MAX_AGE_SECONDS = int(os.getenv("POSITIONS_MAX_AGE_SECONDS", str(24 * 60 * 60)))

SYNC_STATUS_NEVER = "never"
SYNC_STATUS_SUCCESS = "success"
SYNC_STATUS_EMPTY = "empty"
SYNC_STATUS_FAILED = "failed"
_SUCCESSFUL_SYNC_STATUSES = {SYNC_STATUS_SUCCESS, SYNC_STATUS_EMPTY}


class PositionsError(RuntimeError):
    """Raised for invalid manual position input."""


class PositionsFreshnessError(RuntimeError):
    """Raised when Kite-backed portfolio data is not safe to use."""


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _derive(quantity: float, average_price: float, last_price: float) -> dict:
    invested = quantity * average_price
    current = quantity * last_price
    pnl = current - invested
    pnl_pct = (pnl / invested * 100) if invested else 0.0
    return {
        "invested_value": round(invested, 2),
        "current_value": round(current, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sync_age_seconds(last_success_at: str | None) -> float | None:
    timestamp = _timestamp(last_success_at)
    if timestamp is None:
        return None
    return round(max(0.0, (_now() - timestamp).total_seconds()), 2)


def get_positions_sync_state() -> dict:
    """Return persisted Kite sync state plus derived freshness information."""
    status = get_setting(POSITIONS_SYNC_STATUS) or SYNC_STATUS_NEVER
    error = get_setting(POSITIONS_SYNC_ERROR)
    last_success_at = get_setting(POSITIONS_LAST_SUCCESS) or get_setting(POSITIONS_LAST_SYNC)
    age_seconds = _sync_age_seconds(last_success_at)
    is_fresh = (
        status in _SUCCESSFUL_SYNC_STATUSES
        and age_seconds is not None
        and age_seconds <= POSITIONS_MAX_AGE_SECONDS
    )
    return {
        "status": status,
        "sync_status": status,
        "error": error,
        "sync_error": error,
        "last_success_at": last_success_at,
        "age_seconds": age_seconds,
        "sync_age_seconds": age_seconds,
        "max_age_seconds": POSITIONS_MAX_AGE_SECONDS,
        "is_fresh": is_fresh,
        "is_stale": not is_fresh,
        "confirmed_empty": status == SYNC_STATUS_EMPTY,
    }


def get_last_sync() -> str | None:
    return get_positions_sync_state()["last_success_at"]


def positions_are_manual_only(positions: list[dict]) -> bool:
    """Whether a non-empty position set was explicitly entered by the user."""
    return bool(positions) and all(p.get("source") == "manual" for p in positions)


def _ensure_fresh_kite_state() -> dict:
    state = get_positions_sync_state()
    if state["is_fresh"]:
        return state

    if state["status"] == SYNC_STATUS_FAILED:
        detail = state["error"] or "The latest Kite sync failed."
    elif state["status"] == SYNC_STATUS_NEVER:
        detail = "Kite holdings have not been synced successfully yet."
    elif state["last_success_at"] is None:
        detail = "Kite holdings do not have a successful sync timestamp."
    else:
        age = state["age_seconds"]
        detail = f"Kite holdings are stale ({age:.0f}s old)." if age is not None else "Kite holdings are stale."
    raise PositionsFreshnessError(
        f"Portfolio review requires a fresh Kite holdings sync. {detail}"
    )


def get_positions_for_review() -> list[dict]:
    """Return reviewable positions, never silently falling back to stale Kite data.

    Manual-only positions are an explicit local workflow and do not require a
    Kite session. Kite-backed positions must come from a fresh successful sync;
    a confirmed empty response is also reviewable as an empty portfolio.
    """
    view = get_positions_view()
    positions = view["positions"]

    if positions_are_manual_only(positions):
        return positions

    from backend.brokers.kite import get_kite_status

    if get_kite_status().get("connected_today"):
        sync_positions_from_kite()
        view = get_positions_view()
        positions = view["positions"]
        if positions_are_manual_only(positions):
            return positions

    state = get_positions_sync_state()
    if not positions:
        if state["confirmed_empty"] and state["is_fresh"]:
            return positions
        _ensure_fresh_kite_state()
        raise PositionsFreshnessError("No positions were stored after the successful Kite sync.")

    _ensure_fresh_kite_state()
    return positions


def get_positions_view() -> dict:
    """Positions plus portfolio summary, read entirely from the local store."""
    positions = list_positions()
    sync_state = get_positions_sync_state()
    manual_only = positions_are_manual_only(positions)
    kite_backed = any(p.get("source") == "kite" for p in positions)
    source_mode = (
        "manual"
        if manual_only
        else "kite"
        if kite_backed and not any(p.get("source") == "manual" for p in positions)
        else "mixed"
        if positions
        else "empty"
    )
    can_review = manual_only or (
        sync_state["is_fresh"] if positions else sync_state["confirmed_empty"] and sync_state["is_fresh"]
    )
    total_invested = round(sum(_num(p.get("invested_value")) for p in positions), 2)
    total_current = round(sum(_num(p.get("current_value")) for p in positions), 2)
    total_pnl = round(sum(_num(p.get("pnl")) for p in positions), 2)
    total_day_pnl = round(
        sum(_num(p.get("day_change")) * _num(p.get("quantity")) for p in positions), 2
    )

    enriched = []
    for p in positions:
        current = _num(p.get("current_value"))
        enriched.append({
            **p,
            "allocation_pct": round(current / total_current * 100, 2) if total_current else 0.0,
        })

    return {
        "positions": enriched,
        "count": len(enriched),
        "last_sync": sync_state["last_success_at"],
        "sync": sync_state,
        "sync_status": sync_state["status"],
        "sync_error": sync_state["error"],
        "last_success_at": sync_state["last_success_at"],
        "age_seconds": sync_state["age_seconds"],
        "sync_age_seconds": sync_state["age_seconds"],
        "sync_state": sync_state,
        "is_stale": sync_state["is_stale"],
        "manual_only": manual_only,
        "kite_backed": kite_backed,
        "source_mode": source_mode,
        "can_review": can_review,
        "summary": {
            "total_positions": len(enriched),
            "total_invested": total_invested,
            "total_current": total_current,
            "total_pnl": total_pnl,
            "total_pnl_pct": round(total_pnl / total_invested * 100, 2) if total_invested else 0.0,
            "total_day_pnl": total_day_pnl,
            "day_pnl_pct": (
                round(total_day_pnl / (total_current - total_day_pnl) * 100, 2)
                if (total_current - total_day_pnl) else 0.0
            ),
            "manual_count": sum(1 for p in positions if p.get("source") == "manual"),
            "kite_count": sum(1 for p in positions if p.get("source") == "kite"),
        },
    }


def sync_positions_from_kite() -> dict:
    """Explicit sync: fetch live Kite holdings into the local store."""
    from backend.brokers.kite import fetch_equity_holdings

    try:
        holdings = fetch_equity_holdings()
        counts = replace_kite_positions(holdings)
    except Exception as exc:
        set_setting(POSITIONS_SYNC_STATUS, SYNC_STATUS_FAILED)
        set_setting(POSITIONS_SYNC_ERROR, str(exc)[:500] or type(exc).__name__)
        raise

    synced_at = _now().isoformat(timespec="seconds")
    status = SYNC_STATUS_EMPTY if not holdings else SYNC_STATUS_SUCCESS
    set_setting(POSITIONS_SYNC_STATUS, status)
    set_setting(POSITIONS_SYNC_ERROR, None)
    set_setting(POSITIONS_LAST_SUCCESS, synced_at)
    set_setting(POSITIONS_LAST_SYNC, synced_at)
    return {
        "status": "synced",
        "sync_status": status,
        "synced_at": synced_at,
        "last_success_at": synced_at,
        "sync_error": None,
        **counts,
        "sync": get_positions_sync_state(),
        "summary": get_positions_view()["summary"],
    }


def save_manual_position(data: dict) -> dict:
    """Add or fully replace a manually maintained position."""
    symbol = (data.get("tradingsymbol") or "").strip().upper()
    if not symbol:
        raise PositionsError("Symbol is required")

    quantity = _num(data.get("quantity"))
    average_price = _num(data.get("average_price"))
    if quantity <= 0:
        raise PositionsError("Quantity must be greater than zero")
    if average_price <= 0:
        raise PositionsError("Average price must be greater than zero")

    last_price = _num(data.get("last_price")) or average_price
    row = {
        "tradingsymbol": symbol,
        "exchange": (data.get("exchange") or "NSE").strip().upper(),
        "isin": data.get("isin"),
        "product": data.get("product") or "CNC",
        "quantity": quantity,
        "t1_quantity": 0,
        "average_price": round(average_price, 2),
        "last_price": round(last_price, 2),
        "close_price": round(last_price, 2),
        "day_change": 0.0,
        "day_change_pct": 0.0,
        "source": "manual",
        "notes": (data.get("notes") or "").strip() or None,
        **_derive(quantity, average_price, last_price),
    }
    upsert_position(row)
    return row


def update_position_fields(tradingsymbol: str, exchange: str, data: dict) -> dict:
    """Edit an existing position (quantity, average price, last price, notes)."""
    from backend.db import get_position

    existing = get_position(tradingsymbol, exchange)
    if not existing:
        raise PositionsError(f"Position {tradingsymbol} ({exchange}) not found")

    quantity = _num(data.get("quantity", existing["quantity"]))
    average_price = _num(data.get("average_price", existing["average_price"]))
    if quantity <= 0:
        raise PositionsError("Quantity must be greater than zero")
    if average_price <= 0:
        raise PositionsError("Average price must be greater than zero")

    last_price = _num(data.get("last_price")) or _num(existing.get("last_price")) or average_price
    merged = {
        **existing,
        "quantity": quantity,
        "average_price": round(average_price, 2),
        "last_price": round(last_price, 2),
        "notes": (data.get("notes") if data.get("notes") is not None else existing.get("notes")),
        **_derive(quantity, average_price, last_price),
    }
    upsert_position(merged)
    return merged


def remove_position(tradingsymbol: str, exchange: str = "NSE") -> bool:
    return delete_position(tradingsymbol, exchange)
