"""Abstract base classes for automation plugins."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Page
from pydantic import BaseModel, Field

from mansautomation.automation.autofill_engine import AutofillEngine, AutofillResult
from mansautomation.automation.dom_extractor import DomExtractor
from mansautomation.automation.form_detector import FormDetectionEngine
from mansautomation.automation.interactions import (
    click_when_enabled,
    resilient_click,
    resilient_fill,
    resolve_first_visible,
    wait_for_any,
    wait_for_disappear,
    wait_for_enabled,
)
from mansautomation.automation.sync import (
    wait_for_dom_settle,
    wait_for_hydration,
    wait_for_network_quiet,
    wait_for_page_ready,
)
from mansautomation.core.config import WorkflowSettings
from mansautomation.core.models import (
    Profile,
    WorkflowEventLevel,
    WorkflowJob,
)


@dataclass(frozen=True, slots=True)
class SyncToolkit:
    """Bundle of resilient interaction + synchronisation primitives.

    Plugins use these helpers instead of raw Playwright calls when they need
    React/Next.js-grade resilience: stale-recovery clicks, fallback selector
    chains, MutationObserver-based DOM-settle waits, and adaptive network
    quiet detection.
    """

    click_when_enabled: Callable[..., Awaitable[Any]]
    resilient_click: Callable[..., Awaitable[Any]]
    resilient_fill: Callable[..., Awaitable[Any]]
    resolve_first_visible: Callable[..., Awaitable[Any]]
    wait_for_any: Callable[..., Awaitable[Any]]
    wait_for_disappear: Callable[..., Awaitable[Any]]
    wait_for_enabled: Callable[..., Awaitable[Any]]
    wait_for_dom_settle: Callable[..., Awaitable[Any]]
    wait_for_hydration: Callable[..., Awaitable[Any]]
    wait_for_network_quiet: Callable[..., Awaitable[Any]]
    wait_for_page_ready: Callable[..., Awaitable[Any]]


_SYNC_TOOLKIT = SyncToolkit(
    click_when_enabled=click_when_enabled,
    resilient_click=resilient_click,
    resilient_fill=resilient_fill,
    resolve_first_visible=resolve_first_visible,
    wait_for_any=wait_for_any,
    wait_for_disappear=wait_for_disappear,
    wait_for_enabled=wait_for_enabled,
    wait_for_dom_settle=wait_for_dom_settle,
    wait_for_hydration=wait_for_hydration,
    wait_for_network_quiet=wait_for_network_quiet,
    wait_for_page_ready=wait_for_page_ready,
)


@dataclass(frozen=True, slots=True)
class PluginMetadata:
    """Static description of a plugin used by the manager and the GUI."""

    id: str
    name: str
    version: str
    description: str = ""
    author: str = ""
    target_domains: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()


@dataclass(slots=True)
class PluginContext:
    """Runtime context handed to a plugin invocation."""

    page: Page | None
    profile: Profile
    job: WorkflowJob
    autofill: AutofillEngine
    form_detector: FormDetectionEngine
    dom_extractor: DomExtractor
    logger: Any
    workflow_settings: WorkflowSettings
    emit_event: Callable[..., Awaitable[None]]
    request_human: Callable[..., Awaitable[None]]
    wait_for_human_ack: Callable[[], Awaitable[None]]
    is_aborted: Callable[[], bool]
    artifacts: dict[str, Any] = field(default_factory=dict)
    sync: SyncToolkit = field(default_factory=lambda: _SYNC_TOOLKIT)


class PluginExecutionResult(BaseModel):
    success: bool
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)
    autofill: dict[str, Any] | None = None


class AutomationPlugin(ABC):
    """Base class all automation plugins must extend."""

    metadata: PluginMetadata

    async def setup(self) -> None:
        """Hook executed once when the plugin is loaded."""

    async def teardown(self) -> None:
        """Hook executed when the plugin is unloaded."""

    @abstractmethod
    async def execute(self, context: PluginContext) -> PluginExecutionResult:
        """Run the plugin against the prepared context."""

    def matches(self, target_url: str) -> bool:
        if not self.metadata.target_domains:
            return True
        target = target_url.lower()
        return any(domain.lower() in target for domain in self.metadata.target_domains)


def collect_plugin_classes(module_globals: dict[str, Any]) -> Iterable[type[AutomationPlugin]]:
    """Yield AutomationPlugin subclasses defined inside a module's globals."""

    for value in module_globals.values():
        if (
            isinstance(value, type)
            and issubclass(value, AutomationPlugin)
            and value is not AutomationPlugin
        ):
            yield value


__all__ = [
    "AutomationPlugin",
    "AutofillResult",
    "PluginContext",
    "PluginExecutionResult",
    "PluginMetadata",
    "WorkflowEventLevel",
    "collect_plugin_classes",
]
