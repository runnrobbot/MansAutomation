"""Lightweight asyncio-aware event bus for cross-component communication."""

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")

Listener = Callable[[Any], Awaitable[None] | None]


class EventBus:
    """Decoupled publish/subscribe channel.

    Subscribers may register sync or async callbacks. All notifications happen
    on the event loop the publisher is running on.
    """

    def __init__(self) -> None:
        self._listeners: dict[str, list[Listener]] = defaultdict(list)
        self._lock = asyncio.Lock()

    def subscribe(self, topic: str, listener: Listener) -> Callable[[], None]:
        self._listeners[topic].append(listener)

        def _unsubscribe() -> None:
            try:
                self._listeners[topic].remove(listener)
            except ValueError:
                pass

        return _unsubscribe

    async def publish(self, topic: str, payload: Any) -> None:
        listeners = list(self._listeners.get(topic, ()))
        if not listeners:
            return
        for listener in listeners:
            try:
                result = listener(payload)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 - listeners must not break publishers
                continue

    def publish_threadsafe(self, topic: str, payload: Any) -> None:
        """Publish from a non-asyncio context onto the running loop."""

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(self.publish(topic, payload), loop)
