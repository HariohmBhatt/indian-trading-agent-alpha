"""Local positions store routes — read local, sync from Kite on demand."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.brokers.kite import KiteAuthExpired, KiteConfigError
from backend.positions import (
    PositionsError,
    get_positions_view,
    remove_position,
    save_manual_position,
    sync_positions_from_kite,
    update_position_fields,
)

router = APIRouter(prefix="/api/positions", tags=["positions"])


class ManualPositionRequest(BaseModel):
    tradingsymbol: str
    exchange: str = "NSE"
    quantity: float
    average_price: float
    last_price: float | None = None
    product: str | None = None
    isin: str | None = None
    notes: str | None = None


class PositionUpdateRequest(BaseModel):
    quantity: float | None = None
    average_price: float | None = None
    last_price: float | None = None
    notes: str | None = None


def _kite_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KiteAuthExpired):
        return HTTPException(status_code=401, detail=str(exc))
    if isinstance(exc, KiteConfigError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


@router.get("")
def list_all():
    """All locally stored positions + portfolio summary + last sync time."""
    return get_positions_view()


@router.post("/sync")
def sync():
    """Explicitly pull live holdings from Kite into the local store."""
    try:
        return sync_positions_from_kite()
    except Exception as exc:
        raise _kite_error(exc)


@router.post("")
def add_manual(req: ManualPositionRequest):
    """Add or replace a manually maintained position."""
    try:
        row = save_manual_position(req.model_dump())
        return {"status": "saved", "position": row}
    except PositionsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/{exchange}/{symbol}")
def update(exchange: str, symbol: str, req: PositionUpdateRequest):
    """Edit quantity / average price / last price / notes of a stored position."""
    try:
        row = update_position_fields(symbol, exchange, req.model_dump(exclude_unset=True))
        return {"status": "updated", "position": row}
    except PositionsError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/{exchange}/{symbol}")
def delete(exchange: str, symbol: str):
    if not remove_position(symbol, exchange):
        raise HTTPException(status_code=404, detail=f"Position {symbol} ({exchange}) not found")
    return {"status": "deleted", "tradingsymbol": symbol.upper(), "exchange": exchange.upper()}
