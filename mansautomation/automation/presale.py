"""Pre-sale countdown / sale-not-yet-open detection.

When a tiket.com event has not started selling yet the page shows one (or
both) of:

    "Tiket tersedia mulai 28 Mei 2026 13:00 WIB"
    "Beli tiket sekarang" -> "Penjualan dimulai dalam 02:13:45"

This module exposes :class:`SaleStatusDetector` that inspects the live page
and returns a :class:`SaleStatus` describing whether the sale is open, the
remaining seconds, and an optional ISO 8601 start timestamp parsed from the
page text. The plugin uses this to:

    * notify the operator when the sale is not yet open
    * optionally wait until the countdown elapses (small budget) and retry
    * surface the human-readable detail in the workflow event log

Nothing here bypasses queues or anti-bot mechanisms; it is purely descriptive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from playwright.async_api import Page

# 1) Plain countdown timer "HH:MM:SS" or "MM:SS"
_COUNTDOWN_HMS_RE = re.compile(r"\b(\d{1,2}):(\d{2}):(\d{2})\b")
_COUNTDOWN_MS_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")

# 2) "X jam Y menit Z detik" / "X hours Y minutes Z seconds"
_HUMAN_DURATION_RE = re.compile(
    r"(?:(\d{1,3})\s*(?:hari|day|days)\s*)?"
    r"(?:(\d{1,3})\s*(?:jam|hour|hours|hr|hrs)\s*)?"
    r"(?:(\d{1,3})\s*(?:menit|min|minute|minutes|mins)\s*)?"
    r"(?:(\d{1,3})\s*(?:detik|sec|second|seconds|s))?",
    re.IGNORECASE,
)

# 3) Indonesian month names -> month number
_ID_MONTHS = {
    "januari": 1, "februari": 2, "maret": 3, "april": 4, "mei": 5, "juni": 6,
    "juli": 7, "agustus": 8, "september": 9, "oktober": 10, "november": 11, "desember": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "agt": 8,
    "sep": 9, "okt": 10, "nov": 11, "des": 12,
}

# 4) "28 Mei 2026 13:00" / "May 28, 2026 1:00 PM"
_DATE_TIME_ID_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})[,\s]+(\d{1,2})[.:](\d{2})",
    re.IGNORECASE,
)

# 4b) "09 Juni 2026 (05:00)" - tiket.com renders the time in parentheses.
_DATE_TIME_PAREN_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\s*\(\s*(\d{1,2})[.:](\d{2})\s*\)",
    re.IGNORECASE,
)

_NOT_YET_KEYWORDS = (
    "penjualan dimulai",
    "tiket tersedia mulai",
    "tiket akan tersedia",
    "tickets available from",
    "sale starts",
    "sale opens",
    "presale starts",
    "akan dijual",
    "akan tersedia",
    "belum tersedia",
    "coming soon",
    "segera hadir",
)


@dataclass(slots=True)
class SaleStatus:
    """Snapshot of the event sale state read from the live page."""

    is_open: bool
    detail: str = ""
    seconds_until_open: int | None = None
    starts_at: str | None = None  # ISO 8601 if a date+time was parseable
    snippet: str = ""


class SaleStatusDetector:
    """Inspects the visible page text for a pre-sale countdown / not-open state."""

    async def detect(self, page: Page) -> SaleStatus:
        try:
            text = await page.evaluate(
                """() => {
                    if (!document.body) return '';
                    return document.body.innerText.slice(0, 6000);
                }"""
            )
        except Exception:  # noqa: BLE001
            return SaleStatus(is_open=True)
        if not isinstance(text, str) or not text:
            return SaleStatus(is_open=True)
        normalized = text.lower()
        keyword_hit = next(
            (k for k in _NOT_YET_KEYWORDS if k in normalized),
            None,
        )
        if not keyword_hit:
            return SaleStatus(is_open=True, snippet=text[:240])

        # Walk the surrounding text once we found a hint phrase.
        snippet = self._extract_snippet(text, keyword_hit)
        seconds, starts_at, detail = self._parse_timing(snippet)
        return SaleStatus(
            is_open=False,
            detail=detail or snippet[:200],
            seconds_until_open=seconds,
            starts_at=starts_at,
            snippet=snippet[:240],
        )

    @staticmethod
    def _extract_snippet(text: str, keyword: str) -> str:
        idx = text.lower().find(keyword)
        if idx < 0:
            return text[:400]
        start = max(0, idx - 80)
        end = min(len(text), idx + 400)
        return text[start:end]

    @staticmethod
    def _parse_timing(snippet: str) -> tuple[int | None, str | None, str]:
        # Prefer the absolute date+time when present - it is exact and not
        # affected by a client-side countdown that may lag.
        abs_seconds, abs_iso, abs_detail = SaleStatusDetector._parse_absolute(snippet)

        # Try a "HH:MM:SS" countdown (e.g. "Beli tiket dalam 19:55:00").
        # The live countdown is authoritative and timezone-independent, so it
        # wins over the absolute date for the actual wait duration.
        match = _COUNTDOWN_HMS_RE.search(snippet)
        if match:
            h, m, s = (int(x) for x in match.groups())
            seconds = h * 3600 + m * 60 + s
            return (
                seconds,
                abs_iso,
                f"countdown {match.group(0)}" + (f" / {abs_detail}" if abs_detail else ""),
            )

        match = _COUNTDOWN_MS_RE.search(snippet)
        if match:
            m, s = (int(x) for x in match.groups())
            preceding = snippet.lower()[: match.start()]
            if any(v in preceding for v in ("dalam", "in", "sisa", "remaining")):
                seconds = m * 60 + s
                return (seconds, abs_iso, f"countdown {match.group(0)}")

        # Human-friendly duration "X jam Y menit Z detik".
        for h_match in _HUMAN_DURATION_RE.finditer(snippet):
            d, h, m, s = h_match.groups()
            if not any((d, h, m, s)):
                continue
            seconds = (
                (int(d or 0) * 86_400)
                + (int(h or 0) * 3_600)
                + (int(m or 0) * 60)
                + int(s or 0)
            )
            if seconds > 0:
                return seconds, abs_iso, f"duration {h_match.group(0).strip()}"

        # Fall back to the absolute date+time.
        if abs_seconds is not None:
            return abs_seconds, abs_iso, abs_detail
        return None, None, snippet[:200]

    @staticmethod
    def _parse_absolute(snippet: str) -> tuple[int | None, str | None, str]:
        """Parse an absolute sale-open datetime from the snippet.

        Handles both "28 Mei 2026 13:00" and "09 Juni 2026 (05:00)".
        Returns (seconds_until_open, iso_timestamp, detail).
        """

        match = _DATE_TIME_PAREN_RE.search(snippet) or _DATE_TIME_ID_RE.search(snippet)
        if not match:
            return None, None, ""
        day_s, month_s, year_s, hour_s, minute_s = match.groups()
        month = _ID_MONTHS.get(month_s.lower()) or _MONTHS_EN.get(month_s.lower())
        if month is None:
            return None, None, ""
        try:
            starts_local = datetime(
                int(year_s), month, int(day_s), int(hour_s), int(minute_s)
            )
        except ValueError:
            return None, None, ""
        now = datetime.now()
        seconds = max(0, int((starts_local - now).total_seconds()))
        starts_at_iso = starts_local.replace(tzinfo=timezone.utc).isoformat()
        return seconds, starts_at_iso, f"opens at {match.group(0).strip()}"


_MONTHS_EN: dict[str, int] = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
