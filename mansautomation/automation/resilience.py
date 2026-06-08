"""Session resilience and queue-state monitoring.

Production-grade helpers that keep automation stable across queue resets,
websocket reconnects, hydration delays, and tab-freeze events. Nothing here
bypasses queues or anti-bot mechanisms; everything is purely about staying
synchronised with the page's own progression so the operator never silently
loses progress.

Three primitives are exported:

- :class:`HeartbeatWatchdog`  - polls a coroutine probe at a fixed interval
  and emits ``unhealthy`` / ``recovered`` events. Used to detect tab freezes
  and dead browser pages.
- :class:`QueueStateWatcher`  - tracks queue / waiting-room progression by
  reading the visible counter / position number, emitting events when the
  state advances, regresses, freezes, or disappears.
- :class:`SessionMonitor`     - watches authentication cookies + DOM signals
  and yields a "session lost" event so the runner can pause and ask for a
  manual reauthentication instead of silently retrying.

All three are cooperative async tasks that can be started/stopped from
inside a plugin or the runner without blocking other workflows.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import BrowserContext, Page

# ---------------------------------------------------------------------- types

ProbeFn = Callable[[Page], Awaitable[bool]]
EventFn = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class WatcherStats:
    started_at: float = field(default_factory=time.monotonic)
    ticks: int = 0
    last_change_at: float = field(default_factory=time.monotonic)
    last_value: Any = None
    is_healthy: bool = True


# ----------------------------------------------------------- heartbeat watcher


class HeartbeatWatchdog:
    """Polls the page on a fixed interval to detect freezes / disconnects.

    The probe should resolve quickly and never raise; if it does, the page is
    considered unhealthy. On three consecutive failures the watchdog emits
    ``heartbeat_unhealthy``; on the next success it emits ``heartbeat_recovered``.

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
        interval_seconds: float = 4.0,
        failure_threshold: int = 3,
        probe_timeout_seconds: float = 3.0,
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


# --------------------------------------------------------- queue state watcher


_QUEUE_NUMBER_RE = re.compile(r"(\d{1,8})")


class QueueStateWatcher:
    """Monitors waiting-room / queue state and emits structured events.

    The watcher periodically reads the visible queue position from any of the
    well-known waiting-room selectors. It compares the new position against
    the last observed value and emits one of:

    - ``queue_advanced``   - position decreased (good)
    - ``queue_regressed``  - position increased (queue reset / desync)
    - ``queue_frozen``     - no change for ``freeze_seconds`` consecutive ticks
    - ``queue_cleared``    - the queue indicator disappeared (passed through)
    - ``queue_reconnected``- the indicator reappeared after being gone

    The watcher does **not** interact with the page; it is purely observational.
    The runner uses the events to decide whether to keep waiting, pause, or
    notify the operator.
    """

    DEFAULT_SELECTORS: tuple[str, ...] = (
        "[id*='queue' i]",
        "[class*='queue' i]",
        "[data-testid*='queue' i]",
        "[id*='waiting' i]",
        "[class*='waiting-room' i]",
        "[class*='WaitingRoom']",
        "[data-testid*='waitingRoom' i]",
        "iframe[src*='queue-it']",
        "iframe[src*='waitingroom']",
    )

    def __init__(
        self,
        page: Page,
        on_event: EventFn,
        *,
        interval_seconds: float = 2.0,
        freeze_seconds: float = 90.0,
        selectors: tuple[str, ...] | None = None,
    ) -> None:
        self._page = page
        self._on_event = on_event
        self._interval = max(0.5, interval_seconds)
        self._freeze_seconds = max(10.0, freeze_seconds)
        self._selectors = selectors or self.DEFAULT_SELECTORS
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.stats = WatcherStats()
        self._present = False

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="queue-state-watcher")

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
            await self._tick()

    async def _tick(self) -> None:
        try:
            indicator = await self._read_indicator()
        except Exception:  # noqa: BLE001
            return
        self.stats.ticks += 1
        now = time.monotonic()

        if indicator is None:
            if self._present:
                self._present = False
                self.stats.last_change_at = now
                self.stats.last_value = None
                await self._on_event("queue_cleared", {"url": self._safe_url()})
            return

        if not self._present:
            self._present = True
            self.stats.last_change_at = now
            self.stats.last_value = indicator
            await self._on_event(
                "queue_reconnected" if self.stats.ticks > 1 else "queue_detected",
                {"indicator": indicator, "url": self._safe_url()},
            )
            return

        previous = self.stats.last_value
        if indicator == previous:
            if (now - self.stats.last_change_at) >= self._freeze_seconds:
                # Don't spam: only emit once per freeze period.
                self.stats.last_change_at = now
                await self._on_event(
                    "queue_frozen",
                    {
                        "indicator": indicator,
                        "frozen_seconds": int(self._freeze_seconds),
                        "url": self._safe_url(),
                    },
                )
            return

        # Position changed.
        self.stats.last_change_at = now
        self.stats.last_value = indicator
        prev_n = _extract_number(previous)
        new_n = _extract_number(indicator)
        if prev_n is not None and new_n is not None:
            if new_n < prev_n:
                await self._on_event(
                    "queue_advanced",
                    {"from": prev_n, "to": new_n, "indicator": indicator, "url": self._safe_url()},
                )
            elif new_n > prev_n:
                await self._on_event(
                    "queue_regressed",
                    {"from": prev_n, "to": new_n, "indicator": indicator, "url": self._safe_url()},
                )
        else:
            await self._on_event(
                "queue_changed",
                {"from": previous, "to": indicator, "url": self._safe_url()},
            )

    async def _read_indicator(self) -> str | None:
        for selector in self._selectors:
            try:
                locator = self._page.locator(selector).first
                if not await locator.is_visible(timeout=200):
                    continue
                text = (await locator.inner_text()).strip()
                if text:
                    return _shorten(text, 240)
            except Exception:  # noqa: BLE001
                continue
        return None

    def _safe_url(self) -> str:
        try:
            return self._page.url or ""
        except Exception:  # noqa: BLE001
            return ""


def _extract_number(value: Any) -> int | None:
    if value is None:
        return None
    match = _QUEUE_NUMBER_RE.search(str(value))
    return int(match.group(1)) if match else None


def _shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "\u2026"


# ----------------------------------------------------------- session monitor


class SessionMonitor:
    """Watches a browser context for authentication-cookie loss + DOM signals.

    The monitor tracks a baseline cookie set captured when ``start`` is called.
    On each tick it diffs the current cookies against the baseline and fires
    ``session_lost`` if any of the tracked cookies disappear or change to
    empty values. It also probes a ``signed_in_probe`` callback (typically
    the plugin's ``_is_signed_in`` method) to catch logout flows that don't
    involve cookie clears.
    """

    def __init__(
        self,
        context: BrowserContext,
        signed_in_probe: Callable[[], Awaitable[bool]],
        on_event: EventFn,
        *,
        cookie_names: tuple[str, ...] = (),
        interval_seconds: float = 6.0,
    ) -> None:
        self._context = context
        self._signed_in_probe = signed_in_probe
        self._on_event = on_event
        self._cookie_names = tuple(name.lower() for name in cookie_names)
        self._interval = max(2.0, interval_seconds)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._baseline: dict[str, str] = {}
        self._was_signed_in = False
        self.stats = WatcherStats()

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._baseline = await self._snapshot_cookies()
        try:
            self._was_signed_in = await self._signed_in_probe()
        except Exception:  # noqa: BLE001
            self._was_signed_in = False
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="session-monitor")

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
            await self._tick()

    async def _tick(self) -> None:
        self.stats.ticks += 1
        try:
            current = await self._snapshot_cookies()
        except Exception:  # noqa: BLE001
            return
        lost = [
            name
            for name, value in self._baseline.items()
            if not current.get(name)
        ]
        if lost and self._was_signed_in:
            self._was_signed_in = False
            self.stats.last_change_at = time.monotonic()
            await self._on_event(
                "session_lost",
                {"cookies_missing": lost, "reason": "cookie_cleared"},
            )
            return

        try:
            signed = await self._signed_in_probe()
        except Exception:  # noqa: BLE001
            return
        if signed != self._was_signed_in:
            self.stats.last_change_at = time.monotonic()
            self._was_signed_in = signed
            await self._on_event(
                "session_recovered" if signed else "session_lost",
                {"reason": "dom_probe"},
            )
            if signed:
                # Refresh baseline so the next loss is correctly detected.
                self._baseline = current

    async def _snapshot_cookies(self) -> dict[str, str]:
        try:
            cookies = await self._context.cookies()
        except Exception:  # noqa: BLE001
            return {}
        result: dict[str, str] = {}
        for cookie in cookies:
            try:
                name = str(cookie.get("name", "")).lower()
                value = str(cookie.get("value", ""))
            except Exception:  # noqa: BLE001
                continue
            if not name:
                continue
            if self._cookie_names and name not in self._cookie_names:
                continue
            if value:
                result[name] = value
        return result
