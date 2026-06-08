"""Notification protocol and message types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class NotificationLevel(StrEnum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Notification:
    title: str
    message: str
    level: NotificationLevel = NotificationLevel.INFO


class NotificationChannel(Protocol):
    name: str

    async def send(self, notification: Notification) -> None: ...
    @property
    def enabled(self) -> bool: ...
