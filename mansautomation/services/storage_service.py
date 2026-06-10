"""SQLite-backed storage for profiles and workflow history."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from mansautomation.core.exceptions import ProfileError, ProfileNotFoundError
from mansautomation.core.models import Profile
from mansautomation.services.crypto_service import CryptoService

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    payload BLOB NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    plugin_id TEXT NOT NULL,
    profile_id TEXT,
    status TEXT NOT NULL,
    target_url TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_history_started_at ON workflow_history(started_at DESC);
"""


class StorageService:
    """Async SQLite gateway for persisted profiles and workflow telemetry."""

    def __init__(self, db_path: Path, crypto: CryptoService) -> None:
        self._db_path = db_path
        self._crypto = crypto
        self._connection: aiosqlite.Connection | None = None

    async def start(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(str(self._db_path))
        await self._connection.execute("PRAGMA journal_mode=WAL")
        await self._connection.execute("PRAGMA foreign_keys=ON")
        await self._connection.executescript(_SCHEMA)
        await self._connection.commit()

    async def stop(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise ProfileError("storage service is not started")
        return self._connection

    async def list_profiles(self) -> list[Profile]:
        async with self._conn.execute(
            "SELECT id, payload FROM profiles ORDER BY name COLLATE NOCASE ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        result: list[Profile] = []
        for _, payload in rows:
            decrypted = self._crypto.decrypt(payload)
            try:
                result.append(Profile.model_validate_json(decrypted))
            except Exception as exc:
                raise ProfileError(f"corrupt profile payload: {exc}") from exc
        return result

    async def get_profile(self, profile_id: str) -> Profile:
        async with self._conn.execute(
            "SELECT payload FROM profiles WHERE id = ?", (profile_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise ProfileNotFoundError(profile_id)
        return Profile.model_validate_json(self._crypto.decrypt(row[0]))

    async def save_profile(self, profile: Profile) -> None:
        profile.touch()
        payload = self._crypto.encrypt(profile.model_dump_json().encode("utf-8"))
        await self._conn.execute(
            """
            INSERT INTO profiles (id, name, payload, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (
                profile.id,
                profile.name,
                payload,
                profile.created_at.isoformat(),
                profile.updated_at.isoformat(),
            ),
        )
        await self._conn.commit()

    async def delete_profile(self, profile_id: str) -> None:
        await self._conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        await self._conn.commit()

    async def append_history(
        self,
        *,
        job_id: str,
        plugin_id: str,
        profile_id: str | None,
        status: str,
        target_url: str | None,
        started_at: datetime,
        finished_at: datetime | None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self._conn.execute(
            """
            INSERT INTO workflow_history (
                job_id, plugin_id, profile_id, status, target_url,
                started_at, finished_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                plugin_id,
                profile_id,
                status,
                target_url,
                started_at.astimezone(timezone.utc).isoformat(),
                finished_at.astimezone(timezone.utc).isoformat() if finished_at else None,
                json.dumps(payload, ensure_ascii=False) if payload else None,
            ),
        )
        await self._conn.commit()
