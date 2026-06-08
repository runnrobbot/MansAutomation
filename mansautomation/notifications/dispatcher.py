"""Aggregating notification dispatcher."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

import aiohttp

from mansautomation.core.config import NotificationSettings
from mansautomation.core.events import EventBus
from mansautomation.notifications.base import Notification, NotificationChannel
from mansautomation.notifications.desktop import DesktopNotificationChannel
from mansautomation.notifications.discord import DiscordNotificationChannel
from mansautomation.notifications.telegram import TelegramNotificationChannel
from mansautomation.services.logging_service import LoggingService

NOTIFICATION_TOPIC = "notification"


class NotificationDispatcher:
    """Fans out notifications to all configured channels concurrently."""

    def __init__(
        self,
        settings: NotificationSettings,
        logging_service: LoggingService,
        event_bus: EventBus,
    ) -> None:
        self._settings = settings
        self._logger = logging_service.get_logger("notifications")
        self._event_bus = event_bus
        self._http_session: aiohttp.ClientSession | None = None
        self._channels: list[NotificationChannel] = []

    async def start(self) -> None:
        self._http_session = aiohttp.ClientSession()
        self._channels = self._build_channels(self._http_session)
        self._logger.info(
            "notification_channels_ready",
            channels=[c.name for c in self._channels if c.enabled],
        )

    async def stop(self) -> None:
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None
        self._channels = []

    def _build_channels(self, session: aiohttp.ClientSession) -> list[NotificationChannel]:
        return [
            DesktopNotificationChannel(self._settings.desktop),
            TelegramNotificationChannel(self._settings.telegram, session),
            DiscordNotificationChannel(self._settings.discord, session),
        ]

    @property
    def channels(self) -> Iterable[NotificationChannel]:
        return tuple(self._channels)

    async def dispatch(self, notification: Notification) -> None:
        await self._event_bus.publish(NOTIFICATION_TOPIC, notification)
        if not self._channels:
            return
        await asyncio.gather(
            *(self._safe_send(channel, notification) for channel in self._channels if channel.enabled),
            return_exceptions=True,
        )

    async def _safe_send(self, channel: NotificationChannel, notification: Notification) -> None:
        try:
            await channel.send(notification)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "notification_failed",
                channel=channel.name,
                error=str(exc),
            )

    async def apply_settings(self, settings: NotificationSettings) -> None:
        """Rebuild channels when settings change so credential / toggle
        updates take effect without an application restart."""

        self._settings = settings
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
        self._channels = self._build_channels(self._http_session)
        self._logger.info(
            "notification_channels_refreshed",
            channels=[c.name for c in self._channels if c.enabled],
        )
