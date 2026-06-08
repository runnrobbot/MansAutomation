"""High-performance autofill engine driven by the form detector."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from playwright.async_api import ElementHandle, Frame, Page

from mansautomation.automation.dom_extractor import DomExtractor, FieldDescriptor
from mansautomation.automation.form_detector import (
    DetectedField,
    DetectionReport,
    FormDetectionEngine,
)
from mansautomation.automation.datetime_utils import normalize_iso_date
from mansautomation.core.config import WorkflowSettings
from mansautomation.core.exceptions import FormDetectionError
from mansautomation.core.models import Attendee, FieldKey, Profile
from mansautomation.services.logging_service import LoggingService


@dataclass(slots=True)
class AutofillResult:
    filled: list[str]
    skipped: list[str]
    failed: list[str]

    def summary(self) -> dict[str, Any]:
        return {
            "filled": len(self.filled),
            "skipped": len(self.skipped),
            "failed": len(self.failed),
            "details": {
                "filled": self.filled,
                "skipped": self.skipped,
                "failed": self.failed,
            },
        }


class AutofillEngine:
    """Fills detected fields with values from a :class:`Profile`."""

    def __init__(
        self,
        detector: FormDetectionEngine,
        extractor: DomExtractor,
        settings: WorkflowSettings,
        logging_service: LoggingService,
    ) -> None:
        self._detector = detector
        self._extractor = extractor
        self._settings = settings
        self._logger = logging_service.get_logger("autofill")

    def apply_settings(self, settings: WorkflowSettings) -> None:
        self._settings = settings

    async def autofill(
        self,
        page: Page,
        profile: Profile,
        *,
        overrides: dict[FieldKey, str] | None = None,
        attendees: list[Attendee] | None = None,
    ) -> AutofillResult:
        snapshot = await self._extractor.extract(page)
        if not snapshot.fields:
            raise FormDetectionError("no fillable fields were detected on the page")
        report = self._detector.analyze(snapshot)
        return await self.fill_report(page, report, profile, overrides=overrides, attendees=attendees)

    async def fill_report(
        self,
        page: Page,
        report: DetectionReport,
        profile: Profile,
        *,
        overrides: dict[FieldKey, str] | None = None,
        attendees: list[Attendee] | None = None,
    ) -> AutofillResult:
        attendees_pool: list[Attendee] = list(attendees or profile.attendees)
        used_attendees: dict[FieldKey, int] = {}
        result = AutofillResult(filled=[], skipped=[], failed=[])
        used_overrides = overrides or {}

        for entry in report.detected:
            value = self._resolve_value(
                entry,
                profile,
                used_overrides,
                attendees_pool,
                used_attendees,
            )
            if value is None:
                result.skipped.append(self._describe(entry))
                continue
            try:
                ok = await self._apply_value(page, entry.descriptor, entry.field_key, value)
            except Exception as exc:  # noqa: BLE001
                self._logger.warning(
                    "autofill_failed",
                    field=entry.field_key.value,
                    selector=entry.descriptor.selector,
                    error=str(exc),
                )
                result.failed.append(self._describe(entry))
                continue
            if ok:
                result.filled.append(self._describe(entry))
            else:
                result.failed.append(self._describe(entry))
            await asyncio.sleep(self._settings.inter_field_delay_ms / 1000)
        return result

    def _describe(self, detected: DetectedField) -> str:
        return f"{detected.field_key.value}::{detected.descriptor.selector}"

    def _resolve_value(
        self,
        detected: DetectedField,
        profile: Profile,
        overrides: dict[FieldKey, str],
        attendees: list[Attendee],
        used: dict[FieldKey, int],
    ) -> str | None:
        if detected.field_key in overrides:
            return overrides[detected.field_key]
        if detected.descriptor.is_radio or detected.descriptor.is_checkbox:
            return profile.value_for(detected.field_key)
        primary = profile.value_for(detected.field_key)
        if primary:
            return primary
        # Email field on a login form should fall back to the login email.
        if detected.field_key == FieldKey.EMAIL and profile.login.email:
            return profile.login.email
        # Allow attendee-aware fallback for events that prompt for ticket holders
        if attendees and detected.field_key in {FieldKey.FULL_NAME, FieldKey.EMAIL, FieldKey.PHONE,
                                                 FieldKey.DATE_OF_BIRTH, FieldKey.GENDER, FieldKey.ID_NUMBER}:
            index = used.get(detected.field_key, 0)
            if index < len(attendees):
                attendee = attendees[index]
                used[detected.field_key] = index + 1
                if detected.field_key == FieldKey.FULL_NAME:
                    return attendee.full_name
                if detected.field_key == FieldKey.EMAIL and attendee.email:
                    return str(attendee.email)
                if detected.field_key == FieldKey.PHONE and attendee.phone:
                    return attendee.phone
                if detected.field_key == FieldKey.DATE_OF_BIRTH:
                    return attendee.date_of_birth
                if detected.field_key == FieldKey.GENDER:
                    return attendee.gender
                if detected.field_key == FieldKey.ID_NUMBER:
                    return attendee.id_number
        # Fallback to custom fields, accessed by the heuristic key value
        return profile.custom_fields.get(detected.field_key.value)

    async def _apply_value(
        self,
        page: Page,
        descriptor: FieldDescriptor,
        key: FieldKey,
        value: str,
    ) -> bool:
        frame = self._frame_for(page, descriptor)
        locator = frame.locator(descriptor.selector).first
        try:
            await locator.wait_for(state="attached", timeout=4000)
            await locator.scroll_into_view_if_needed(timeout=1500)
        except Exception:  # noqa: BLE001
            return False

        if descriptor.is_select:
            return await self._fill_select(locator, descriptor, value)
        if descriptor.is_checkbox:
            return await self._toggle_checkbox(locator, value)
        if descriptor.is_radio:
            return await self._select_radio(frame, descriptor, value)
        if descriptor.is_contenteditable:
            return await self._fill_contenteditable(locator, value)
        return await self._fill_text(locator, descriptor, key, value)

    def _frame_for(self, page: Page, descriptor: FieldDescriptor) -> Frame:
        if not descriptor.frame_url or descriptor.frame_url == page.url:
            return page.main_frame
        for frame in page.frames:
            if frame.url == descriptor.frame_url:
                return frame
        return page.main_frame

    async def _fill_text(
        self,
        locator: Any,
        descriptor: FieldDescriptor,
        key: FieldKey,
        value: str,
    ) -> bool:
        normalized = self._normalize_value(descriptor, key, value)
        if descriptor.max_length and descriptor.max_length > 0:
            normalized = normalized[: descriptor.max_length]
        try:
            await locator.fill("")
        except Exception:  # noqa: BLE001
            pass
        try:
            await locator.click(timeout=2000)
        except Exception:  # noqa: BLE001
            pass
        try:
            delay = max(0, self._settings.field_typing_delay_ms)
            await locator.type(normalized, delay=delay)
            try:
                await locator.dispatch_event("input")
                await locator.dispatch_event("change")
                await locator.dispatch_event("blur")
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception:  # noqa: BLE001
            try:
                await locator.fill(normalized)
                return True
            except Exception:  # noqa: BLE001
                return False

    async def _fill_contenteditable(self, locator: Any, value: str) -> bool:
        try:
            await locator.click()
            await locator.evaluate("(el, v) => { el.textContent = v; }", value)
            await locator.dispatch_event("input")
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _fill_select(self, locator: Any, descriptor: FieldDescriptor, value: str) -> bool:
        try:
            await locator.select_option(value=value)
            return True
        except Exception:  # noqa: BLE001
            pass
        try:
            await locator.select_option(label=value)
            return True
        except Exception:  # noqa: BLE001
            pass
        if descriptor.options:
            from rapidfuzz import process

            choices = {opt["value"]: opt["label"] for opt in descriptor.options}
            match = process.extractOne(value, list(choices.values()), score_cutoff=70)
            if match:
                label = match[0]
                option_value = next((v for v, lbl in choices.items() if lbl == label), None)
                if option_value:
                    try:
                        await locator.select_option(value=option_value)
                        return True
                    except Exception:  # noqa: BLE001
                        return False
        return False

    async def _toggle_checkbox(self, locator: Any, value: str) -> bool:
        truthy = value.strip().lower() in {"1", "true", "yes", "y", "on", "checked"}
        try:
            handle: ElementHandle | None = await locator.element_handle()
            current = bool(await handle.get_property("checked")) if handle else False
            if truthy and not current:
                await locator.check()
            elif not truthy and current:
                await locator.uncheck()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _select_radio(self, frame: Frame, descriptor: FieldDescriptor, value: str) -> bool:
        # Prefer to find the radio in the same name group whose label/value matches
        if descriptor.name:
            try:
                candidates = frame.locator(f'input[type="radio"][name="{descriptor.name}"]')
                count = await candidates.count()
                target_lower = value.strip().lower()
                for index in range(count):
                    candidate = candidates.nth(index)
                    try:
                        candidate_value = (await candidate.get_attribute("value")) or ""
                        candidate_id = (await candidate.get_attribute("id")) or ""
                    except Exception:  # noqa: BLE001
                        continue
                    if candidate_value.lower() == target_lower:
                        await candidate.check()
                        return True
                    if candidate_id:
                        label = frame.locator(f'label[for="{candidate_id}"]').first
                        try:
                            label_text = (await label.inner_text()).strip().lower()
                        except Exception:  # noqa: BLE001
                            label_text = ""
                        if label_text and target_lower in label_text:
                            await candidate.check()
                            return True
            except Exception:  # noqa: BLE001
                pass
        try:
            locator = frame.locator(descriptor.selector).first
            await locator.check()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _normalize_value(self, descriptor: FieldDescriptor, key: FieldKey, value: str) -> str:
        if key == FieldKey.PHONE:
            return re.sub(r"[^\d+]", "", value)
        if key == FieldKey.POSTAL_CODE:
            return value.strip()
        if key == FieldKey.DATE_OF_BIRTH and descriptor.type == "date":
            return normalize_iso_date(value)
        return value
