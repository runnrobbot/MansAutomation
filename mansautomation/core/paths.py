"""Filesystem path resolution helpers using platform-appropriate directories."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from platformdirs import PlatformDirs

_APP_NAME = "MansAutomation"
_APP_AUTHOR = "MansAutomation"


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Resolved filesystem locations used across the application."""

    data_dir: Path
    config_dir: Path
    cache_dir: Path
    log_dir: Path
    plugins_dir: Path
    profiles_dir: Path
    sessions_dir: Path
    keystore_path: Path
    profiles_db: Path
    settings_path: Path

    def ensure(self) -> None:
        for path in (
            self.data_dir,
            self.config_dir,
            self.cache_dir,
            self.log_dir,
            self.plugins_dir,
            self.profiles_dir,
            self.sessions_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def resolve_paths() -> AppPaths:
    dirs = PlatformDirs(_APP_NAME, _APP_AUTHOR, roaming=False, ensure_exists=True)
    data_dir = Path(dirs.user_data_dir)
    config_dir = Path(dirs.user_config_dir)
    cache_dir = Path(dirs.user_cache_dir)
    log_dir = Path(dirs.user_log_dir)
    plugins_dir = data_dir / "plugins"
    profiles_dir = data_dir / "profiles"
    sessions_dir = data_dir / "sessions"
    keystore_path = data_dir / "keystore.bin"
    profiles_db = data_dir / "profiles.db"
    settings_path = config_dir / "settings.yaml"
    paths = AppPaths(
        data_dir=data_dir,
        config_dir=config_dir,
        cache_dir=cache_dir,
        log_dir=log_dir,
        plugins_dir=plugins_dir,
        profiles_dir=profiles_dir,
        sessions_dir=sessions_dir,
        keystore_path=keystore_path,
        profiles_db=profiles_db,
        settings_path=settings_path,
    )
    paths.ensure()
    return paths
