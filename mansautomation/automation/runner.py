"""High-level automation runner that orchestrates plugins, browsers, and recovery."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from playwright.async_api import Page

from mansautomation.automation.autofill_engine import AutofillEngine
from mansautomation.automation.dom_extractor import DomExtractor
from mansautomation.automation.form_detector import FormDetectionEngine
from mansautomation.automation.human_intervention import (
    HumanInterventionDetector,
    HumanInterventionSignal,
)
from mansautomation.automation.interactions import (
    DEFAULT_LOADING_SELECTORS,
    wait_for_disappear,
)
from mansautomation.automation.resilience import HeartbeatWatchdog
from mansautomation.automation.sync import (
    wait_for_dom_settle,
    wait_for_network_quiet,
    wait_for_page_ready,
)
from mansautomation.core.config import RetrySettings, WorkflowSettings
from mansautomation.core.events import EventBus
from mansautomation.core.exceptions import (
    HumanInterventionRequired,
    PluginError,
    WorkflowAbortedError,
)
from mansautomation.core.models import (
    Profile,
    WorkflowEvent,
    WorkflowEventLevel,
    WorkflowJob,
    WorkflowStatus,
)
from mansautomation.notifications.base import Notification, NotificationLevel
from mansautomation.notifications.dispatcher import NotificationDispatcher
from mansautomation.plugins.base import (
    AutomationPlugin,
    PluginContext,
    PluginExecutionResult,
)
from mansautomation.plugins.manager import PluginManager
from mansautomation.profiles.manager import ProfileManager
from mansautomation.services.browser_service import BrowserService
from mansautomation.services.logging_service import LoggingService
from mansautomation.services.storage_service import StorageService

WORKFLOW_EVENT_TOPIC = "workflow.event"
WORKFLOW_STATUS_TOPIC = "workflow.status"


class WorkflowRunner:
    """Coordinates plugin execution with smart retries and recovery."""

    def __init__(
        self,
        plugin_manager: PluginManager,
        profile_manager: ProfileManager,
        browser_service: BrowserService,
        autofill_engine: AutofillEngine,
        form_detector: FormDetectionEngine,
        dom_extractor: DomExtractor,
        notifications: NotificationDispatcher,
        storage: StorageService,
        retry_settings: RetrySettings,
        workflow_settings: WorkflowSettings,
        event_bus: EventBus,
        logging_service: LoggingService,
    ) -> None:
        self._plugin_manager = plugin_manager
        self._profile_manager = profile_manager
        self._browser_service = browser_service
        self._autofill_engine = autofill_engine
        self._form_detector = form_detector
        self._dom_extractor = dom_extractor
        self._notifications = notifications
        self._storage = storage
        self._retry_settings = retry_settings
        self._workflow_settings = workflow_settings
        self._event_bus = event_bus
        self._logger = logging_service.get_logger("workflow")
        self._active_task: asyncio.Task[Any] | None = None
        self._abort_event = asyncio.Event()
        self._human_event = asyncio.Event()
        self._human_signal: HumanInterventionSignal | None = None
        self._intervention_detector = HumanInterventionDetector()
        self._lock = asyncio.Lock()

    def apply_settings(
        self,
        retry_settings: RetrySettings,
        workflow_settings: WorkflowSettings,
    ) -> None:
        self._retry_settings = retry_settings
        self._workflow_settings = workflow_settings

    @property
    def is_running(self) -> bool:
        return self._active_task is not None and not self._active_task.done()

    @property
    def human_signal(self) -> HumanInterventionSignal | None:
        return self._human_signal

    async def submit(self, job: WorkflowJob) -> asyncio.Task[PluginExecutionResult]:
        async with self._lock:
            if self.is_running:
                raise WorkflowAbortedError("a workflow is already running")
            self._abort_event.clear()
            self._human_event.clear()
            self._human_signal = None
            task: asyncio.Task[PluginExecutionResult] = asyncio.create_task(
                self._run(job), name=f"workflow-{job.id}"
            )
            self._active_task = task
            return task

    async def abort(self) -> None:
        if self._active_task and not self._active_task.done():
            self._abort_event.set()
            self._active_task.cancel()
            try:
                await self._active_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def acknowledge_human(self) -> None:
        self._human_event.set()

    async def _run(self, job: WorkflowJob) -> PluginExecutionResult:
        plugin = self._plugin_manager.get(job.plugin_id)
        if plugin is None:
            raise PluginError(f"unknown plugin: {job.plugin_id}")
        profile = await self._profile_manager.get(job.profile_id)
        started = datetime.now(tz=timezone.utc)
        await self._publish_status(job, WorkflowStatus.STARTING, "workflow_started")
        result: PluginExecutionResult | None = None
        status = WorkflowStatus.FAILED
        error_payload: dict[str, Any] | None = None
        try:
            result = await self._execute(job, plugin, profile)
            status = WorkflowStatus.COMPLETED if result.success else WorkflowStatus.FAILED
        except asyncio.CancelledError:
            status = WorkflowStatus.ABORTED
            await self._publish_status(job, status, "workflow_cancelled")
            raise
        except HumanInterventionRequired as exc:
            status = WorkflowStatus.HUMAN_REQUIRED
            await self._publish_status(
                job, status, exc.reason, level=WorkflowEventLevel.WARN, context={"url": exc.url}
            )
            error_payload = {"reason": exc.reason, "url": exc.url}
        except Exception as exc:  # noqa: BLE001
            self._logger.error("workflow_failed", error=str(exc), job_id=job.id)
            await self._publish_status(
                job,
                WorkflowStatus.FAILED,
                f"workflow_failed: {exc}",
                level=WorkflowEventLevel.ERROR,
            )
            error_payload = {"error": str(exc)}
        finally:
            finished = datetime.now(tz=timezone.utc)
            payload: dict[str, Any] = {}
            if result is not None:
                payload["result"] = result.model_dump(mode="json")
            if error_payload:
                payload["error"] = error_payload
            try:
                await self._storage.append_history(
                    job_id=job.id,
                    plugin_id=job.plugin_id,
                    profile_id=job.profile_id,
                    status=status.value,
                    target_url=job.target_url,
                    started_at=started,
                    finished_at=finished,
                    payload=payload or None,
                )
            except Exception as exc:  # noqa: BLE001
                self._logger.warning("workflow_history_failed", error=str(exc))
            await self._publish_status(job, status, f"workflow_{status.value}")
            await self._notifications.dispatch(
                Notification(
                    title="Workflow finished" if status == WorkflowStatus.COMPLETED else "Workflow update",
                    message=f"{plugin.metadata.name} → {status.value}",
                    level=_status_to_level(status),
                )
            )
            self._active_task = None

        if result is None:
            return PluginExecutionResult(success=False, message=f"workflow ended in status {status.value}")
        return result

    async def _execute(
        self,
        job: WorkflowJob,
        plugin: AutomationPlugin,
        profile: Profile,
    ) -> PluginExecutionResult:
        context = await self._build_context(job, plugin, profile)
        async with self._page_for(job) as page:
            context.page = page
            await self._publish_status(
                job, WorkflowStatus.RUNNING, "workflow_running",
                context={"plugin": plugin.metadata.id, "profile": profile.name},
            )
            attempt = 0
            last_exc: Exception | None = None
            while attempt < self._retry_settings.max_attempts:
                attempt += 1
                self._raise_if_aborted()
                try:
                    await self._wait_for_navigation_safe(page, job.target_url)
                    await self._await_human_if_needed(page, job)
                    result = await plugin.execute(context)
                    return result
                except HumanInterventionRequired as exc:
                    self._human_signal = HumanInterventionSignal(
                        reason=exc.reason, detail=exc.reason, url=exc.url or page.url
                    )
                    await self._await_human_if_needed(page, job, force=True)
                    last_exc = exc
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    self._logger.warning(
                        "workflow_attempt_failed",
                        attempt=attempt,
                        error=str(exc),
                        job_id=job.id,
                    )
                    if not self._workflow_settings.auto_recover:
                        raise
                    await self._publish_status(
                        job,
                        WorkflowStatus.RUNNING,
                        f"retrying after error: {exc}",
                        level=WorkflowEventLevel.WARN,
                        context={"attempt": attempt},
                    )
                    # Allow React/Next.js to fully re-mount before retrying.
                    try:
                        await wait_for_dom_settle(page, quiet_ms=300, timeout_ms=3_000)
                        await wait_for_network_quiet(page, quiet_ms=400, timeout_ms=3_000)
                    except Exception:  # noqa: BLE001
                        pass
                    await self._sleep_with_backoff(attempt)
            raise last_exc or RuntimeError("workflow failed without specific error")

    @asynccontextmanager
    async def _page_for(self, job: WorkflowJob) -> AsyncIterator[Page]:
        context = await self._browser_service.acquire_context(session_name=f"job-{job.plugin_id}")
        page = await context.new_page()
        watchers = await self._start_resilience_watchers(page, job)
        try:
            yield page
        finally:
            await self._stop_resilience_watchers(watchers)
            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass

    async def _start_resilience_watchers(
        self, page: Page, job: WorkflowJob
    ) -> list[Any]:
        """Attach the heartbeat watchdog and forward its events.

        Only a lightweight, read-only liveness watchdog runs here. Queue /
        waiting-room progression is tracked authoritatively by the plugin's
        ``_wait_through_queue`` (which reads queue-it's embedded JSON), so we
        deliberately do not run a second text-scraping queue watcher - that
        would only add page load and emit confusing "queue reconnected" noise
        during a high-traffic sale.
        """

        async def _emit(name: str, payload: dict[str, Any]) -> None:
            await self._publish_event(
                job,
                message=name.replace("_", " "),
                level=_resilience_level(name),
                status=WorkflowStatus.RUNNING,
                context={"event": name, **payload},
            )
            if name == "heartbeat_unhealthy":
                await self._notifications.dispatch(
                    Notification(
                        title="Workflow alert",
                        message=f"{name.replace('_', ' ')} - {payload.get('url', '')}",
                        level=NotificationLevel.WARNING,
                    )
                )

        heartbeat = HeartbeatWatchdog(page, _emit)
        await heartbeat.start()
        return [heartbeat]

    async def _stop_resilience_watchers(self, watchers: list[Any]) -> None:
        for watcher in watchers:
            try:
                await watcher.stop()
            except Exception:  # noqa: BLE001
                continue

    async def _build_context(
        self,
        job: WorkflowJob,
        plugin: AutomationPlugin,
        profile: Profile,
    ) -> PluginContext:
        return PluginContext(
            page=None,  # set later by _execute
            profile=profile,
            job=job,
            autofill=self._autofill_engine,
            form_detector=self._form_detector,
            dom_extractor=self._dom_extractor,
            logger=self._logger.bind(plugin=plugin.metadata.id, job_id=job.id),
            workflow_settings=self._workflow_settings,
            emit_event=self._make_event_emitter(job),
            request_human=self._make_human_handler(job),
            wait_for_human_ack=self._wait_for_human_ack,
            is_aborted=lambda: self._abort_event.is_set(),
        )

    def _make_event_emitter(self, job: WorkflowJob) -> Callable[..., Awaitable[None]]:
        async def emit(message: str, *, level: WorkflowEventLevel = WorkflowEventLevel.INFO,
                       context: dict[str, Any] | None = None) -> None:
            await self._publish_event(job, message=message, level=level, status=WorkflowStatus.RUNNING,
                                      context=context or {})

        return emit

    def _make_human_handler(self, job: WorkflowJob) -> Callable[..., Awaitable[None]]:
        async def handler(reason: str, *, url: str | None = None) -> None:
            self._human_signal = HumanInterventionSignal(reason=reason, detail=reason, url=url or "")
            self._human_event.clear()
            await self._publish_status(
                job,
                WorkflowStatus.HUMAN_REQUIRED,
                f"human intervention required: {reason}",
                level=WorkflowEventLevel.WARN,
                context={"url": url},
            )
            await self._notifications.dispatch(
                Notification(
                    title="Manual action required",
                    message=reason,
                    level=NotificationLevel.WARNING,
                )
            )
            await self._human_event.wait()
            self._human_signal = None
            await self._publish_status(
                job,
                WorkflowStatus.RUNNING,
                "resumed after manual intervention",
                level=WorkflowEventLevel.INFO,
            )

        return handler

    async def _wait_for_human_ack(self) -> None:
        await self._human_event.wait()

    async def _await_human_if_needed(self, page: Page, job: WorkflowJob, *, force: bool = False) -> None:
        signal: HumanInterventionSignal | None
        if force:
            signal = self._human_signal
        else:
            signal = await self._intervention_detector.detect(page)
        if signal is None:
            return
        # Queues / waiting rooms are handled automatically by plugins that
        # support them (they actively wait and track position). Don't pause
        # the workflow for a queue here - only genuine CAPTCHA / anti-bot
        # challenges require a human.
        if signal.reason == "queue" and not force:
            return
        self._human_signal = signal
        self._human_event.clear()
        await self._publish_status(
            job,
            WorkflowStatus.HUMAN_REQUIRED,
            f"intervention required ({signal.reason})",
            level=WorkflowEventLevel.WARN,
            context={"detail": signal.detail, "url": signal.url},
        )
        await self._notifications.dispatch(
            Notification(
                title="Manual action required",
                message=f"{signal.reason}: {signal.detail}",
                level=NotificationLevel.WARNING,
            )
        )
        await self._human_event.wait()
        self._human_signal = None
        await self._publish_status(
            job, WorkflowStatus.RUNNING, "resumed after manual intervention",
        )

    async def _wait_for_navigation_safe(self, page: Page, url: str) -> None:
        if not url:
            return
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("navigation_failed", error=str(exc), url=url)
            raise
        # Adaptive synchronisation guard: hydration + DOM settle + network
        # quiet. Each step has its own bounded budget that scales with the
        # configured workflow speed so fast setups don't pay 20s/nav.
        speed = max(0.25, float(getattr(self._workflow_settings, "sync_speed_multiplier", 1.0)))
        try:
            statuses = await wait_for_page_ready(
                page,
                hydration_timeout_ms=int(6_000 * speed),
                settle_quiet_ms=int(300 * speed),
                settle_timeout_ms=int(4_000 * speed),
                network_timeout_ms=int(4_000 * speed),
            )
            self._logger.debug("page_ready", url=page.url, **statuses)
        except Exception as exc:  # noqa: BLE001
            self._logger.debug("page_ready_failed", error=str(exc))
        # Skeleton/spinner overlays are commonly painted during hydration; give
        # them a chance to disappear before the plugin starts interacting.
        try:
            await wait_for_disappear(
                page, DEFAULT_LOADING_SELECTORS, timeout_ms=int(3_000 * speed)
            )
        except Exception:  # noqa: BLE001
            pass

    async def _sleep_with_backoff(self, attempt: int) -> None:
        base = self._retry_settings.base_delay_seconds
        delay = min(self._retry_settings.max_delay_seconds, base * (2 ** (attempt - 1)))
        delay = max(0.0, delay + random.uniform(-self._retry_settings.jitter, self._retry_settings.jitter))
        await asyncio.sleep(delay)

    def _raise_if_aborted(self) -> None:
        if self._abort_event.is_set():
            raise WorkflowAbortedError("workflow aborted by operator")

    async def _publish_status(
        self,
        job: WorkflowJob,
        status: WorkflowStatus,
        message: str,
        *,
        level: WorkflowEventLevel = WorkflowEventLevel.INFO,
        context: dict[str, Any] | None = None,
    ) -> None:
        await self._publish_event(job, message=message, level=level, status=status,
                                  context=context or {})
        await self._event_bus.publish(WORKFLOW_STATUS_TOPIC, {"job_id": job.id, "status": status.value})

    async def _publish_event(
        self,
        job: WorkflowJob,
        *,
        message: str,
        level: WorkflowEventLevel,
        status: WorkflowStatus,
        context: dict[str, Any],
    ) -> None:
        event = WorkflowEvent(level=level, status=status, message=message, context=context)
        payload = {"job_id": job.id, "event": event.model_dump(mode="json")}
        await self._event_bus.publish(WORKFLOW_EVENT_TOPIC, payload)
        log = self._logger.bind(job_id=job.id, status=status.value)
        if level == WorkflowEventLevel.ERROR:
            log.error(message, **context)
        elif level == WorkflowEventLevel.WARN:
            log.warning(message, **context)
        elif level == WorkflowEventLevel.DEBUG:
            log.debug(message, **context)
        else:
            log.info(message, **context)


def _status_to_level(status: WorkflowStatus) -> NotificationLevel:
    if status == WorkflowStatus.COMPLETED:
        return NotificationLevel.SUCCESS
    if status in {WorkflowStatus.FAILED, WorkflowStatus.ABORTED}:
        return NotificationLevel.ERROR
    if status == WorkflowStatus.HUMAN_REQUIRED:
        return NotificationLevel.WARNING
    return NotificationLevel.INFO


def _resilience_level(event_name: str) -> WorkflowEventLevel:
    if event_name == "heartbeat_unhealthy":
        return WorkflowEventLevel.WARN
    return WorkflowEventLevel.INFO
