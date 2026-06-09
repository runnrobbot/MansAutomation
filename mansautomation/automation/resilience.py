"""Session resilience monitoring.

A production-grade heartbeat watcher that keeps automation stable across tab
freezes, websocket reconnects, and hydration delays. Nothing here bypasses
queues or anti-bot mechanisms; it is purely observational so the operator
never silently loses progress.

Only one primitive is exported:

- :class:`HeartbeatWatchdog` - polls a lightweight liveness probe at a fixed
  interval and emits ``heartbeat_unhealthy`` / ``heartbeat_recovered`` events.
  Used to detect tab freezes and dead browser pages. It never touches the
  page beyond a read-only ``document.hidden`` check, so it is safe to run
  during a queue / waiting-room wait.

Queue / waiting-room progression is tracked authoritatively by the plugin's
own ``_wait_through_queue`` (which reads queue-it's embedded JSON model), so
no separate text-scraping queue watcher is needed here.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Page

# ---------------------------------------------------------------------- types

EventFn = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class WatcherStats:
    started_at: float = field(default_factory=time.monotonic)
    ticks: int = 0
    last_change_at: float = field(default_factory=time.monotonic)
    is_healthy: bool = True


# ----------------------------------------------------------- heartbeat watcher


class HeartbeatWatchdog:
    """Polls the page on a fixed interval to detect freezes / disconnects.

    The probe is read-only and resolves quickly; if it raises or times out the
    page is considered momentarily unresponsive. Defaults are deliberately
    tolerant so transient slowness during a high-traffic sale does not produce
    false ``heartbeat_unhealthy`` alarms. On ``failure_threshold`` consecutive
    failures the watchdog emits ``heartbeat_unhealthy``; on the next success it
    emits ``heartbeat_recovered``.

    The watchdog never closes, reloads, or otherwise mutates the page - it only
    reports state via ``on_event``.

    Example::

        watchdog = HeartbeatWatchdog(page, on_event)
        await watchdog.start()
        ...
        await watchdog.stop()
    """

    def __init__(
        self,
        page: Page,
        on_event: EventFn,
        *,
        interval_seconds: float = 8.0,
        failure_threshold: int = 5,
        probe_timeout_seconds: float = 8.0,
    ) -> None:
        self._page = page
        self._on_event = on_event
        self._interval = max(1.0, interval_seconds)
        self._failure_threshold = max(1, failure_threshold)
        self._probe_timeout = max(1.0, probe_timeout_seconds)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.stats = WatcherStats()
        self._failures = 0

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="heartbeat-watchdog")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                return
            except asyncio.TimeoutError:
                pass
            ok = await self._probe()
            self.stats.ticks += 1
            if ok:
                if not self.stats.is_healthy:
                    self.stats.is_healthy = True
                    self.stats.last_change_at = time.monotonic()
                    await self._on_event(
                        "heartbeat_recovered",
                        {"ticks": self.stats.ticks, "url": self._safe_url()},
                    )
                self._failures = 0
                continue
            self._failures += 1
            if self._failures >= self._failure_threshold and self.stats.is_healthy:
                self.stats.is_healthy = False
                self.stats.last_change_at = time.monotonic()
                await self._on_event(
                    "heartbeat_unhealthy",
                    {
                        "consecutive_failures": self._failures,
                        "url": self._safe_url(),
                    },
                )

    async def _probe(self) -> bool:
        try:
            return bool(
                await asyncio.wait_for(
                    self._page.evaluate("() => typeof document !== 'undefined' && !document.hidden"),
                    timeout=self._probe_timeout,
                )
            )
        except Exception:  # noqa: BLE001
            return False

    def _safe_url(self) -> str:
        try:
            return self._page.url or ""
        except Exception:  # noqa: BLE001
            return ""
