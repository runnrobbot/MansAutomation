"""Playwright browser lifecycle management."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Route,
    async_playwright,
)

from mansautomation.core.config import BrowserSettings
from mansautomation.core.exceptions import BrowserError
from mansautomation.services.logging_service import LoggingService


class BrowserService:
    """High-performance Playwright wrapper with persistent contexts and preloading."""

    def __init__(
        self,
        settings: BrowserSettings,
        sessions_dir: Path,
        logging_service: LoggingService,
    ) -> None:
        self._settings = settings
        self._sessions_dir = sessions_dir
        self._logger = logging_service.get_logger("browser")
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._contexts: dict[str, BrowserContext] = {}
        self._context_last_used: dict[str, float] = {}
        # Sessions currently held by a running workflow. An active session is
        # NEVER torn down by the idle reaper, no matter how long it waits (a
        # queue / pre-queue wait can legitimately run for hours).
        self._active_sessions: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._preload_task: asyncio.Task[None] | None = None
        self._idle_reaper_task: asyncio.Task[None] | None = None
        self._stopped = False
        # Contexts idle for longer than this are torn down to release memory.
        # The runner re-acquires them lazily on the next workflow.
        self._idle_ttl_seconds: float = 15 * 60

    async def start(self) -> None:
        """Eagerly start Playwright in the background to minimise first-job latency."""

        if self._preload_task is None:
            self._preload_task = asyncio.create_task(self._preload(), name="browser-preload")
        if self._idle_reaper_task is None:
            self._idle_reaper_task = asyncio.create_task(
                self._reap_idle_contexts(), name="browser-idle-reaper"
            )

    async def _preload(self) -> None:
        try:
            await self._ensure_playwright()
            self._logger.info("browser_preloaded", engine=self._settings.engine)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("browser_preload_failed", error=str(exc))

    async def stop(self) -> None:
        self._stopped = True
        if self._preload_task is not None:
            self._preload_task.cancel()
            try:
                await self._preload_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._preload_task = None
        if self._idle_reaper_task is not None:
            self._idle_reaper_task.cancel()
            try:
                await self._idle_reaper_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._idle_reaper_task = None
        async with self._lock:
            for context in list(self._contexts.values()):
                try:
                    await context.close()
                except Exception:  # noqa: BLE001
                    continue
            self._contexts.clear()
            if self._browser is not None:
                try:
                    await self._browser.close()
                except Exception:  # noqa: BLE001
                    pass
                self._browser = None
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except Exception:  # noqa: BLE001
                    pass
                self._playwright = None

    async def _ensure_playwright(self) -> Playwright:
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        return self._playwright

    def _resolve_browser_type(self, pw: Playwright) -> Any:
        return getattr(pw, self._settings.engine)

    async def acquire_context(self, *, session_name: str = "default") -> BrowserContext:
        """Return a persistent browser context, reusing a cached one when possible."""

        if self._stopped:
            raise BrowserError("browser service has been stopped")
        async with self._lock:
            cached = self._contexts.get(session_name)
            if cached is not None:
                if self._is_context_alive(cached):
                    self._context_last_used[session_name] = asyncio.get_event_loop().time()
                    return cached
                # Drop a stale cache entry and re-launch below
                self._contexts.pop(session_name, None)
                self._context_last_used.pop(session_name, None)

            pw = await self._ensure_playwright()
            session_path = self._sessions_dir / session_name
            session_path.mkdir(parents=True, exist_ok=True)

            launch_args: list[str] = []
            if self._settings.disable_blink_features:
                launch_args.append("--disable-blink-features=AutomationControlled")

            proxy: dict[str, str] | None = None
            if self._settings.proxy_url:
                proxy = {"server": self._settings.proxy_url}

            try:
                if self._settings.persistent_session:
                    browser_type = self._resolve_browser_type(pw)
                    context = await browser_type.launch_persistent_context(
                        user_data_dir=str(session_path),
                        headless=self._settings.headless,
                        slow_mo=self._settings.slow_mo_ms,
                        viewport={
                            "width": self._settings.viewport_width,
                            "height": self._settings.viewport_height,
                        },
                        locale=self._settings.locale,
                        timezone_id=self._settings.timezone,
                        user_agent=self._settings.user_agent,
                        args=launch_args,
                        proxy=proxy,
                    )
                else:
                    if self._browser is None:
                        browser_type = self._resolve_browser_type(pw)
                        self._browser = await browser_type.launch(
                            headless=self._settings.headless,
                            slow_mo=self._settings.slow_mo_ms,
                            args=launch_args,
                            proxy=proxy,
                        )
                    context = await self._browser.new_context(
                        viewport={
                            "width": self._settings.viewport_width,
                            "height": self._settings.viewport_height,
                        },
                        locale=self._settings.locale,
                        timezone_id=self._settings.timezone,
                        user_agent=self._settings.user_agent,
                    )
            except Exception as exc:
                raise BrowserError(f"failed to launch browser: {exc}") from exc

            context.set_default_timeout(self._settings.default_timeout_ms)
            context.set_default_navigation_timeout(self._settings.navigation_timeout_ms)
            await self._install_resource_blocker(context)

            self._contexts[session_name] = context
            self._context_last_used[session_name] = asyncio.get_event_loop().time()
            self._attach_recovery_hooks(context, session_name)
            self._logger.info("browser_context_ready", session=session_name)
            return context

    async def _install_resource_blocker(self, context: BrowserContext) -> None:
        block_types = {entry.strip().lower() for entry in self._settings.block_resources if entry.strip()}
        if not block_types:
            return

        async def _route(route: Route) -> None:
            try:
                if route.request.resource_type in block_types:
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:  # noqa: BLE001 - routing must never crash navigation
                try:
                    await route.continue_()
                except Exception:  # noqa: BLE001
                    pass

        await context.route("**/*", _route)

    async def new_page(self, *, session_name: str = "default") -> Page:
        context = await self.acquire_context(session_name=session_name)
        page = await context.new_page()
        return page

    def mark_active(self, session_name: str) -> None:
        """Pin a session as in-use by a running workflow.

        While pinned, the idle reaper will not tear the context down even if it
        sits on the same page for hours (e.g. a pre-queue countdown). Safe to
        call more than once; balanced by :meth:`release`.
        """

        self._active_sessions[session_name] = self._active_sessions.get(session_name, 0) + 1
        self._context_last_used[session_name] = asyncio.get_event_loop().time()

    def release(self, session_name: str) -> None:
        """Unpin a session when a workflow finishes and restart its idle timer."""

        remaining = self._active_sessions.get(session_name, 0) - 1
        if remaining <= 0:
            self._active_sessions.pop(session_name, None)
        else:
            self._active_sessions[session_name] = remaining
        self._context_last_used[session_name] = asyncio.get_event_loop().time()

    async def close_session(self, session_name: str) -> None:
        async with self._lock:
            context = self._contexts.pop(session_name, None)
            self._context_last_used.pop(session_name, None)
        if context is not None:
            try:
                await context.close()
            except Exception:  # noqa: BLE001
                pass

    async def apply_settings(self, settings: BrowserSettings) -> None:
        """Apply new browser settings. Contexts are torn down when launch-time
        options change so the next ``acquire_context`` recreates them with the
        new configuration."""

        previous = self._settings
        self._settings = settings
        relaunch = (
            previous.engine != settings.engine
            or previous.headless != settings.headless
            or previous.persistent_session != settings.persistent_session
            or previous.user_agent != settings.user_agent
            or previous.proxy_url != settings.proxy_url
            or previous.viewport_width != settings.viewport_width
            or previous.viewport_height != settings.viewport_height
            or previous.locale != settings.locale
            or previous.timezone != settings.timezone
            or previous.disable_blink_features != settings.disable_blink_features
            or list(previous.block_resources) != list(settings.block_resources)
        )
        if not relaunch:
            # Just refresh the timeouts on existing contexts.
            for context in list(self._contexts.values()):
                try:
                    context.set_default_timeout(settings.default_timeout_ms)
                    context.set_default_navigation_timeout(settings.navigation_timeout_ms)
                except Exception:  # noqa: BLE001
                    continue
            self._logger.info("browser_settings_refreshed")
            return
        sessions = list(self._contexts.keys())
        self._logger.info("browser_settings_relaunch", sessions=sessions)
        for name in sessions:
            await self.close_session(name)

    @staticmethod
    def _is_context_alive(context: BrowserContext) -> bool:
        """Return True if the context's underlying browser is still connected."""

        try:
            browser = context.browser
        except Exception:  # noqa: BLE001
            return False
        if browser is None:
            # Persistent contexts expose ``browser=None``; rely on the pages
            # call to validate the underlying connection.
            try:
                _ = context.pages
                return True
            except Exception:  # noqa: BLE001
                return False
        try:
            return bool(browser.is_connected())
        except Exception:  # noqa: BLE001
            return False

    def _attach_recovery_hooks(self, context: BrowserContext, session_name: str) -> None:
        """Drop the cached context if it crashes / disconnects."""

        def _on_close() -> None:
            self._contexts.pop(session_name, None)
            self._context_last_used.pop(session_name, None)
            self._logger.warning("browser_context_closed", session=session_name)

        try:
            context.on("close", lambda *_: _on_close())
        except Exception:  # noqa: BLE001
            pass

    async def _reap_idle_contexts(self) -> None:
        """Tear down browser contexts that have been idle for too long.

        Persistent contexts hold meaningful memory; reaping them after long
        idle periods keeps long-running desktop sessions stable while still
        being instantly re-acquirable when the next workflow starts.
        """

        try:
            while not self._stopped:
                await asyncio.sleep(60)
                now = asyncio.get_event_loop().time()
                victims: list[str] = []
                async with self._lock:
                    for name, last_used in list(self._context_last_used.items()):
                        if name in self._active_sessions:
                            # A workflow is actively using this context (e.g.
                            # waiting in a queue). Never reap it mid-run.
                            continue
                        if now - last_used >= self._idle_ttl_seconds:
                            victims.append(name)
                for name in victims:
                    self._logger.info("browser_context_reaping_idle", session=name)
                    await self.close_session(name)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("browser_idle_reaper_error", error=str(exc))
