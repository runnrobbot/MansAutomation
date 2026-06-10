"""A minimal type-safe dependency injection container."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar, cast, runtime_checkable

T = TypeVar("T")


@runtime_checkable
class AsyncLifecycle(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class Container:
    """Singleton-only dependency injection container.

    The container resolves registered factories lazily and caches the resulting
    instance. Instances implementing :class:`AsyncLifecycle` are tracked so the
    container can orchestrate async startup/shutdown.
    """

    def __init__(self) -> None:
        self._factories: dict[type, Callable[[Container], Any]] = {}
        self._instances: dict[type, Any] = {}
        self._lifecycle: list[AsyncLifecycle] = []
        self._lock = asyncio.Lock()
        self._started = False

    def register(self, key: type[T], factory: Callable[[Container], T]) -> None:
        if key in self._factories:
            raise RuntimeError(f"service already registered: {key.__name__}")
        self._factories[key] = factory

    def register_instance(self, key: type[T], instance: T) -> None:
        self._instances[key] = instance
        if isinstance(instance, AsyncLifecycle) and instance not in self._lifecycle:
            self._lifecycle.append(instance)

    def resolve(self, key: type[T]) -> T:
        if key in self._instances:
            return cast(T, self._instances[key])
        factory = self._factories.get(key)
        if factory is None:
            raise RuntimeError(f"no service registered for {key.__name__}")
        instance = factory(self)
        if inspect.isawaitable(instance):  # pragma: no cover - defensive
            raise RuntimeError("factories must return synchronously; use async start() instead")
        self._instances[key] = instance
        if isinstance(instance, AsyncLifecycle):
            self._lifecycle.append(instance)
        return cast(T, instance)

    def try_resolve(self, key: type[T]) -> T | None:
        try:
            return self.resolve(key)
        except RuntimeError:
            return None

    async def start_async_services(self) -> None:
        async with self._lock:
            if self._started:
                return
            for service in list(self._lifecycle):
                await _maybe_await(service.start())
            self._started = True

    async def stop_async_services(self) -> None:
        async with self._lock:
            if not self._started:
                return
            for service in reversed(self._lifecycle):
                try:
                    await _maybe_await(service.stop())
                except Exception:  # noqa: BLE001 - best effort shutdown
                    continue
            self._started = False


async def _maybe_await(value: Awaitable[Any] | Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value
