"""Discord webhook notification channel."""

from __future__ import annotations

import aiohttp

from mansautomation.core.config import DiscordSettings
from mansautomation.notifications.base import Notification, NotificationChannel, NotificationLevel

_LEVEL_COLORS: dict[NotificationLevel, int] = {
    NotificationLevel.INFO: 0x3498DB,
    NotificationLevel.SUCCESS: 0x2ECC71,
    NotificationLevel.WARNING: 0xF1C40F,
    NotificationLevel.ERROR: 0xE74C3C,
}


class DiscordNotificationChannel(NotificationChannel):
    name = "discord"

    def __init__(self, settings: DiscordSettings, session: aiohttp.ClientSession) -> None:
        self._settings = settings
        self._session = session

    @property
    def enabled(self) -> bool:
        return bool(self._settings.enabled and self._settings.webhook_url)

    async def send(self, notification: Notification) -> None:
        if not self.enabled:
            return
        url = self._settings.webhook_url.get_secret_value() if self._settings.webhook_url else ""
        payload = {
            "username": "MansAutomation",
            "embeds": [
                {
                    "title": notification.title,
                    "description": notification.message,
                    "color": _LEVEL_COLORS.get(notification.level, _LEVEL_COLORS[NotificationLevel.INFO]),
                }
            ],
        }
        try:
            async with self._session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
        except aiohttp.ClientError:
            return
