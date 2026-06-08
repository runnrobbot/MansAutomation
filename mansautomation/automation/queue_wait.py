"""Waiting-room / queue detection and position tracking.

When a high-demand event puts the buyer into a virtual waiting room (queue-it,
tiket.com's own waiting room, etc.) the automation must *wait its turn* rather
than racing ahead and failing. This module detects the queue, reads the
current position / estimated wait, and exposes a status object the plugin uses
to drive an active wait loop.

Nothing here bypasses or skips the queue. It only observes the page's own
progression so the operator's automation stays in sync and resumes package
selection exactly when the queue releases the session.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from playwright.async_api import Page

# URL fragments that indicate a queue / waiting-room host.
_QUEUE_URL_PATTERNS = (
    re.compile(r"queue-it\.net", re.I),
    re.compile(r"queue\.fastly", re.I),
    re.compile(r"waitingroom", re.I),
    re.compile(r"waiting-room", re.I),
    re.compile(r"/queue", re.I),
    re.compile(r"antrian", re.I),
)

# Visible-text markers that indicate a waiting room (ID + EN).
_QUEUE_TEXT_MARKERS = (
    "waiting room",
    "you are now in line",
    "you are in line",
    "your place in line",
    "queue position",
    "position in queue",
    "estimated wait",
    "please wait",
    "ruang tunggu",
    "kamu sedang dalam antrian",
    "kamu berada di antrian",
    "posisi antrian",
    "nomor antrian",
    "mohon tunggu",
    "sedang mengantri",
    "estimasi waktu tunggu",
)

# Patterns to pull a numeric position out of the queue text.
_POSITION_PATTERNS = (
    re.compile(r"(?:position|posisi|nomor)\D{0,20}?([\d.,]{1,12})", re.I),
    re.compile(r"(?:place in line|tempat (?:kamu )?di antrian)\D{0,20}?([\d.,]{1,12})", re.I),
    re.compile(r"([\d.,]{2,12})\s*(?:orang|people|users?)\s*(?:di depan|ahead|in front)", re.I),
    re.compile(r"(?:antrian|queue)\D{0,12}?([\d.,]{2,12})", re.I),
)

# Estimated wait, e.g. "estimated wait: 5 minutes" / "estimasi 10 menit".
_WAIT_PATTERNS = (
    re.compile(r"(\d{1,4})\s*(?:minutes?|mins?|menit)", re.I),
    re.compile(r"(\d{1,4})\s*(?:hours?|jam)", re.I),
)


@dataclass(slots=True)
class QueueStatus:
    """Snapshot of the waiting-room state."""

    in_queue: bool
    position: int | None = None
    estimated_wait_seconds: int | None = None
    detail: str = ""
    url: str = ""


class QueueDetector:
    """Detects a waiting room and parses the current position / wait."""

    async def detect(self, page: Page) -> QueueStatus:
        url = ""
        try:
            url = page.url or ""
        except Exception:  # noqa: BLE001
            url = ""

        # 1) URL-based detection (covers queue-it and tiket.com host redirects),
        #    including any child frames (queue-it commonly runs in an iframe).
        for candidate_url in self._all_urls(page):
            for pat in _QUEUE_URL_PATTERNS:
                if pat.search(candidate_url):
                    text = await self._read_text(page)
                    return QueueStatus(
                        in_queue=True,
                        position=self._parse_position(text),
                        estimated_wait_seconds=self._parse_wait(text),
                        detail=self._snippet(text) or f"queue host: {candidate_url}",
                        url=url,
                    )

        # 2) Text-based detection on the page body.
        text = await self._read_text(page)
        lowered = text.lower()
        marker = next((m for m in _QUEUE_TEXT_MARKERS if m in lowered), None)
        if marker:
            return QueueStatus(
                in_queue=True,
                position=self._parse_position(text),
                estimated_wait_seconds=self._parse_wait(text),
                detail=self._snippet(text, around=marker),
                url=url,
            )

        return QueueStatus(in_queue=False, url=url)

    @staticmethod
    def _all_urls(page: Page) -> list[str]:
        urls: list[str] = []
        try:
            urls.append(page.url or "")
        except Exception:  # noqa: BLE001
            pass
        try:
            for frame in page.frames:
                try:
                    if frame.url:
                        urls.append(frame.url)
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass
        return [u for u in urls if u]

    async def _read_text(self, page: Page) -> str:
        # Read text from the main page and any child frames so queue-it
        # iframes are covered.
        chunks: list[str] = []
        try:
            main = await page.evaluate(
                "() => document.body ? document.body.innerText.slice(0, 4000) : ''"
            )
            if isinstance(main, str):
                chunks.append(main)
        except Exception:  # noqa: BLE001
            pass
        try:
            for frame in page.frames:
                if frame is page.main_frame:
                    continue
                try:
                    t = await frame.evaluate(
                        "() => document.body ? document.body.innerText.slice(0, 4000) : ''"
                    )
                    if isinstance(t, str) and t:
                        chunks.append(t)
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass
        return " \n ".join(chunks)

    @staticmethod
    def _parse_position(text: str) -> int | None:
        if not text:
            return None
        for pat in _POSITION_PATTERNS:
            m = pat.search(text)
            if m:
                digits = re.sub(r"[.,]", "", m.group(1))
                if digits.isdigit():
                    value = int(digits)
                    # Sanity: positions are usually > 0 and < 10 million.
                    if 0 < value < 10_000_000:
                        return value
        return None

    @staticmethod
    def _parse_wait(text: str) -> int | None:
        if not text:
            return None
        total = 0
        matched = False
        for pat in _WAIT_PATTERNS:
            m = pat.search(text)
            if m:
                matched = True
                n = int(m.group(1))
                if "jam" in m.group(0).lower() or "hour" in m.group(0).lower():
                    total += n * 3600
                else:
                    total += n * 60
        return total if matched else None

    @staticmethod
    def _snippet(text: str, *, around: str | None = None, width: int = 200) -> str:
        if not text:
            return ""
        normalized = re.sub(r"\s+", " ", text).strip()
        if around:
            idx = normalized.lower().find(around)
            if idx >= 0:
                start = max(0, idx - 40)
                return normalized[start : start + width]
        return normalized[:width]
