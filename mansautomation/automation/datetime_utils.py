"""Date helpers shared by the autofill engine."""

from __future__ import annotations

from datetime import datetime


_INPUT_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%Y/%m/%d",
)


def normalize_iso_date(value: str) -> str:
    """Best-effort conversion of common date strings to ISO 8601 (YYYY-MM-DD)."""

    candidate = value.strip()
    if not candidate:
        return ""
    for fmt in _INPUT_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return candidate
