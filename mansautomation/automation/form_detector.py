"""Adaptive form detection using DOM context and fuzzy heuristics."""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from mansautomation.automation.dom_extractor import FieldDescriptor, FormSnapshot
from mansautomation.automation.heuristics import HEURISTICS, FieldHeuristic
from mansautomation.core.models import FieldKey

_NORMALIZE_RE = re.compile(r"[^a-z0-9 ]+")


@dataclass(slots=True)
class DetectedField:
    descriptor: FieldDescriptor
    field_key: FieldKey
    confidence: float
    reasons: list[str]


@dataclass(slots=True)
class DetectionReport:
    snapshot: FormSnapshot
    detected: list[DetectedField]
    unknown: list[FieldDescriptor]


class FormDetectionEngine:
    """Maps each detected DOM field to a canonical :class:`FieldKey`."""

    def __init__(
        self,
        *,
        confidence_threshold: float = 0.55,
        overrides: dict[str, FieldKey] | None = None,
    ) -> None:
        self._threshold = confidence_threshold
        self._overrides = overrides or {}

    def analyze(self, snapshot: FormSnapshot) -> DetectionReport:
        detected: list[DetectedField] = []
        unknown: list[FieldDescriptor] = []
        for field in snapshot.fields:
            classification = self._classify(field)
            if classification is None:
                unknown.append(field)
                continue
            detected.append(classification)
        return DetectionReport(snapshot=snapshot, detected=detected, unknown=unknown)

    def _classify(self, field: FieldDescriptor) -> DetectedField | None:
        # Manual override via id, name, or selector
        for override_key in (field.id, field.name, field.selector):
            if override_key and override_key in self._overrides:
                return DetectedField(
                    descriptor=field,
                    field_key=self._overrides[override_key],
                    confidence=1.0,
                    reasons=["override"],
                )

        signature = _normalize(field.signature_text())
        if not signature:
            return None

        best: tuple[FieldHeuristic, float, list[str]] | None = None
        for heuristic in HEURISTICS:
            score, reasons = self._score(field, signature, heuristic)
            if score <= 0:
                continue
            if best is None or score > best[1]:
                best = (heuristic, score, reasons)

        if best is None or best[1] < self._threshold:
            return None

        heuristic, confidence, reasons = best
        return DetectedField(
            descriptor=field,
            field_key=heuristic.key,
            confidence=min(1.0, confidence),
            reasons=reasons,
        )

    def _score(
        self,
        field: FieldDescriptor,
        signature: str,
        heuristic: FieldHeuristic,
    ) -> tuple[float, list[str]]:
        reasons: list[str] = []
        score = 0.0

        for keyword in heuristic.keywords:
            # Short keywords (<=4 chars) require near-exact matches to avoid
            # false positives like "quantity" -> "city".
            if len(keyword) <= 4:
                if _word_present(keyword, signature):
                    score = max(score, 0.95 * heuristic.weight)
                    reasons.append(f"keyword:{keyword}")
                continue
            ratio = fuzz.partial_ratio(keyword, signature) / 100.0
            if ratio >= 0.9:
                gain = ratio * heuristic.weight
                score = max(score, gain)
                reasons.append(f"keyword:{keyword}")

        for excl in heuristic.excludes:
            if excl in signature:
                score *= 0.4
                reasons.append(f"exclude:{excl}")

        if field.autocomplete:
            ac = field.autocomplete.lower()
            for token in heuristic.autocomplete_tokens:
                if token in ac:
                    score = max(score, 0.95)
                    reasons.append(f"autocomplete:{token}")

        if field.type and heuristic.type_hints:
            if field.type in heuristic.type_hints:
                score = max(score, 0.9)
                reasons.append(f"type:{field.type}")

        return score, reasons


def _normalize(value: str) -> str:
    return _NORMALIZE_RE.sub(" ", value.lower()).strip()


def _word_present(needle: str, signature: str) -> bool:
    """Return True when *needle* appears as a whole token in *signature*."""

    return bool(re.search(rf"\b{re.escape(needle)}\b", signature))
