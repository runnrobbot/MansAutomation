"""Waiting-room / queue detection and position tracking.

High-demand tiket.com events route buyers through a queue-it virtual waiting
room (``queue.tiket.com`` / ``*.queue-it.net``). That page embeds an
authoritative JSON model with exact timing and position data, e.g.::

    "ticket": {
        "secondsToStart": 10405,
        "eventStartTimeUTC": "2026-06-09T05:00:00Z",
        "queueNumber": "calculating...",
        "usersInLineAheadOfYou": "calculating..."
    }

This module reads that embedded data directly instead of scraping the visible
countdown text - so timing is exact and timezone-independent (no off-by-one
minute drift from parsing "02 Hours 56 Minutes" style text). It falls back to
generic text scraping for non-queue-it waiting rooms.

Nothing here bypasses or skips the queue. It only observes the page's own
progression so automation stays in sync and resumes exactly when released.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from playwright.async_api import Page

# URL fragments that indicate a queue / waiting-room host.
_QUEUE_URL_PATTERNS = (
    re.compile(r"queue-it\.net", re.I),
    re.compile(r"queue\.tiket\.com", re.I),
    re.compile(r"queue\.fastly", re.I),
    re.compile(r"waitingroom", re.I),
    re.compile(r"waiting-room", re.I),
    re.compile(r"antrian", re.I),
)

# Visible-text markers that indicate a waiting room (ID + EN).
_QUEUE_TEXT_MARKERS = (
    "waiting room",
    "ticket sales will start soon",
    "you are now in line",
    "you are in line",
    "your place in line",
    "your number in line",
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
    "penjualan akan segera dimulai",
)

# Generic text fallbacks (used only when queue-it JSON is unavailable).
_POSITION_PATTERNS = (
    re.compile(r"(?:number in line|posisi antrian|nomor antrian)\D{0,20}?([\d.,]{1,12})", re.I),
    re.compile(r"(?:ahead of you|di depan(?: kamu)?)\D{0,20}?([\d.,]{1,12})", re.I),
    re.compile(r"([\d.,]{2,12})\s*(?:orang|people|users?)\s*(?:di depan|ahead|in front)", re.I),
)


@dataclass(slots=True)
class QueueStatus:
    """Snapshot of the waiting-room state.

    Phases:
      - ``before``  : queue-it pre-queue page, counting down to sale start.
      - ``queue``   : in the live queue, holding a position.
      - ``unknown`` : a waiting room of some kind we couldn't classify.
    """

    in_queue: bool
    phase: str = "unknown"  # 'before' | 'queue' | 'unknown'
    position: int | None = None
    users_ahead: int | None = None
    seconds_to_start: int | None = None
    event_start_utc: str | None = None
    estimated_wait_seconds: int | None = None
    is_queueit: bool = False
    detail: str = ""
    url: str = ""

    def seconds_until_start_now(self) -> int | None:
        """Exact seconds until sale start, recomputed against the current
        clock from the absolute UTC timestamp (most accurate)."""

        if self.event_start_utc:
            try:
                target = datetime.fromisoformat(
                    self.event_start_utc.replace("Z", "+00:00")
                )
                now = datetime.now(tz=timezone.utc)
                return max(0, int((target - now).total_seconds()))
            except Exception:  # noqa: BLE001
                pass
        return self.seconds_to_start


# JS that extracts queue-it's embedded model from the live page. Reads the
# knockout view model first, then falls back to regex over the page HTML.
_QUEUEIT_EXTRACT_JS = r"""() => {
    const out = { isQueueit: false };
    const html = document.documentElement ? document.documentElement.innerHTML : '';

    const looksQueueit =
        !!document.getElementById('queue-it_log') ||
        !!window.queueViewModel ||
        /queue-it\.net|queue\.tiket\.com/i.test(html.slice(0, 8000)) ||
        (document.body && /\b(before|queue)\b/.test(document.body.className || ''));
    if (!looksQueueit) return out;
    out.isQueueit = true;

    const cls = (document.body && document.body.className) || '';
    if (/\bqueue\b/.test(cls) && !/\bbefore\b/.test(cls)) out.phase = 'queue';
    else if (/\bbefore\b/.test(cls)) out.phase = 'before';
    else out.phase = 'unknown';

    const read = (f) => {
        try { return (typeof f === 'function') ? f() : f; } catch (e) { return null; }
    };

    // 1) Live knockout view model (authoritative, updates every 30s).
    try {
        const vm = window.queueViewModel;
        if (vm && vm.ticket) {
            const t = vm.ticket;
            out.queueNumber = read(t.queueNumber);
            out.usersAhead = read(t.usersInLineAheadOfYou);
            out.secondsToStart = read(t.secondsToStart);
            out.eventStartUTC = read(t.eventStartTimeUTC);
        }
    } catch (e) {}

    // 2) Regex fallback over the embedded inqueueInfo JSON.
    if (out.eventStartUTC == null) {
        const m = html.match(/"eventStartTimeUTC"\s*:\s*"([^"]+)"/);
        if (m) out.eventStartUTC = m[1];
    }
    if (out.secondsToStart == null) {
        const s = html.match(/"secondsToStart"\s*:\s*(\d+)/);
        if (s) out.secondsToStart = parseInt(s[1], 10);
    }
    if (out.queueNumber == null) {
        const q = html.match(/"queueNumber"\s*:\s*"?([^",}]+)"?/);
        if (q) out.queueNumber = q[1];
    }
    if (out.usersAhead == null) {
        const u = html.match(/"usersInLineAheadOfYou"\s*:\s*"?([^",}]+)"?/);
        if (u) out.usersAhead = u[1];
    }
    return out;
}"""


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return int(value)
        s = re.sub(r"[.,]", "", str(value))
        if s.isdigit():
            return int(s)
    except Exception:  # noqa: BLE001
        return None
    return None


class QueueDetector:
    """Detects a waiting room (queue-it first, then generic) and reads its
    position / timing data."""

    async def detect(self, page: Page) -> QueueStatus:
        url = ""
        try:
            url = page.url or ""
        except Exception:  # noqa: BLE001
            url = ""

        # 1) queue-it (or its iframe) - read the authoritative embedded model.
        qi = await self._extract_queueit(page)
        if qi and qi.get("isQueueit"):
            phase = str(qi.get("phase") or "unknown")
            return QueueStatus(
                in_queue=True,
                phase=phase,
                position=_to_int(qi.get("queueNumber")),
                users_ahead=_to_int(qi.get("usersAhead")),
                seconds_to_start=_to_int(qi.get("secondsToStart")),
                event_start_utc=(str(qi["eventStartUTC"]) if qi.get("eventStartUTC") else None),
                is_queueit=True,
                detail=(
                    "queue-it pre-queue (sale not started)"
                    if phase == "before"
                    else "queue-it waiting room"
                ),
                url=url,
            )

        # 2) URL-based detection for other queue hosts.
        for candidate_url in self._all_urls(page):
            for pat in _QUEUE_URL_PATTERNS:
                if pat.search(candidate_url):
                    text = await self._read_text(page)
                    return QueueStatus(
                        in_queue=True,
                        phase="queue",
                        position=self._parse_position(text),
                        detail=self._snippet(text) or f"queue host: {candidate_url}",
                        url=url,
                    )

        # 3) Generic text-based detection.
        text = await self._read_text(page)
        lowered = text.lower()
        marker = next((m for m in _QUEUE_TEXT_MARKERS if m in lowered), None)
        if marker:
            return QueueStatus(
                in_queue=True,
                phase="queue",
                position=self._parse_position(text),
                detail=self._snippet(text, around=marker),
                url=url,
            )

        return QueueStatus(in_queue=False, phase="unknown", url=url)

    async def _extract_queueit(self, page: Page) -> dict | None:
        # Try the main frame, then any child frame (queue-it can be iframed).
        targets = [page.main_frame]
        try:
            targets += [f for f in page.frames if f is not page.main_frame]
        except Exception:  # noqa: BLE001
            pass
        for target in targets:
            try:
                data = await target.evaluate(_QUEUEIT_EXTRACT_JS)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(data, dict) and data.get("isQueueit"):
                return data
        return None

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
                    if 0 < value < 10_000_000:
                        return value
        return None

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
