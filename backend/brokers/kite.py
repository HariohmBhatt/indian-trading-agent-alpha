"""Read-only Kite Connect integration for portfolio review."""

import json
import math
from datetime import date
from typing import Any

from backend.db import get_setting, set_setting


KITE_API_KEY = "kite_api_key"
KITE_API_SECRET = "kite_api_secret"
KITE_ACCESS_TOKEN = "kite_access_token"
KITE_ACCESS_TOKEN_DATE = "kite_access_token_date"
KITE_PROFILE = "kite_profile"


class KiteConfigError(RuntimeError):
    """Raised when Kite credentials or access token are not ready."""


class KiteAuthExpired(RuntimeError):
    """Raised when Kite rejects the current token."""


class KiteHoldingsError(RuntimeError):
    """Raised when Kite returns a response that cannot be trusted as holdings."""


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"


def _today() -> str:
    return date.today().isoformat()


def _load_profile() -> dict | None:
    raw = get_setting(KITE_PROFILE)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def get_kite_status() -> dict:
    api_key = get_setting(KITE_API_KEY)
    api_secret = get_setting(KITE_API_SECRET)
    token_date = get_setting(KITE_ACCESS_TOKEN_DATE)
    access_token = get_setting(KITE_ACCESS_TOKEN)
    connected_today = bool(access_token and token_date == _today())
    profile = _load_profile()

    return {
        "api_key_configured": bool(api_key),
        "api_secret_configured": bool(api_secret),
        "configured": bool(api_key and api_secret),
        "connected_today": connected_today,
        "token_date": token_date,
        "login_ready": bool(api_key and api_secret),
        "masked_api_key": mask_secret(api_key),
        "profile": profile,
    }


def save_kite_credentials(api_key: str, api_secret: str) -> dict:
    api_key = (api_key or "").strip()
    api_secret = (api_secret or "").strip()
    if not api_key or not api_secret:
        raise KiteConfigError("Both Kite API key and API secret are required")
    set_setting(KITE_API_KEY, api_key)
    set_setting(KITE_API_SECRET, api_secret)
    # A changed credential pair invalidates any old session.
    clear_kite_access_token()
    return get_kite_status()


def clear_kite_access_token():
    set_setting(KITE_ACCESS_TOKEN, None)
    set_setting(KITE_ACCESS_TOKEN_DATE, None)


def _kite_connect_cls():
    try:
        from kiteconnect import KiteConnect
        return KiteConnect
    except ImportError as exc:
        raise KiteConfigError("kiteconnect package is not installed") from exc


def _new_client():
    api_key = get_setting(KITE_API_KEY)
    if not api_key:
        raise KiteConfigError("Kite API key is not configured")
    return _kite_connect_cls()(api_key=api_key)


def get_login_url() -> str:
    if not get_setting(KITE_API_SECRET):
        raise KiteConfigError("Kite API secret is not configured")
    return _new_client().login_url()


def exchange_request_token(request_token: str) -> dict:
    request_token = (request_token or "").strip()
    api_secret = get_setting(KITE_API_SECRET)
    if not request_token:
        raise KiteConfigError("Missing Kite request token")
    if not api_secret:
        raise KiteConfigError("Kite API secret is not configured")

    client = _new_client()
    session = client.generate_session(request_token, api_secret=api_secret)
    access_token = session.get("access_token")
    if not access_token:
        raise KiteConfigError("Kite did not return an access token")

    set_setting(KITE_ACCESS_TOKEN, access_token)
    set_setting(KITE_ACCESS_TOKEN_DATE, _today())
    client.set_access_token(access_token)

    profile = {
        "user_id": session.get("user_id"),
        "user_name": session.get("user_name"),
        "user_shortname": session.get("user_shortname"),
        "broker": session.get("broker"),
        "email": mask_secret(session.get("email")),
    }
    set_setting(KITE_PROFILE, json.dumps({k: v for k, v in profile.items() if v}))
    return get_kite_status()


def get_authenticated_client():
    access_token = get_setting(KITE_ACCESS_TOKEN)
    token_date = get_setting(KITE_ACCESS_TOKEN_DATE)
    if not access_token or token_date != _today():
        clear_kite_access_token()
        raise KiteConfigError("Kite login is required for today")

    client = _new_client()
    client.set_access_token(access_token)
    return client


def _is_auth_error(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    return (
        "token" in message
        or "permission" in message
        or "auth" in name
        or "tokenexception" in name
    )


def fetch_equity_holdings() -> list[dict[str, Any]]:
    client = get_authenticated_client()
    try:
        holdings = client.holdings()
    except Exception as exc:
        if _is_auth_error(exc):
            clear_kite_access_token()
            raise KiteAuthExpired("Kite session expired. Please connect Kite again.") from exc
        raise
    return normalize_holdings(holdings)


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _holding_number(value: Any, field: str, index: int) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, bool):
        raise KiteHoldingsError(f"Kite holdings row {index} has invalid {field}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise KiteHoldingsError(f"Kite holdings row {index} has invalid {field}") from exc
    if not math.isfinite(number):
        raise KiteHoldingsError(f"Kite holdings row {index} has non-finite {field}")
    return number


def normalize_holdings(holdings: Any) -> list[dict[str, Any]]:
    """Validate and normalize one complete Kite holdings response.

    Kite's empty list is a valid, confirmed empty portfolio. Any other
    unexpected outer shape or malformed row is rejected before the local
    positions store can be changed.
    """
    if not isinstance(holdings, list):
        raise KiteHoldingsError("Kite holdings response must be a list")

    normalized = []
    seen = set()
    for index, h in enumerate(holdings):
        if not isinstance(h, dict):
            raise KiteHoldingsError(f"Kite holdings row {index} is malformed")

        raw_symbol = h.get("tradingsymbol") or h.get("ticker")
        if not isinstance(raw_symbol, str) or not raw_symbol.strip():
            raise KiteHoldingsError(f"Kite holdings row {index} has no trading symbol")
        symbol = raw_symbol.strip().upper()
        exchange = h.get("exchange") or "NSE"
        if not isinstance(exchange, str) or not exchange.strip():
            raise KiteHoldingsError(f"Kite holdings row {index} has no exchange")
        exchange = exchange.strip().upper()
        key = (symbol, exchange)
        if key in seen:
            raise KiteHoldingsError(f"Kite holdings contain duplicate row: {symbol} ({exchange})")
        seen.add(key)

        quantity = _holding_number(h.get("quantity"), "quantity", index)
        avg_price = _holding_number(h.get("average_price"), "average_price", index)
        last_price = _holding_number(h.get("last_price"), "last_price", index)
        close_price = _holding_number(h.get("close_price"), "close_price", index)
        invested_value = avg_price * quantity
        current_value = last_price * quantity
        pnl = (
            _holding_number(h.get("pnl"), "pnl", index)
            if h.get("pnl") is not None
            else current_value - invested_value
        )
        pnl_pct = (pnl / invested_value * 100) if invested_value else 0.0
        day_change = _holding_number(h.get("day_change"), "day_change", index)
        day_change_pct = _holding_number(
            h.get("day_change_percentage"), "day_change_percentage", index
        )

        normalized.append({
            "tradingsymbol": symbol,
            "exchange": exchange,
            "isin": h.get("isin"),
            "product": h.get("product"),
            "quantity": quantity,
            "t1_quantity": _holding_number(h.get("t1_quantity"), "t1_quantity", index),
            "average_price": round(avg_price, 2),
            "last_price": round(last_price, 2),
            "close_price": round(close_price, 2),
            "invested_value": round(invested_value, 2),
            "current_value": round(current_value, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "day_change": round(day_change, 2),
            "day_change_pct": round(day_change_pct, 2),
        })
    return normalized

