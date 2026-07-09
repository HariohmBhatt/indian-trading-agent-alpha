"""Kite Connect auth and status routes."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from urllib.parse import urlencode

from backend.brokers.kite import (
    KiteAuthExpired,
    KiteConfigError,
    clear_kite_access_token,
    exchange_request_token,
    get_kite_status,
    get_login_url,
    save_kite_credentials,
)


router = APIRouter(prefix="/api/kite", tags=["kite"])


class KiteCredentials(BaseModel):
    api_key: str
    api_secret: str


def _frontend_url(request: Request, query: dict[str, str]) -> str:
    host = request.url.hostname or "localhost"
    return f"http://{host}:3000/equity-portfolio-analysis?{urlencode(query)}"


@router.get("/status")
def status():
    return get_kite_status()


@router.put("/credentials")
def credentials(data: KiteCredentials):
    try:
        return save_kite_credentials(data.api_key, data.api_secret)
    except KiteConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/login-url")
def login_url():
    try:
        return {"login_url": get_login_url()}
    except KiteConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/callback")
def callback(request: Request, request_token: str | None = None, status: str | None = None):
    if status and status != "success":
        return RedirectResponse(_frontend_url(request, {"kite": "error", "message": status}))
    if not request_token:
        return RedirectResponse(_frontend_url(request, {"kite": "error", "message": "missing_request_token"}))
    try:
        exchange_request_token(request_token)
        return RedirectResponse(_frontend_url(request, {"kite": "connected"}))
    except (KiteConfigError, KiteAuthExpired) as exc:
        return RedirectResponse(_frontend_url(request, {"kite": "error", "message": str(exc)}))
    except Exception as exc:
        return RedirectResponse(_frontend_url(request, {"kite": "error", "message": str(exc)[:120]}))


@router.post("/logout")
def logout():
    clear_kite_access_token()
    return {"status": "logged_out", "kite": get_kite_status()}
