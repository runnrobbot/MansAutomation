"""Desktop and sound notifications via plyer."""

from __future__ import annotations

import asyncio
from typing import Any

from mansautomation.core.config import DesktopNotificationSettings
from mansautomation.notifications.base import Notification, NotificationChannel


class DesktopNotificationChannel(NotificationChannel):
    name = "desktop"

    def __init__(self, settings: DesktopNotificationSettings) -> None:
        self._settings = settings
        self._notifier: Any | None
        self._sound: Any | None
        try:
            from plyer import notification  # type: ignore[import-untyped]

            self._notifier = notification
        except Exception:  # noqa: BLE001 - plyer is optional at runtime
            self._notifier = None
        try:
            from PyQt6.QtMultimedia import QSoundEffect  # type: ignore[import-not-found]

            self._sound_cls: Any | None = QSoundEffect
        except Exception:  # noqa: BLE001
            self._sound_cls = None
        self._sound = None

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    async def send(self, notification: Notification) -> None:
        if not self.enabled:
            return
        await asyncio.to_thread(self._send_sync, notification)

    def _send_sync(self, notification: Notification) -> None:
        if self._notifier is None:
            return
        # Windows balloon tips cap at 256 chars; trim defensively for every OS.
        title = self._truncate(f"MansAutomation - {notification.title}", 60)
        message = self._truncate(notification.message, 200)
        try:
            self._notifier.notify(
                title=title,
                message=message,
                app_name="MansAutomation",
                timeout=6,
            )
        except Exception:  # noqa: BLE001 - desktop notifications are best-effort
            pass
        if self._settings.sound_enabled:
            self._play_sound()

    def _play_sound(self) -> None:
        """Play a short OS-native alert sound. Safe on every platform."""

        try:
            import sys

            if sys.platform == "win32":
                import winsound

                winsound.MessageBeep(winsound.MB_ICONASTERISK)
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            print("\a", end="", flush=True)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[: max(limit - 1, 1)] + "\u2026"
