"""Asyncio bridges for PyQt6 widgets."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from PyQt6.QtCore import QObject, pyqtSignal

T = TypeVar("T")


def run_async(coro: Awaitable[T]) -> asyncio.Task[T]:
    """Schedule an awaitable on the running asyncio loop."""

    loop = asyncio.get_event_loop()
    return loop.create_task(coro)


class AsyncSignalRelay(QObject):
    """Relays values from coroutines into Qt signals on the main thread."""

    payload = pyqtSignal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)


def emit_threadsafe(signal: pyqtSignal, value: Any) -> None:
    """Emit a Qt signal from a coroutine context."""

    signal.emit(value)


def on_qt_event(callback: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator for Qt signal handlers that may invoke coroutines."""

    def _wrapper(*args: Any, **kwargs: Any) -> Any:
        result = callback(*args, **kwargs)
        if asyncio.iscoroutine(result):
            run_async(result)
        return result

    return _wrapper
