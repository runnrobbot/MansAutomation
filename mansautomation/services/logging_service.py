"""Structured logging configuration."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

import structlog

from mansautomation.core.config import LoggingSettings


class LoggingService:
    """Configures structlog + stdlib logging for the entire application."""

    def __init__(self, settings: LoggingSettings, log_dir: Path) -> None:
        self._settings = settings
        self._log_dir = log_dir
        self._configured = False
        self._configure()

    def _configure(self) -> None:
        if self._configured:
            return
        log_level = getattr(logging, self._settings.level, logging.INFO)

        timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

        shared_processors: list[Any] = [
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            timestamper,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
        ]

        renderer: Any
        if self._settings.json_logs:
            renderer = structlog.processors.JSONRenderer()
        else:
            renderer = structlog.dev.ConsoleRenderer(colors=False)

        structlog.configure(
            processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
            wrapper_class=structlog.stdlib.BoundLogger,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=True,
        )

        formatter = structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processor=renderer,
        )

        root = logging.getLogger()
        root.handlers.clear()
        root.setLevel(log_level)

        stream_handler = logging.StreamHandler(stream=sys.stdout)
        stream_handler.setFormatter(formatter)
        stream_handler.setLevel(log_level)
        root.addHandler(stream_handler)

        if self._settings.log_to_file:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                self._log_dir / "mansautomation.log",
                maxBytes=self._settings.rotation_max_bytes,
                backupCount=self._settings.rotation_backups,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.setLevel(log_level)
            root.addHandler(file_handler)

        # Quiet noisy third-party loggers
        for noisy in ("playwright", "asyncio", "websockets"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

        self._configured = True

    def get_logger(self, name: str) -> structlog.stdlib.BoundLogger:
        return structlog.get_logger(name)

    def apply_settings(self, settings: LoggingSettings) -> None:
        """Re-apply settings live without restarting the application."""

        self._settings = settings
        log_level = getattr(logging, settings.level, logging.INFO)
        root = logging.getLogger()
        root.setLevel(log_level)
        for handler in root.handlers:
            handler.setLevel(log_level)
