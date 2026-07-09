"""Telegram Bot API notification helpers."""

import json
import html
import urllib.parse
import urllib.request
import urllib.error
from datetime import date

from backend.db import get_setting, set_setting


TELEGRAM_BOT_TOKEN = "telegram_bot_token"
TELEGRAM_CHAT_ID = "telegram_chat_id"
TELEGRAM_ENABLED = "telegram_enabled"
TELEGRAM_APP_URL = "telegram_app_url"
DEFAULT_APP_URL = "http://100.91.136.0:3000/equity-portfolio-analysis"


class TelegramConfigError(RuntimeError):
    """Raised when Telegram notification settings are incomplete."""


class TelegramSendError(RuntimeError):
    """Raised when Telegram rejects a send request."""


def mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 10:
        return "****"
    return f"{value[:6]}...{value[-4:]}"


def get_telegram_status() -> dict:
    token = get_setting(TELEGRAM_BOT_TOKEN)
    chat_id = get_setting(TELEGRAM_CHAT_ID)
    enabled = get_setting(TELEGRAM_ENABLED)
    return {
        "configured": bool(token and chat_id),
        "enabled": enabled != "false" and bool(token and chat_id),
        "masked_bot_token": mask_secret(token),
        "masked_chat_id": mask_secret(chat_id),
    }


def save_telegram_settings(bot_token: str, chat_id: str, enabled: bool = True) -> dict:
    bot_token = (bot_token or "").strip()
    chat_id = (chat_id or "").strip()
    if not bot_token or not chat_id:
        raise TelegramConfigError("Telegram bot token and chat ID are required")
    set_setting(TELEGRAM_BOT_TOKEN, bot_token)
    set_setting(TELEGRAM_CHAT_ID, chat_id)
    set_setting(TELEGRAM_ENABLED, "true" if enabled else "false")
    return get_telegram_status()


def clear_telegram_settings() -> dict:
    set_setting(TELEGRAM_BOT_TOKEN, None)
    set_setting(TELEGRAM_CHAT_ID, None)
    set_setting(TELEGRAM_ENABLED, None)
    return get_telegram_status()


def _settings() -> tuple[str, str]:
    token = get_setting(TELEGRAM_BOT_TOKEN)
    chat_id = get_setting(TELEGRAM_CHAT_ID)
    enabled = get_setting(TELEGRAM_ENABLED)
    if enabled == "false":
        raise TelegramConfigError("Telegram notifications are disabled")
    if not token or not chat_id:
        raise TelegramConfigError("Telegram bot token or chat ID is not configured")
    return token, chat_id


def get_app_url() -> str:
    return get_setting(TELEGRAM_APP_URL) or DEFAULT_APP_URL


def portfolio_keyboard(app_url: str | None = None) -> dict:
    url = app_url or get_app_url()
    return {
        "inline_keyboard": [
            [{"text": "Open portfolio page", "url": url}],
            [{"text": "Connect Kite for today", "url": url}],
        ]
    }


def send_message(text: str, parse_mode: str | None = None, reply_markup: dict | None = None) -> dict:
    token, chat_id = _settings()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": text[:4096],
        "disable_web_page_preview": "true",
    }
    if parse_mode:
        data["parse_mode"] = parse_mode
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)
    payload = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise TelegramSendError(body or str(exc)) from exc
    except Exception as exc:
        raise TelegramSendError(str(exc)) from exc

    if not body.get("ok"):
        description = body.get("description") or "Telegram send failed"
        raise TelegramSendError(description)
    return body


def send_html_message(text: str, reply_markup: dict | None = None) -> dict:
    return send_message(text, parse_mode="HTML", reply_markup=reply_markup)


def send_html_message_with_optional_buttons(text: str, reply_markup: dict | None = None) -> dict:
    try:
        return send_html_message(text, reply_markup=reply_markup)
    except TelegramSendError as exc:
        if reply_markup and _looks_like_button_url_error(str(exc)):
            return send_html_message(text, reply_markup=None)
        raise


def _looks_like_button_url_error(message: str) -> bool:
    lowered = message.lower()
    return "button_url_invalid" in lowered or "wrong http url" in lowered or "url host is empty" in lowered


def build_kite_login_reminder(app_url: str | None = None) -> str:
    url = app_url or get_app_url()
    return "\n".join([
        "<b>Kite login required</b>",
        "",
        "Your daily Kite session is not active yet.",
        "Open the portfolio page, complete Kite login, then the scheduled review can fetch holdings.",
        "",
        f'<a href="{html.escape(url, quote=True)}">Open Equity Portfolio Analysis</a>',
    ])


def build_portfolio_review_message(review: dict, app_url: str | None = None) -> str:
    summary = review.get("summary") or {}
    insights = review.get("insights") or {}
    review_date = review.get("review_date") or date.today().isoformat()
    app_url = app_url or get_app_url()
    lines = [
        f"<b>Equity Portfolio Review</b> - {html.escape(str(review_date))}",
        "",
        f"<b>Status:</b> {html.escape(str(insights.get('portfolio_status') or 'NO_STATUS'))}",
        f"<b>Value:</b> Rs.{_fmt_money(summary.get('total_current'))}",
        f"<b>Invested:</b> Rs.{_fmt_money(summary.get('total_invested'))}",
        f"<b>Unrealized P&amp;L:</b> Rs.{_fmt_money(summary.get('total_pnl'))} ({_fmt_pct(summary.get('total_pnl_pct'))})",
        f"<b>Day P&amp;L:</b> Rs.{_fmt_money(summary.get('total_day_pnl'))} ({_fmt_pct(summary.get('day_pnl_pct'))})",
        "",
        html.escape(str(insights.get("plain_summary") or "No summary available.")),
    ]

    high_risk = insights.get("high_risk_holdings") or []
    if high_risk:
        lines.extend(["", "<b>Review flags</b>"])
        for item in high_risk[:8]:
            symbol = html.escape(str(item.get("tradingsymbol") or "-"))
            action = html.escape(str(item.get("action") or "-"))
            reasons = html.escape(" ".join(item.get("reasons") or []))
            lines.append(
                f"- <b>{symbol}</b>: {action} "
                f"({_fmt_pct(item.get('pnl_pct'))}, {item.get('allocation_pct', 0):.1f}% allocation)"
            )
            if reasons:
                lines.append(f"  {reasons[:160]}")
    else:
        lines.extend(["", "<b>Review flags:</b> none"])

    warnings = insights.get("concentration_warnings") or []
    if warnings:
        lines.extend(["", "<b>Concentration warnings</b>"])
        for warning in warnings[:5]:
            lines.append(f"- {html.escape(str(warning))}")

    if app_url:
        lines.extend(["", f'<a href="{html.escape(app_url, quote=True)}">Open Equity Portfolio Analysis</a>'])

    return "\n".join(lines)


def _fmt_money(value) -> str:
    try:
        return f"{float(value or 0):,.0f}"
    except (TypeError, ValueError):
        return "0"


def _fmt_pct(value) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0.0
    sign = "+" if number >= 0 else ""
    return f"{sign}{number:.2f}%"
