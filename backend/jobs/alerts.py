"""Freshness and failure alert delivery for scheduled jobs."""

from __future__ import annotations

import html
from datetime import datetime
from typing import Any


class TelegramFreshnessAlertSink:
    """Best-effort Telegram sink with a safe no-configuration path."""

    def alert(
        self,
        *,
        job_name: str,
        status: str,
        scheduled_at: datetime,
        now: datetime,
        detail: str,
    ) -> dict[str, Any]:
        from backend.notifications.telegram import (
            get_telegram_status,
            send_html_message_with_optional_buttons,
        )

        if not get_telegram_status().get("enabled"):
            return {"status": "skipped", "reason": "telegram_not_configured"}

        text = "\n".join(
            [
                "<b>Scheduled freshness alert</b>",
                f"<b>Job:</b> {html.escape(job_name)}",
                f"<b>Status:</b> {html.escape(status)}",
                f"<b>Scheduled:</b> {html.escape(scheduled_at.isoformat())}",
                f"<b>Observed:</b> {html.escape(now.isoformat())}",
                f"<b>Detail:</b> {html.escape(detail[:1000])}",
            ]
        )
        try:
            result = send_html_message_with_optional_buttons(text)
        except Exception as exc:
            return {"status": "error", "reason": str(exc)[:500]}
        return {
            "status": "sent",
            "message_id": result.get("result", {}).get("message_id"),
        }
