"""Asyncio bridges for PyQt6 widgets."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


def run_async(coro: Awaitable[T]) -> asyncio.Task[T]:
    """Schedule an awaitable on the running asyncio loop."""

    loop = asyncio.get_event_loop()
    return loop.create_task(coro)
