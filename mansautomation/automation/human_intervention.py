"""CAPTCHA, queue, and waiting-room detection.

When detected, the runner pauses automation and notifies the user. We never
implement bypass logic - automation simply yields control until the user
indicates manual interaction is complete.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from playwright.async_api import Page

_CAPTCHA_HOST_PATTERNS = (
    re.compile(r"recaptcha\.net", re.I),
    re.compile(r"google\.com/recaptcha", re.I),
    re.compile(r"hcaptcha\.com", re.I),
    re.compile(r"cloudflare\.com/cdn-cgi/challenge", re.I),
    re.compile(r"challenges\.cloudflare\.com", re.I),
    re.compile(r"arkoselabs\.com", re.I),
    re.compile(r"funcaptcha\.com", re.I),
    re.compile(r"geetest\.com", re.I),
)

_QUEUE_HOST_PATTERNS = (
    re.compile(r"queue-it\.net", re.I),
    re.compile(r"queue\.fastly\.net", re.I),
    re.compile(r"queueing", re.I),
    re.compile(r"waitingroom", re.I),
)

_BODY_KEYWORDS = (
    "are you human",
    "verify you are not a robot",
    "checking your browser",
    "please complete the security check",
    "captcha",
    "queue position",
    "waiting room",
    "you are now in line",
)


@dataclass(slots=True)
class HumanInterventionSignal:
    reason: str
    detail: str
    url: str


class HumanInterventionDetector:
    """Inspects pages and frames for CAPTCHA / queue indicators."""

    async def detect(self, page: Page) -> HumanInterventionSignal | None:
        signal = self._inspect_url(page.url)
        if signal:
            return signal
        for frame in page.frames:
            signal = self._inspect_url(frame.url or "")
            if signal:
                return signal
        signal = await self._inspect_body(page)
        return signal

    def _inspect_url(self, url: str) -> HumanInterventionSignal | None:
        if not url:
            return None
        for pattern in _CAPTCHA_HOST_PATTERNS:
            if pattern.search(url):
                return HumanInterventionSignal(
                    reason="captcha",
                    detail=f"captcha-related frame detected: {url}",
                    url=url,
                )
        for pattern in _QUEUE_HOST_PATTERNS:
            if pattern.search(url):
                return HumanInterventionSignal(
                    reason="queue",
                    detail=f"queue / waiting-room detected: {url}",
                    url=url,
                )
        return None

    async def _inspect_body(self, page: Page) -> HumanInterventionSignal | None:
        try:
            content_snippet: str = await page.evaluate(
                """() => (document.body ? document.body.innerText.slice(0, 4000).toLowerCase() : '')"""
            )
        except Exception:  # noqa: BLE001
            return None
        for keyword in _BODY_KEYWORDS:
            if keyword in content_snippet:
                reason = "queue" if "queue" in keyword or "waiting" in keyword else "captcha"
                return HumanInterventionSignal(
                    reason=reason,
                    detail=f"body keyword matched: {keyword}",
                    url=page.url,
                )
        return None
