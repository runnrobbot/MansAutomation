"""High-level profile manager: SQLite + JSON + YAML import/export."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import yaml

from mansautomation.core.events import EventBus
from mansautomation.core.exceptions import ProfileError, ProfileNotFoundError
from mansautomation.core.models import Profile
from mansautomation.services.logging_service import LoggingService
from mansautomation.services.storage_service import StorageService

PROFILES_TOPIC = "profiles.changed"


class ProfileManager:
    """Coordinates profile persistence and exposes a thread-safe cache."""

    def __init__(
        self,
        storage: StorageService,
        logging_service: LoggingService,
        event_bus: EventBus,
    ) -> None:
        self._storage = storage
        self._logger = logging_service.get_logger("profiles")
        self._event_bus = event_bus
        self._lock = asyncio.Lock()
        self._cache: dict[str, Profile] = {}
        self._loaded = False

    async def start(self) -> None:
        await self.refresh()

    async def stop(self) -> None:
        self._cache.clear()
        self._loaded = False

    async def refresh(self) -> list[Profile]:
        async with self._lock:
            profiles = await self._storage.list_profiles()
            self._cache = {p.id: p for p in profiles}
            self._loaded = True
        await self._event_bus.publish(PROFILES_TOPIC, list(self._cache.values()))
        return list(self._cache.values())

    async def list_profiles(self) -> list[Profile]:
        if not self._loaded:
            return await self.refresh()
        return list(self._cache.values())

    async def get(self, profile_id: str) -> Profile:
        if profile_id in self._cache:
            return self._cache[profile_id]
        profile = await self._storage.get_profile(profile_id)
        self._cache[profile.id] = profile
        return profile

    async def get_by_name(self, name: str) -> Profile | None:
        normalized = name.strip().lower()
        for profile in self._cache.values():
            if profile.name.lower() == normalized:
                return profile
        return None

    async def save(self, profile: Profile) -> Profile:
        if not profile.name.strip():
            raise ProfileError("profile name must not be empty")
        async with self._lock:
            await self._storage.save_profile(profile)
            self._cache[profile.id] = profile
        self._logger.info("profile_saved", profile_id=profile.id, name=profile.name)
        await self._event_bus.publish(PROFILES_TOPIC, list(self._cache.values()))
        return profile

    async def delete(self, profile_id: str) -> None:
        async with self._lock:
            if profile_id not in self._cache:
                # Verify that it's not present in storage either
                try:
                    await self._storage.get_profile(profile_id)
                except ProfileNotFoundError as exc:
                    raise exc
            await self._storage.delete_profile(profile_id)
            self._cache.pop(profile_id, None)
        self._logger.info("profile_deleted", profile_id=profile_id)
        await self._event_bus.publish(PROFILES_TOPIC, list(self._cache.values()))

    async def export_json(self, profile_id: str, path: Path) -> Path:
        profile = await self.get(profile_id)
        await asyncio.to_thread(
            path.write_text,
            json.dumps(_export_payload(profile), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    async def export_yaml(self, profile_id: str, path: Path) -> Path:
        profile = await self.get(profile_id)
        payload = _export_payload(profile)
        await asyncio.to_thread(_write_yaml, path, payload)
        return path

    async def import_file(self, path: Path) -> Profile:
        payload = await asyncio.to_thread(_read_structured_file, path)
        if not isinstance(payload, dict):
            raise ProfileError(f"unsupported profile payload in {path.name}")
        try:
            profile = Profile.model_validate(payload)
        except Exception as exc:
            raise ProfileError(f"invalid profile data: {exc}") from exc
        return await self.save(profile)


def _export_payload(profile: Profile) -> dict[str, Any]:
    payload = profile.model_dump(mode="json")
    # Strip secret bank fields from exports - users can re-enter them
    if "bank" in payload and isinstance(payload["bank"], dict):
        for secret_key in ("account_number", "routing_number", "iban"):
            if secret_key in payload["bank"]:
                payload["bank"][secret_key] = None
    if "login" in payload and isinstance(payload["login"], dict):
        if "password" in payload["login"]:
            payload["login"]["password"] = None
    return payload


def _read_structured_file(path: Path) -> Any:
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    if suffix == ".json":
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    raise ProfileError(f"unsupported file type: {path.suffix}")


def _write_yaml(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, sort_keys=False, allow_unicode=True)
