"""DOM, hydration, and network synchronization primitives.

This module centralises the production-grade waiting strategies used by the
automation runner and plugins. It provides cooperative, low-overhead routines
that keep automation stable on React, Vue, Next.js, and other SPA stacks where
naive ``wait_for_load_state('networkidle')`` is unreliable.

Strategies implemented:

- ``wait_for_dom_settle``   — MutationObserver-based DOM-settle detection.
- ``wait_for_hydration``    — Detects React / Next.js hydration completion.
- ``wait_for_network_quiet``— Adaptive request-tracker waiter, more forgiving
                              than Playwright's ``networkidle`` event under
                              websocket / polling scenarios.
- ``wait_for_page_ready``   — Composite helper executed by the runner after
                              every navigation.

All routines are non-blocking from Qt's point of view (they cooperate with
asyncio) and are safe to call from any plugin without disrupting the main
automation context. They never raise on timeout - they degrade gracefully and
leave the caller free to continue with adaptive locator strategies.
"""

from __future__ import annotations

import asyncio

from playwright.async_api import Page

# JS executed inside the page to wait for DOM mutations to settle. The script
# resolves once no mutation has been observed for ``quiet_ms`` milliseconds, or
# the supplied ``timeout_ms`` budget is exhausted.
_DOM_SETTLE_SCRIPT = """
async ([quietMs, timeoutMs]) => {
    return await new Promise((resolve) => {
        if (!('MutationObserver' in window)) {
            resolve('no-observer');
            return;
        }
        let lastChange = performance.now();
        const observer = new MutationObserver(() => {
            lastChange = performance.now();
        });
        try {
            observer.observe(document.documentElement, {
                childList: true,
                subtree: true,
                attributes: true,
                characterData: true,
            });
        } catch (err) {
            resolve('observer-failed');
            return;
        }
        const start = performance.now();
        const tick = () => {
            const now = performance.now();
            if (now - lastChange >= quietMs) {
                observer.disconnect();
                resolve('settled');
                return;
            }
            if (now - start >= timeoutMs) {
                observer.disconnect();
                resolve('timeout');
                return;
            }
            // Use rAF when available for cheaper polling; fall back to a
            // setTimeout micro-pulse to keep the main thread responsive.
            if (typeof requestIdleCallback === 'function') {
                requestIdleCallback(tick, { timeout: 80 });
            } else {
                setTimeout(tick, 60);
            }
        };
        tick();
    });
}
"""

# Heuristics for React/Next.js hydration detection. The page is considered
# hydrated when (a) ``document.readyState === 'complete'``, AND (b) any of the
# framework signals indicate a mounted root.
_HYDRATION_SCRIPT = """
async (timeoutMs) => {
    const start = performance.now();
    const isHydrated = () => {
        if (document.readyState !== 'complete') return false;
        // Next.js
        const next = window.__NEXT_DATA__;
        if (next && document.getElementById('__next')) {
            const root = document.getElementById('__next');
            if (root && root.children.length > 0) return true;
        }
        // React 18 root attribute
        const reactRoots = document.querySelectorAll('[data-reactroot], [data-rh="true"]');
        if (reactRoots.length > 0) return true;
        // React fiber probe
        const probe = document.querySelector('#root, #app, main, [id^="app"]');
        if (probe) {
            for (const key of Object.keys(probe)) {
                if (key.startsWith('__reactContainer$') || key.startsWith('__reactFiber$')) {
                    return true;
                }
            }
            if (probe.children.length > 0) return true;
        }
        // Vue 3
        if (window.__VUE__ || window.Vue) return true;
        // Generic SPA fallback - app-mount markers
        const mounted = document.querySelector('[data-mounted="true"], [data-hydrated="true"]');
        if (mounted) return true;
        // If body has appreciable text/elements treat the page as ready.
        return (document.body && document.body.childElementCount > 2);
    };
    if (isHydrated()) return 'ready';
    return await new Promise((resolve) => {
        const tick = () => {
            if (isHydrated()) {
                resolve('ready');
                return;
            }
            if (performance.now() - start >= timeoutMs) {
                resolve('timeout');
                return;
            }
            if (typeof requestAnimationFrame === 'function') {
                requestAnimationFrame(tick);
            } else {
                setTimeout(tick, 80);
            }
        };
        tick();
    });
}
"""


async def wait_for_dom_settle(
    page: Page,
    *,
    quiet_ms: int = 350,
    timeout_ms: int = 6_000,
) -> str:
    """Wait until no DOM mutations have occurred for ``quiet_ms`` milliseconds.

    Returns one of ``"settled"``, ``"timeout"``, ``"no-observer"``,
    ``"observer-failed"``, or ``"error"``. Never raises.
    """

    try:
        result = await page.evaluate(_DOM_SETTLE_SCRIPT, [quiet_ms, timeout_ms])
        return str(result) if isinstance(result, str) else "error"
    except Exception:  # noqa: BLE001
        return "error"


async def wait_for_hydration(page: Page, *, timeout_ms: int = 8_000) -> str:
    """Wait until the page hydrates a React/Vue/Next.js root.

    Returns ``"ready"``, ``"timeout"``, or ``"error"``. Never raises.
    """

    try:
        result = await page.evaluate(_HYDRATION_SCRIPT, timeout_ms)
        return str(result) if isinstance(result, str) else "error"
    except Exception:  # noqa: BLE001
        return "error"


async def wait_for_network_quiet(
    page: Page,
    *,
    quiet_ms: int = 400,
    timeout_ms: int = 4_000,
    ignore_websocket: bool = True,
) -> str:
    """Adaptive request-tracker network-idle helper.

    Unlike Playwright's ``networkidle`` event, this helper:

    - Tolerates long-lived websocket / SSE connections that never go idle.
    - Resets when a meaningful HTTP request fires, but ignores beacons,
      ping requests, and navigator.sendBeacon traffic that frequently
      churns on Next.js apps.
    - Returns gracefully on timeout without raising.
    """

    in_flight = 0
    last_change = asyncio.get_event_loop().time()
    completed = asyncio.Event()

    def _matters(request_url: str, resource_type: str) -> bool:
        if ignore_websocket and resource_type in {"websocket", "eventsource"}:
            return False
        if "beacon" in request_url or "/ping" in request_url:
            return False
        # Ignore long-lived telemetry / analytics that never goes idle.
        for noisy in (
            "google-analytics.com",
            "googletagmanager.com",
            "doubleclick.net",
            "facebook.com/tr",
            "/heartbeat",
            "/v1/event",
            "/api/log",
            "/track",
        ):
            if noisy in request_url:
                return False
        return resource_type in {
            "document",
            "fetch",
            "xhr",
            "script",
            "stylesheet",
        }

    def _on_request(request: object) -> None:
        nonlocal in_flight, last_change
        try:
            if not _matters(request.url, request.resource_type):  # type: ignore[attr-defined]
                return
        except Exception:  # noqa: BLE001
            return
        in_flight += 1
        last_change = asyncio.get_event_loop().time()

    def _on_done(request: object) -> None:
        nonlocal in_flight, last_change
        try:
            if not _matters(request.url, request.resource_type):  # type: ignore[attr-defined]
                return
        except Exception:  # noqa: BLE001
            return
        in_flight = max(0, in_flight - 1)
        last_change = asyncio.get_event_loop().time()
        if in_flight == 0:
            completed.set()

    page.on("request", _on_request)
    page.on("requestfinished", _on_done)
    page.on("requestfailed", _on_done)
    try:
        deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000.0)
        quiet_seconds = quiet_ms / 1000.0
        while True:
            now = asyncio.get_event_loop().time()
            if now >= deadline:
                return "timeout"
            if in_flight == 0 and (now - last_change) >= quiet_seconds:
                return "quiet"
            try:
                await asyncio.wait_for(completed.wait(), timeout=min(0.25, deadline - now))
            except asyncio.TimeoutError:
                pass
            completed.clear()
    finally:
        try:
            page.remove_listener("request", _on_request)
            page.remove_listener("requestfinished", _on_done)
            page.remove_listener("requestfailed", _on_done)
        except Exception:  # noqa: BLE001
            pass


async def wait_for_page_ready(
    page: Page,
    *,
    hydration_timeout_ms: int = 8_000,
    settle_quiet_ms: int = 350,
    settle_timeout_ms: int = 6_000,
    network_timeout_ms: int = 6_000,
) -> dict[str, str]:
    """Composite synchronizer used after navigation.

    Runs hydration, network-quiet, and DOM-settle waits in series with
    bounded budgets. Always returns a status dict; never raises.
    """

    statuses: dict[str, str] = {}
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=hydration_timeout_ms)
        statuses["domcontentloaded"] = "ok"
    except Exception:  # noqa: BLE001
        statuses["domcontentloaded"] = "timeout"

    statuses["hydration"] = await wait_for_hydration(page, timeout_ms=hydration_timeout_ms)
    statuses["network"] = await wait_for_network_quiet(page, timeout_ms=network_timeout_ms)
    statuses["dom_settle"] = await wait_for_dom_settle(
        page, quiet_ms=settle_quiet_ms, timeout_ms=settle_timeout_ms
    )
    return statuses
