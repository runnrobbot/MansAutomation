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
    expected_service: str | None = None
    serviced_soon: bool = False
    redirect_prompt: bool = False
    connection_lost: bool = False
    queue_paused: bool = False
    challenge: bool = False
    is_queueit: bool = False
    # True when the reading came from a frame carrying the authoritative queue
    # state (knockout view model, before/queue body class, or the position
    # spans). False means a degraded read - e.g. only a queue-it panel iframe
    # responded because the main frame was momentarily unresponsive. Callers
    # must NOT treat a degraded read as a phase change.
    has_state: bool = False
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
# rendered DOM spans first (most reliable on the live queue page), then the
# knockout view model, then a regex over the embedded JSON.
_QUEUEIT_EXTRACT_JS = r"""() => {
    const out = { isQueueit: false };
    const html = document.documentElement ? document.documentElement.innerHTML : '';

    const looksQueueit =
        !!document.getElementById('queue-it_log') ||
        !!window.queueViewModel ||
        !!document.getElementById('MainPart_lbQueueNumber') ||
        /queue-it\.net|queue\.tiket\.com/i.test(html.slice(0, 12000)) ||
        (document.body && /\b(before|queue)\b/.test(document.body.className || ''));
    if (!looksQueueit) return out;
    out.isQueueit = true;

    const cls = (document.body && document.body.className) || '';
    if (/\bqueue\b/.test(cls) && !/\bbefore\b/.test(cls)) out.phase = 'queue';
    else if (/\bbefore\b/.test(cls)) out.phase = 'before';
    else out.phase = 'unknown';

    // Whether THIS frame carries the authoritative queue state. A queue-it
    // panel iframe matches looksQueueit (it references queue-it.net) but has
    // none of these, so it reports hasState=false and callers can ignore it.
    out.hasState = !!window.queueViewModel
        || /\b(before|queue)\b/.test(cls)
        || !!document.getElementById('MainPart_lbQueueNumber');

    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
    const txt = (id) => {
        const el = document.getElementById(id);
        return el ? norm(el.innerText) : null;
    };

    // 1) Rendered DOM spans (knockout has already bound the live values).
    out.queueNumber = txt('MainPart_lbQueueNumber');
    out.usersAhead = txt('MainPart_lbUsersInLineAheadOfYou');
    out.expectedService = txt('MainPart_lbExpectedServiceTime');
    const countdownTxt = txt('defaultCountdown');
    if (countdownTxt) out.countdownText = countdownTxt;

    // 2) Live knockout view model (authoritative, updates every cycle).
    const read = (f) => { try { return (typeof f === 'function') ? f() : f; } catch (e) { return null; } };
    try {
        const vm = window.queueViewModel;
        if (vm && vm.ticket) {
            const t = vm.ticket;
            out.queueNumber = out.queueNumber || read(t.queueNumber);
            out.usersAhead = out.usersAhead || read(t.usersInLineAheadOfYou);
            out.secondsToStart = read(t.secondsToStart);
            out.eventStartUTC = read(t.eventStartTimeUTC);
            out.expectedService = out.expectedService || read(t.expectedServiceTime);
        }
    } catch (e) {}

    // 3) Regex fallback over the embedded inqueueInfo JSON.
    const grab = (key) => {
        const m = html.match(new RegExp('"' + key + '"\\s*:\\s*"?([^",}]+)"?'));
        return m ? m[1] : null;
    };
    if (out.eventStartUTC == null) out.eventStartUTC = grab('eventStartTimeUTC');
    if (out.secondsToStart == null) {
        const s = html.match(/"secondsToStart"\s*:\s*(\d+)/);
        if (s) out.secondsToStart = parseInt(s[1], 10);
    }
    if (!out.queueNumber || /calculating/i.test(String(out.queueNumber))) {
        out.queueNumber = grab('queueNumber') || out.queueNumber;
    }
    if (!out.usersAhead || /calculating/i.test(String(out.usersAhead))) {
        out.usersAhead = grab('usersInLineAheadOfYou') || out.usersAhead;
    }

    // 4) Visibility helper - handles display:none, zero-size, and knockout
    //    toggles. queue-it pre-renders all state elements and only shows the
    //    relevant ones, so we must check actual visibility, not presence.
    const isShown = (el) => {
        if (!el) return false;
        const st = window.getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden') return false;
        const r = el.getBoundingClientRect();
        return r.width > 1 && r.height > 1;
    };

    // 5) "It is your turn" / redirect signals.
    out.firstInLine = isShown(document.getElementById('first-in-line'));
    out.servicedSoon = isShown(document.getElementById('serviced-soon'));

    // 6) Redirect-confirm dialog: some events require an explicit click on
    //    "Yes, please" to proceed to the site when your turn arrives. Surface
    //    it so the caller can click the legitimate proceed button (no bypass).
    out.redirectPrompt = isShown(document.getElementById('divConfirmRedirectModal'));

    // 7) Connection lost / queue paused (informational guards).
    out.connectionLost = isShown(document.getElementById('MainPart_lbManualUpdateWarning'));
    out.queuePaused = isShown(document.getElementById('queue-paused'));

    // 8) Interactive CAPTCHA challenge that genuinely needs a human. queue-it
    //    solves its ProofOfWork challenge itself; only image/checkbox
    //    challenges (reCaptcha bframe / hCaptcha) block and need a person.
    out.challenge = false;
    const cSel = [
        'iframe[src*="recaptcha/api2/bframe"]',
        'iframe[src*="hcaptcha.com"][src*="frame=challenge"]',
    ];
    for (const s of cSel) {
        const el = document.querySelector(s);
        if (isShown(el)) {
            const r = el.getBoundingClientRect();
            if (r.width > 100 && r.height > 100) { out.challenge = true; break; }
        }
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
                expected_service=(str(qi["expectedService"]) if qi.get("expectedService") else None),
                serviced_soon=bool(qi.get("servicedSoon") or qi.get("firstInLine")),
                redirect_prompt=bool(qi.get("redirectPrompt")),
                connection_lost=bool(qi.get("connectionLost")),
                queue_paused=bool(qi.get("queuePaused")),
                challenge=bool(qi.get("challenge")),
                is_queueit=True,
                has_state=bool(qi.get("hasState")),
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
        """Read queue-it's embedded model, preferring the frame that actually
        carries the queue state.

        The main frame is authoritative in the healthy case. If it is
        momentarily unresponsive (page busy / self-refresh / renderer hiccup),
        ``evaluate`` raises and we fall back to child frames - but a queue-it
        *panel* iframe only references queue-it without holding the real state
        (``hasState`` is false). We must return that degraded result only when
        no state-bearing frame is available, so the caller can tell a genuine
        phase change apart from a transient read failure.
        """

        # 1) Main frame first - the common, healthy path (single evaluate).
        main_data: dict | None = None
        try:
            data = await page.main_frame.evaluate(_QUEUEIT_EXTRACT_JS)
            if isinstance(data, dict) and data.get("isQueueit"):
                main_data = data
                if data.get("hasState"):
                    return data
        except Exception:  # noqa: BLE001
            main_data = None

        # 2) Main frame dead/degraded - scan child frames, preferring one that
        #    carries real state; otherwise keep the degraded fallback.
        fallback = main_data
        try:
            frames = [f for f in page.frames if f is not page.main_frame]
        except Exception:  # noqa: BLE001
            frames = []
        for frame in frames:
            try:
                data = await frame.evaluate(_QUEUEIT_EXTRACT_JS)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(data, dict) and data.get("isQueueit"):
                if data.get("hasState"):
                    return data
                fallback = fallback or data
        return fallback

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
