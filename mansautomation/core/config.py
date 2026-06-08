"""Application configuration models."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator

from mansautomation.core.exceptions import ConfigurationError

SETTINGS_CHANGED_TOPIC = "settings.changed"


class BrowserSettings(BaseModel):
    """Configuration for the Playwright browser engine."""

    engine: Literal["chromium", "firefox", "webkit"] = "chromium"
    headless: bool = False
    slow_mo_ms: int = 0
    user_agent: str | None = None
    viewport_width: int = 1366
    viewport_height: int = 820
    locale: str = "en-US"
    timezone: str = "UTC"
    persistent_session: bool = True
    disable_blink_features: bool = True
    block_resources: list[str] = Field(default_factory=list)
    default_timeout_ms: int = 15_000
    navigation_timeout_ms: int = 25_000
    proxy_url: str | None = None


class RetrySettings(BaseModel):
    max_attempts: int = 5
    base_delay_seconds: float = 0.4
    max_delay_seconds: float = 6.0
    jitter: float = 0.3


class WorkflowSettings(BaseModel):
    field_typing_delay_ms: int = 12
    inter_field_delay_ms: int = 35
    poll_interval_ms: int = 200
    capture_screenshots_on_error: bool = True
    auto_recover: bool = True
    # Budget multiplier applied to all sync waits (DOM-settle, hydration,
    # network-quiet, etc.). Lower the value for faster machines / better
    # connections; raise it on slow networks / underpowered hardware.
    sync_speed_multiplier: float = 1.0


class TelegramSettings(BaseModel):
    enabled: bool = False
    bot_token: SecretStr | None = None
    chat_id: str | None = None


class DiscordSettings(BaseModel):
    enabled: bool = False
    webhook_url: SecretStr | None = None


class DesktopNotificationSettings(BaseModel):
    enabled: bool = True
    sound_enabled: bool = True


class NotificationSettings(BaseModel):
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    discord: DiscordSettings = Field(default_factory=DiscordSettings)
    desktop: DesktopNotificationSettings = Field(default_factory=DesktopNotificationSettings)


class LoggingSettings(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    json_logs: bool = True
    log_to_file: bool = True
    rotation_max_bytes: int = 5 * 1024 * 1024
    rotation_backups: int = 5


class AppSettings(BaseModel):
    """Root application configuration."""

    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    workflow: WorkflowSettings = Field(default_factory=WorkflowSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    active_profile_id: str | None = None
    enabled_plugins: list[str] = Field(default_factory=list)

    @field_validator("enabled_plugins")
    @classmethod
    def _unique_plugins(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for entry in value:
            if entry not in seen:
                seen.add(entry)
                result.append(entry)
        return result


def load_settings(path: Path) -> AppSettings:
    """Load settings from disk, creating the file with defaults if missing."""

    if not path.exists():
        defaults = AppSettings()
        save_settings(path, defaults)
        return defaults
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw: Any = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"failed to read settings file: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("settings file must contain a mapping at the top level")
    try:
        return AppSettings.model_validate(raw)
    except Exception as exc:
        raise ConfigurationError(f"invalid settings: {exc}") from exc


def save_settings(path: Path, settings: AppSettings) -> None:
    """Persist settings to disk in YAML format."""

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = settings.model_dump(mode="json", exclude_none=False)
    try:
        with path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, sort_keys=False, allow_unicode=True)
    except OSError as exc:
        raise ConfigurationError(f"failed to write settings file: {exc}") from exc
