"""Coordinator that propagates AppSettings changes to live services."""

from __future__ import annotations

from mansautomation.automation.autofill_engine import AutofillEngine
from mansautomation.automation.runner import WorkflowRunner
from mansautomation.core.config import SETTINGS_CHANGED_TOPIC, AppSettings
from mansautomation.core.events import EventBus
from mansautomation.notifications.dispatcher import NotificationDispatcher
from mansautomation.services.browser_service import BrowserService
from mansautomation.services.logging_service import LoggingService


class SettingsApplier:
    """Subscribes to settings updates and re-applies them to running services.

    The ``SettingsPanel`` calls :meth:`apply` after writing new YAML to disk.
    Each service exposes a stable ``apply_settings`` hook so we can update
    log levels, browser launch options, notification channels, retry budgets
    and workflow timing without restarting the application.
    """

    def __init__(
        self,
        event_bus: EventBus,
        logging_service: LoggingService,
        browser_service: BrowserService,
        notifications: NotificationDispatcher,
        autofill: AutofillEngine,
        runner: WorkflowRunner,
    ) -> None:
        self._bus = event_bus
        self._logging = logging_service
        self._browser = browser_service
        self._notifications = notifications
        self._autofill = autofill
        self._runner = runner
        self._bus.subscribe(SETTINGS_CHANGED_TOPIC, self._on_settings_changed)

    async def _on_settings_changed(self, settings: AppSettings) -> None:
        await self.apply(settings)

    async def apply(self, settings: AppSettings) -> None:
        # Logging first so subsequent operations are observable at the new level.
        try:
            self._logging.apply_settings(settings.logging)
        except Exception:  # noqa: BLE001
            pass
        try:
            await self._browser.apply_settings(settings.browser)
        except Exception:  # noqa: BLE001
            pass
        try:
            await self._notifications.apply_settings(settings.notifications)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._autofill.apply_settings(settings.workflow)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._runner.apply_settings(settings.retry, settings.workflow)
        except Exception:  # noqa: BLE001
            pass
