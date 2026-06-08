"""Wire all services into a :class:`Container`."""

from __future__ import annotations

from pathlib import Path

from mansautomation.automation.autofill_engine import AutofillEngine
from mansautomation.automation.dom_extractor import DomExtractor
from mansautomation.automation.form_detector import FormDetectionEngine
from mansautomation.automation.runner import WorkflowRunner
from mansautomation.core.config import AppSettings, load_settings
from mansautomation.core.container import Container
from mansautomation.core.events import EventBus
from mansautomation.core.paths import AppPaths, resolve_paths
from mansautomation.core.settings_applier import SettingsApplier
from mansautomation.notifications.dispatcher import NotificationDispatcher
from mansautomation.plugins.manager import PluginManager
from mansautomation.profiles.manager import ProfileManager
from mansautomation.services.browser_service import BrowserService
from mansautomation.services.crypto_service import CryptoService
from mansautomation.services.logging_service import LoggingService
from mansautomation.services.storage_service import StorageService


def build_container() -> Container:
    """Construct and wire the full application container."""

    container = Container()

    paths = resolve_paths()
    settings = load_settings(paths.settings_path)

    container.register_instance(AppPaths, paths)
    container.register_instance(AppSettings, settings)

    container.register(EventBus, lambda _: EventBus())
    container.register(
        LoggingService,
        lambda c: LoggingService(c.resolve(AppSettings).logging, c.resolve(AppPaths).log_dir),
    )
    container.register(
        CryptoService,
        lambda c: CryptoService(c.resolve(AppPaths).keystore_path),
    )
    container.register(
        StorageService,
        lambda c: StorageService(c.resolve(AppPaths).profiles_db, c.resolve(CryptoService)),
    )
    container.register(
        ProfileManager,
        lambda c: ProfileManager(
            c.resolve(StorageService),
            c.resolve(LoggingService),
            c.resolve(EventBus),
        ),
    )
    container.register(
        BrowserService,
        lambda c: BrowserService(
            c.resolve(AppSettings).browser,
            c.resolve(AppPaths).sessions_dir,
            c.resolve(LoggingService),
        ),
    )
    container.register(
        NotificationDispatcher,
        lambda c: NotificationDispatcher(
            c.resolve(AppSettings).notifications,
            c.resolve(LoggingService),
            c.resolve(EventBus),
        ),
    )
    container.register(DomExtractor, lambda _: DomExtractor())
    container.register(FormDetectionEngine, lambda _: FormDetectionEngine())
    container.register(
        AutofillEngine,
        lambda c: AutofillEngine(
            c.resolve(FormDetectionEngine),
            c.resolve(DomExtractor),
            c.resolve(AppSettings).workflow,
            c.resolve(LoggingService),
        ),
    )
    container.register(
        PluginManager,
        lambda c: PluginManager(
            [c.resolve(AppPaths).plugins_dir, _builtin_plugins_dir()],
            c.resolve(LoggingService),
            c.resolve(EventBus),
        ),
    )
    container.register(
        WorkflowRunner,
        lambda c: WorkflowRunner(
            c.resolve(PluginManager),
            c.resolve(ProfileManager),
            c.resolve(BrowserService),
            c.resolve(AutofillEngine),
            c.resolve(FormDetectionEngine),
            c.resolve(DomExtractor),
            c.resolve(NotificationDispatcher),
            c.resolve(StorageService),
            c.resolve(AppSettings).retry,
            c.resolve(AppSettings).workflow,
            c.resolve(EventBus),
            c.resolve(LoggingService),
        ),
    )
    container.register(
        SettingsApplier,
        lambda c: SettingsApplier(
            c.resolve(EventBus),
            c.resolve(LoggingService),
            c.resolve(BrowserService),
            c.resolve(NotificationDispatcher),
            c.resolve(AutofillEngine),
            c.resolve(WorkflowRunner),
        ),
    )

    # Force eager resolution so logging is configured immediately.
    container.resolve(LoggingService)
    # Eagerly resolve the settings applier so it subscribes to the event bus.
    container.resolve(SettingsApplier)

    return container


def _builtin_plugins_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "plugins"
