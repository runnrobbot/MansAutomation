"""Generic checkout plugin.

This plugin demonstrates a real, production-grade workflow:

1. Wait for the page to settle through SPA / lazy-loaded frameworks.
2. Run the adaptive form detector.
3. Use the autofill engine to populate every recognised field.
4. Optionally advance to the next step (next button, accept terms, submit).
5. Pause for human intervention when CAPTCHA or queue-it style screens appear.

The plugin never bypasses CAPTCHA. It defers to the runner's human-intervention
pipeline whenever a manual step is needed.
"""

from __future__ import annotations

import asyncio
import re

from playwright.async_api import Locator, Page

from mansautomation.core.exceptions import HumanInterventionRequired
from mansautomation.core.models import WorkflowEventLevel
from mansautomation.plugins.base import (
    AutomationPlugin,
    PluginContext,
    PluginExecutionResult,
    PluginMetadata,
)

_NEXT_BUTTON_PATTERNS: tuple[str, ...] = (
    "continue",
    "next",
    "proceed",
    "checkout",
    "submit",
    "place order",
    "complete order",
    "agree",
    "i agree",
)

_TERMS_PATTERNS: tuple[str, ...] = (
    "i agree",
    "accept",
    "terms",
    "privacy",
    "consent",
)


class GenericCheckoutPlugin(AutomationPlugin):
    metadata = PluginMetadata(
        id="generic.checkout",
        name="Generic Checkout",
        version="1.0.0",
        description="Adaptive autofill workflow for generic checkout, registration and ticket pages.",
        author="MansAutomation",
        target_domains=(),
        capabilities=(
            "autofill",
            "form-detection",
            "human-intervention",
            "spa",
            "react",
            "vue",
            "next.js",
        ),
    )

    async def execute(self, context: PluginContext) -> PluginExecutionResult:
        page = context.page
        if page is None:
            return PluginExecutionResult(success=False, message="page was not initialised")

        await context.emit_event("waiting for page to stabilise")
        await self._wait_until_ready(page)

        detection = context.form_detector.analyze(await context.dom_extractor.extract(page))
        if not detection.detected:
            await context.emit_event(
                "no fillable fields detected", level=WorkflowEventLevel.WARN,
            )
        else:
            await context.emit_event(
                f"detected {len(detection.detected)} fields",
                context={"fields": [d.field_key.value for d in detection.detected]},
            )

        autofill_result = await context.autofill.fill_report(
            page,
            detection,
            context.profile,
            attendees=context.profile.attendees or None,
        )
        await context.emit_event(
            "autofill complete",
            context=autofill_result.summary(),
        )

        await self._accept_terms(page, context)
        await self._click_next(page, context)

        # Wait briefly to capture the next page's state and intervene if needed
        await asyncio.sleep(1.0)
        await self._raise_if_human_required(page, context)

        return PluginExecutionResult(
            success=True,
            message="checkout step completed",
            data={
                "url": page.url,
                "title": await page.title(),
            },
            autofill=autofill_result.summary(),
        )

    async def _wait_until_ready(self, page: Page) -> None:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except Exception:  # noqa: BLE001
            return
        try:
            await page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:  # noqa: BLE001
            pass
        # Give SPA frameworks a moment to settle their state
        await asyncio.sleep(0.4)

    async def _accept_terms(self, page: Page, context: PluginContext) -> None:
        try:
            checkboxes = page.locator("input[type='checkbox']")
            count = await checkboxes.count()
        except Exception:  # noqa: BLE001
            return
        for i in range(count):
            checkbox = checkboxes.nth(i)
            if not await self._is_terms_checkbox(checkbox):
                continue
            try:
                checked = await checkbox.is_checked()
            except Exception:  # noqa: BLE001
                continue
            if checked:
                continue
            try:
                await checkbox.check()
                await context.emit_event(
                    "accepted terms / consent checkbox", level=WorkflowEventLevel.DEBUG,
                )
            except Exception:  # noqa: BLE001
                continue

    async def _is_terms_checkbox(self, checkbox: Locator) -> bool:
        try:
            label = (await checkbox.evaluate(
                """el => {
                    if (el.labels && el.labels.length) return el.labels[0].innerText || '';
                    if (el.id) {
                        const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                        if (lbl) return lbl.innerText || '';
                    }
                    const parent = el.parentElement;
                    return parent ? (parent.innerText || '') : '';
                }"""
            )) or ""
        except Exception:  # noqa: BLE001
            return False
        normalized = label.lower()
        return any(token in normalized for token in _TERMS_PATTERNS)

    async def _click_next(self, page: Page, context: PluginContext) -> None:
        try:
            buttons = page.locator(
                "button, input[type='submit'], a[role='button'], [role='button']"
            )
            count = await buttons.count()
        except Exception:  # noqa: BLE001
            return
        for i in range(count):
            button = buttons.nth(i)
            try:
                visible = await button.is_visible()
                enabled = await button.is_enabled()
            except Exception:  # noqa: BLE001
                continue
            if not (visible and enabled):
                continue
            try:
                text = (await button.inner_text()).strip().lower()
            except Exception:  # noqa: BLE001
                text = ""
            if not text:
                try:
                    text = (await button.get_attribute("aria-label") or "").lower()
                except Exception:  # noqa: BLE001
                    text = ""
            if not any(self._matches_word(text, pattern) for pattern in _NEXT_BUTTON_PATTERNS):
                continue
            try:
                await button.click()
                await context.emit_event(
                    "clicked progression button",
                    context={"text": text},
                )
                return
            except Exception:  # noqa: BLE001
                continue

    @staticmethod
    def _matches_word(haystack: str, needle: str) -> bool:
        return re.search(rf"\b{re.escape(needle)}\b", haystack) is not None

    async def _raise_if_human_required(self, page: Page, context: PluginContext) -> None:
        from mansautomation.automation.human_intervention import HumanInterventionDetector

        detector = HumanInterventionDetector()
        signal = await detector.detect(page)
        if signal is None:
            return
        await context.request_human(signal.detail, url=signal.url)
        # After the user resumes we simply continue - the runner clears the human event.
        if context.is_aborted():
            raise HumanInterventionRequired(signal.reason, url=signal.url)
