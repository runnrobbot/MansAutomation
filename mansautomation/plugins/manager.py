"""Discovers, loads, and lifecycle-manages :class:`AutomationPlugin` instances."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import Iterable
from pathlib import Path

from mansautomation.core.events import EventBus
from mansautomation.core.exceptions import PluginLoadError
from mansautomation.plugins.base import AutomationPlugin, collect_plugin_classes
from mansautomation.services.logging_service import LoggingService

PLUGINS_TOPIC = "plugins.changed"


class PluginManager:
    """Loads automation plugins from one or more directories."""

    def __init__(
        self,
        directories: Iterable[Path],
        logging_service: LoggingService,
        event_bus: EventBus,
    ) -> None:
        self._directories = [Path(p) for p in directories]
        self._logger = logging_service.get_logger("plugins")
        self._event_bus = event_bus
        self._plugins: dict[str, AutomationPlugin] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        await self.reload()

    async def stop(self) -> None:
        async with self._lock:
            for plugin in list(self._plugins.values()):
                try:
                    await plugin.teardown()
                except Exception as exc:  # noqa: BLE001
                    self._logger.warning(
                        "plugin_teardown_failed",
                        plugin=plugin.metadata.id,
                        error=str(exc),
                    )
            self._plugins.clear()

    @property
    def plugins(self) -> dict[str, AutomationPlugin]:
        return dict(self._plugins)

    def get(self, plugin_id: str) -> AutomationPlugin | None:
        return self._plugins.get(plugin_id)

    def for_url(self, url: str) -> list[AutomationPlugin]:
        return [p for p in self._plugins.values() if p.matches(url)]

    async def reload(self) -> list[AutomationPlugin]:
        async with self._lock:
            await self._teardown_unlocked()
            for directory in self._directories:
                if not directory.exists():
                    directory.mkdir(parents=True, exist_ok=True)
                    continue
                for path in sorted(directory.rglob("*.py")):
                    if path.name.startswith("_"):
                        continue
                    await self._load_module(path)
            self._logger.info("plugins_loaded", count=len(self._plugins))
        await self._event_bus.publish(PLUGINS_TOPIC, list(self._plugins.values()))
        return list(self._plugins.values())

    async def _teardown_unlocked(self) -> None:
        for plugin in list(self._plugins.values()):
            try:
                await plugin.teardown()
            except Exception as exc:  # noqa: BLE001
                self._logger.warning(
                    "plugin_teardown_failed",
                    plugin=plugin.metadata.id,
                    error=str(exc),
                )
        self._plugins.clear()

    async def _load_module(self, path: Path) -> None:
        module_name = f"mansautomation_plugin_{path.stem}_{abs(hash(path))}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise PluginLoadError(f"failed to create spec for {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("plugin_import_failed", path=str(path), error=str(exc))
            return

        for plugin_cls in collect_plugin_classes(module.__dict__):
            try:
                instance = plugin_cls()
                metadata = getattr(instance, "metadata", None)
                if metadata is None:
                    self._logger.warning("plugin_missing_metadata", path=str(path))
                    continue
                if metadata.id in self._plugins:
                    self._logger.warning("plugin_duplicate_id", id=metadata.id, path=str(path))
                    continue
                await instance.setup()
                self._plugins[metadata.id] = instance
                self._logger.info(
                    "plugin_registered",
                    id=metadata.id,
                    name=metadata.name,
                    version=metadata.version,
                    path=str(path),
                )
            except Exception as exc:  # noqa: BLE001
                self._logger.warning(
                    "plugin_instantiation_failed",
                    plugin=getattr(plugin_cls, "__name__", "?"),
                    path=str(path),
                    error=str(exc),
                )
