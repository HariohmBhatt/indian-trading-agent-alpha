"""Telegram notification settings and test routes."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.notifications.telegram import (
    TelegramConfigError,
    TelegramSendError,
    build_kite_login_reminder,
    clear_telegram_settings,
    get_telegram_status,
    portfolio_keyboard,
    save_telegram_settings,
    send_message,
    send_html_message_with_optional_buttons,
)


router = APIRouter(prefix="/api/telegram", tags=["telegram"])


class TelegramSettings(BaseModel):
    bot_token: str
    chat_id: str
    enabled: bool = True


class TelegramTestMessage(BaseModel):
    text: str | None = None


def _telegram_error(exc: Exception) -> HTTPException:
    if isinstance(exc, TelegramConfigError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, TelegramSendError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.get("/status")
def status():
    return get_telegram_status()


@router.put("/settings")
def settings(data: TelegramSettings):
    try:
        return save_telegram_settings(data.bot_token, data.chat_id, enabled=data.enabled)
    except Exception as exc:
        raise _telegram_error(exc)


@router.delete("/settings")
def delete_settings():
    return clear_telegram_settings()


@router.post("/test")
def test_message(data: TelegramTestMessage | None = None):
    text = data.text if data and data.text else "Trading Agent Telegram notifications are connected."
    try:
        result = send_message(text)
        return {"status": "sent", "message_id": result.get("result", {}).get("message_id")}
    except Exception as exc:
        raise _telegram_error(exc)


@router.post("/kite-login-reminder")
def kite_login_reminder():
    try:
        text = build_kite_login_reminder()
        result = send_html_message_with_optional_buttons(text, reply_markup=portfolio_keyboard())
        return {"status": "sent", "message_id": result.get("result", {}).get("message_id")}
    except Exception as exc:
        raise _telegram_error(exc)
