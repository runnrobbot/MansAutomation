"""Telegram bot notification channel."""

from __future__ import annotations

import aiohttp

from mansautomation.core.config import TelegramSettings
from mansautomation.notifications.base import Notification, NotificationChannel

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotificationChannel(NotificationChannel):
    name = "telegram"

    def __init__(self, settings: TelegramSettings, session: aiohttp.ClientSession) -> None:
        self._settings = settings
        self._session = session

    @property
    def enabled(self) -> bool:
        return bool(
            self._settings.enabled and self._settings.bot_token and self._settings.chat_id
        )

    async def send(self, notification: Notification) -> None:
        if not self.enabled:
            return
        token = self._settings.bot_token.get_secret_value() if self._settings.bot_token else ""
        url = _API_URL.format(token=token)
        text = f"<b>{_escape_html(notification.title)}</b>\n{_escape_html(notification.message)}"
        payload = {
            "chat_id": self._settings.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            async with self._session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
        except aiohttp.ClientError:
            # Notification failures are surfaced via logging by the dispatcher
            return


def _escape_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
