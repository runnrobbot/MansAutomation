"""Production-grade resilient interactions for highly dynamic SPAs.

These helpers add stale-element recovery, selector fallback chains, and
state-synchronization guards on top of Playwright. They are designed for
React/Next.js workloads where elements rerender mid-interaction, lazy-load,
toggle disabled state, and surface inside dynamically rendered modals.

Every helper accepts a ``Page`` and a tuple of selector candidates. The first
selector that matches a visible, enabled, on-screen element wins; if the
match fails mid-interaction (stale element, detached DOM node, transient
disabled state) the helper retries with adaptive backoff and re-resolves the
locator from scratch on each attempt.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Frame, Locator, Page, TimeoutError as PlaywrightTimeout


@dataclass(frozen=True, slots=True)
class InteractionResult:
    """Outcome of a resilient interaction call."""

    success: bool
    selector: str | None = None
    attempts: int = 0
    detail: str = ""


_TRANSIENT_KEYWORDS = (
    "stale",
    "detached",
    "not attached",
    "element is not",
    "intercepts pointer",
    "is not enabled",
    "subtree",
)


def _is_transient(error: BaseException) -> bool:
    msg = str(error).lower()
    return any(token in msg for token in _TRANSIENT_KEYWORDS)


async def _backoff(attempt: int, base: float = 0.18, cap: float = 1.6) -> None:
    delay = min(cap, base * (2 ** attempt))
    delay += random.uniform(0, base)
    await asyncio.sleep(delay)


async def resolve_first_visible(
    page: Page,
    selectors: Sequence[str],
    *,
    frame: Frame | None = None,
    timeout_ms: int = 4_000,
) -> tuple[str, Locator] | None:
    """Return ``(selector, locator)`` for the first selector that resolves to a
    visible element within ``timeout_ms``.

    The probe is cheap: each candidate is given an equal share of the budget,
    capped at 1.5s per attempt to avoid head-of-line blocking on a single
    selector that never appears.
    """

    if not selectors:
        return None
    target = frame or page.main_frame
    per_attempt = max(400, min(1_500, timeout_ms // max(1, len(selectors))))
    deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000.0)
    for selector in selectors:
        if asyncio.get_event_loop().time() >= deadline:
            break
        try:
            locator = target.locator(selector).first
            await locator.wait_for(state="visible", timeout=per_attempt)
            return selector, locator
        except PlaywrightTimeout:
            continue
        except Exception:  # noqa: BLE001
            continue
    return None


async def resilient_click(
    page: Page,
    selectors: Sequence[str],
    *,
    frame: Frame | None = None,
    max_attempts: int = 4,
    timeout_ms: int = 6_000,
    require_enabled: bool = True,
    scroll_into_view: bool = True,
    post_click_wait: float = 0.0,
    matcher: Callable[[Locator], "Any"] | None = None,
) -> InteractionResult:
    """Click the first viable element from a candidate list with full
    stale/transient-error recovery.

    The helper re-resolves the locator on every attempt so that React
    rerenders or detached fibers do not poison subsequent retries.

    ``matcher`` may be supplied to add an extra predicate (e.g. ensure the
    button text matches a regex) before clicking. It is awaited if it returns
    a coroutine.
    """

    last_detail = ""
    for attempt in range(max_attempts):
        resolved = await resolve_first_visible(
            page, selectors, frame=frame, timeout_ms=timeout_ms
        )
        if resolved is None:
            last_detail = "no candidate visible"
            await _backoff(attempt)
            continue
        selector, locator = resolved
        try:
            if matcher is not None:
                match = matcher(locator)
                if asyncio.iscoroutine(match):
                    match = await match
                if not match:
                    await _backoff(attempt)
                    continue
            if scroll_into_view:
                try:
                    await locator.scroll_into_view_if_needed(timeout=1_500)
                except Exception:  # noqa: BLE001 - scrolling is best-effort
                    pass
            if require_enabled:
                try:
                    if not await locator.is_enabled():
                        last_detail = "element disabled"
                        await _backoff(attempt)
                        continue
                except Exception:  # noqa: BLE001
                    pass
            await locator.click(timeout=2_500)
            if post_click_wait > 0:
                await asyncio.sleep(post_click_wait)
            return InteractionResult(
                success=True, selector=selector, attempts=attempt + 1, detail="clicked"
            )
        except Exception as exc:  # noqa: BLE001
            last_detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            if not _is_transient(exc) and attempt >= 1:
                # Non-transient errors after the first attempt: stop hammering.
                return InteractionResult(
                    success=False,
                    selector=selector,
                    attempts=attempt + 1,
                    detail=last_detail,
                )
            await _backoff(attempt)
    return InteractionResult(success=False, attempts=max_attempts, detail=last_detail)


async def resilient_fill(
    page: Page,
    selectors: Sequence[str],
    value: str,
    *,
    frame: Frame | None = None,
    max_attempts: int = 4,
    timeout_ms: int = 5_000,
    typing_delay_ms: int = 12,
) -> InteractionResult:
    """Fill the first viable input with ``value``, with stale-recovery."""

    last_detail = ""
    for attempt in range(max_attempts):
        resolved = await resolve_first_visible(
            page, selectors, frame=frame, timeout_ms=timeout_ms
        )
        if resolved is None:
            last_detail = "no candidate visible"
            await _backoff(attempt)
            continue
        selector, locator = resolved
        try:
            try:
                await locator.scroll_into_view_if_needed(timeout=1_500)
            except Exception:  # noqa: BLE001
                pass
            try:
                await locator.fill("", timeout=1_500)
            except Exception:  # noqa: BLE001
                pass
            await locator.click(timeout=1_500)
            await locator.type(value, delay=max(0, typing_delay_ms))
            try:
                await locator.dispatch_event("input")
                await locator.dispatch_event("change")
                await locator.dispatch_event("blur")
            except Exception:  # noqa: BLE001
                pass
            return InteractionResult(
                success=True, selector=selector, attempts=attempt + 1, detail="filled"
            )
        except Exception as exc:  # noqa: BLE001
            last_detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            if not _is_transient(exc) and attempt >= 1:
                return InteractionResult(
                    success=False,
                    selector=selector,
                    attempts=attempt + 1,
                    detail=last_detail,
                )
            await _backoff(attempt)
    return InteractionResult(success=False, attempts=max_attempts, detail=last_detail)


async def wait_for_enabled(
    page: Page,
    selectors: Sequence[str],
    *,
    frame: Frame | None = None,
    timeout_ms: int = 8_000,
) -> InteractionResult:
    """Block until a candidate element is visible AND enabled."""

    deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000.0)
    last_detail = ""
    while asyncio.get_event_loop().time() < deadline:
        resolved = await resolve_first_visible(
            page, selectors, frame=frame, timeout_ms=600
        )
        if resolved is not None:
            selector, locator = resolved
            try:
                if await locator.is_enabled():
                    return InteractionResult(
                        success=True, selector=selector, detail="enabled"
                    )
                last_detail = "disabled"
            except Exception as exc:  # noqa: BLE001
                last_detail = str(exc).splitlines()[0]
        await asyncio.sleep(0.2)
    return InteractionResult(success=False, detail=last_detail or "timeout")


async def click_when_enabled(
    page: Page,
    selectors: Sequence[str],
    *,
    frame: Frame | None = None,
    enable_timeout_ms: int = 8_000,
    click_timeout_ms: int = 4_000,
) -> InteractionResult:
    """Wait until a candidate is enabled, then perform a resilient click."""

    enabled = await wait_for_enabled(
        page, selectors, frame=frame, timeout_ms=enable_timeout_ms
    )
    if not enabled.success:
        return enabled
    return await resilient_click(
        page,
        [enabled.selector] if enabled.selector else list(selectors),
        frame=frame,
        timeout_ms=click_timeout_ms,
    )


async def wait_for_any(
    page: Page,
    selectors: Sequence[str],
    *,
    frame: Frame | None = None,
    timeout_ms: int = 6_000,
) -> str | None:
    """Return the first selector that becomes visible, or ``None``."""

    resolved = await resolve_first_visible(
        page, selectors, frame=frame, timeout_ms=timeout_ms
    )
    return resolved[0] if resolved else None


async def wait_for_disappear(
    page: Page,
    selectors: Sequence[str],
    *,
    frame: Frame | None = None,
    timeout_ms: int = 8_000,
) -> bool:
    """Wait until none of the supplied selectors are visible (e.g. loading
    skeletons, spinners, modal overlays)."""

    target = frame or page.main_frame
    deadline = asyncio.get_event_loop().time() + (timeout_ms / 1000.0)
    while asyncio.get_event_loop().time() < deadline:
        any_visible = False
        for selector in selectors:
            try:
                if await target.locator(selector).first.is_visible(timeout=200):
                    any_visible = True
                    break
            except Exception:  # noqa: BLE001
                continue
        if not any_visible:
            return True
        await asyncio.sleep(0.2)
    return False


# Default loading-skeleton selectors observed across React / Next.js sites.
DEFAULT_LOADING_SELECTORS: tuple[str, ...] = (
    "[data-testid*='skeleton' i]",
    "[class*='skeleton' i]",
    "[class*='Skeleton']",
    "[class*='loading' i]:not(button):not(a)",
    "[class*='shimmer' i]",
    "[role='progressbar']",
    "[aria-busy='true']",
)
