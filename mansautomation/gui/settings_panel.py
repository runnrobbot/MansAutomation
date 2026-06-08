"""Settings panel for application configuration."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from pydantic import SecretStr

from mansautomation.core.config import AppSettings, SETTINGS_CHANGED_TOPIC, save_settings
from mansautomation.core.container import Container
from mansautomation.core.events import EventBus
from mansautomation.core.paths import AppPaths
from mansautomation.gui.widgets import (
    configure_form,
    make_form_row,
    make_primary_button,
    make_scroll_container,
    make_section_header,
    make_title,
)
from mansautomation.utils.async_qt import run_async


class SettingsPanel(QWidget):
    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container
        self._settings: AppSettings = container.resolve(AppSettings)
        self._paths: AppPaths = container.resolve(AppPaths)
        self._build_ui()
        self._load_values()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(14)
        outer.addWidget(make_title("Settings"))
        outer.addWidget(
            make_section_header("Configure browser, workflow, and notification behaviour.")
        )

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 4, 0, 4)
        body_layout.setSpacing(18)

        body_layout.addWidget(self._build_browser_group())
        body_layout.addWidget(self._build_workflow_group())
        body_layout.addWidget(self._build_notifications_group())
        body_layout.addWidget(self._build_logging_group())

        save_btn = make_primary_button("Save settings")
        save_btn.setMinimumHeight(38)
        save_btn.clicked.connect(self._on_save)
        body_layout.addWidget(save_btn)
        body_layout.addStretch(1)

        outer.addWidget(make_scroll_container(body), stretch=1)

    def _build_browser_group(self) -> QGroupBox:
        group = QGroupBox("Browser")
        form = QFormLayout(group)
        configure_form(form)

        self._engine = QComboBox()
        self._engine.addItems(["chromium", "firefox", "webkit"])
        self._headless = QCheckBox("Run headless")
        self._persistent = QCheckBox("Persistent session")
        self._block_resources = QLineEdit()
        self._block_resources.setPlaceholderText("comma-separated, e.g. image,font,media")
        self._user_agent = QLineEdit()
        self._user_agent.setPlaceholderText("Optional user agent override")
        self._proxy = QLineEdit()
        self._proxy.setPlaceholderText("http://user:pass@host:port (optional)")

        for label, widget in (
            ("Engine", self._engine),
            ("Headless", self._headless),
            ("Persistent session", self._persistent),
            ("Block resource types", self._block_resources),
            ("User agent override", self._user_agent),
            ("Proxy URL", self._proxy),
        ):
            make_form_row(form, label, widget)
        return group

    def _build_workflow_group(self) -> QGroupBox:
        group = QGroupBox("Workflow")
        form = QFormLayout(group)
        configure_form(form)

        self._typing_delay = QSpinBox()
        self._typing_delay.setRange(0, 200)
        self._typing_delay.setSuffix(" ms")
        self._inter_delay = QSpinBox()
        self._inter_delay.setRange(0, 1000)
        self._inter_delay.setSuffix(" ms")
        self._max_attempts = QSpinBox()
        self._max_attempts.setRange(1, 25)
        self._auto_recover = QCheckBox("Automatically retry on transient errors")
        self._sync_speed = QComboBox()
        self._sync_speed.addItem("Fast (0.5x waits) - fast network", 0.5)
        self._sync_speed.addItem("Normal (1.0x waits) - default", 1.0)
        self._sync_speed.addItem("Patient (1.5x waits) - slow network", 1.5)
        self._sync_speed.addItem("Very patient (2.0x waits) - very slow", 2.0)

        for label, widget in (
            ("Typing delay", self._typing_delay),
            ("Inter-field delay", self._inter_delay),
            ("Max retry attempts", self._max_attempts),
            ("Auto-recover", self._auto_recover),
            ("Sync wait speed", self._sync_speed),
        ):
            make_form_row(form, label, widget)
        return group

    def _build_notifications_group(self) -> QGroupBox:
        group = QGroupBox("Notifications")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(16, 24, 16, 16)
        layout.setSpacing(12)

        form = QFormLayout()
        configure_form(form)

        self._desktop_enabled = QCheckBox("Show desktop notifications")
        self._desktop_sound = QCheckBox("Play sound alerts")
        self._telegram_enabled = QCheckBox("Enable Telegram")
        self._telegram_token = QLineEdit()
        self._telegram_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._telegram_token.setPlaceholderText("Bot token")
        self._telegram_chat = QLineEdit()
        self._telegram_chat.setPlaceholderText("Chat ID")
        self._discord_enabled = QCheckBox("Enable Discord")
        self._discord_webhook = QLineEdit()
        self._discord_webhook.setEchoMode(QLineEdit.EchoMode.Password)
        self._discord_webhook.setPlaceholderText("Webhook URL")

        for label, widget in (
            ("Desktop", self._desktop_enabled),
            ("Sound", self._desktop_sound),
            ("Telegram", self._telegram_enabled),
            ("Telegram bot token", self._telegram_token),
            ("Telegram chat ID", self._telegram_chat),
            ("Discord", self._discord_enabled),
            ("Discord webhook URL", self._discord_webhook),
        ):
            make_form_row(form, label, widget)
        layout.addLayout(form)

        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        self._test_desktop_btn = QPushButton("Test desktop")
        self._test_telegram_btn = QPushButton("Test Telegram")
        self._test_discord_btn = QPushButton("Test Discord")
        for btn in (self._test_desktop_btn, self._test_telegram_btn, self._test_discord_btn):
            btn.setMinimumHeight(32)
            button_row.addWidget(btn)
        button_row.addStretch(1)
        self._test_desktop_btn.clicked.connect(lambda: self._on_test_channel("desktop"))
        self._test_telegram_btn.clicked.connect(lambda: self._on_test_channel("telegram"))
        self._test_discord_btn.clicked.connect(lambda: self._on_test_channel("discord"))
        layout.addLayout(button_row)
        return group

    def _build_logging_group(self) -> QGroupBox:
        group = QGroupBox("Logging")
        form = QFormLayout(group)
        configure_form(form)
        self._log_level = QComboBox()
        self._log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
        self._log_json = QCheckBox("Emit JSON logs (machine-readable)")
        self._log_to_file = QCheckBox("Write logs to file (rotating)")
        for label, widget in (
            ("Log level", self._log_level),
            ("JSON logs", self._log_json),
            ("Log to file", self._log_to_file),
        ):
            make_form_row(form, label, widget)
        return group

    def _build_logging_group(self) -> QGroupBox:
        group = QGroupBox("Logging")
        form = QFormLayout(group)
        configure_form(form)
        self._log_level = QComboBox()
        self._log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
        self._log_json = QCheckBox("Emit JSON logs (machine-readable)")
        self._log_to_file = QCheckBox("Write logs to file (rotating)")
        for label, widget in (
            ("Log level", self._log_level),
            ("JSON logs", self._log_json),
            ("Log to file", self._log_to_file),
        ):
            make_form_row(form, label, widget)
        return group

    def _load_values(self) -> None:
        b = self._settings.browser
        idx = self._engine.findText(b.engine)
        self._engine.setCurrentIndex(idx if idx >= 0 else 0)
        self._headless.setChecked(b.headless)
        self._persistent.setChecked(b.persistent_session)
        self._block_resources.setText(",".join(b.block_resources))
        self._user_agent.setText(b.user_agent or "")
        self._proxy.setText(b.proxy_url or "")

        w = self._settings.workflow
        self._typing_delay.setValue(w.field_typing_delay_ms)
        self._inter_delay.setValue(w.inter_field_delay_ms)
        self._max_attempts.setValue(self._settings.retry.max_attempts)
        self._auto_recover.setChecked(w.auto_recover)
        idx = self._sync_speed.findData(w.sync_speed_multiplier)
        if idx < 0:
            # Match the closest preset to whatever value is on disk.
            idx = min(
                range(self._sync_speed.count()),
                key=lambda i: abs(self._sync_speed.itemData(i) - w.sync_speed_multiplier),
            )
        self._sync_speed.setCurrentIndex(idx)

        n = self._settings.notifications
        self._desktop_enabled.setChecked(n.desktop.enabled)
        self._desktop_sound.setChecked(n.desktop.sound_enabled)
        self._telegram_enabled.setChecked(n.telegram.enabled)
        self._telegram_token.setText(
            n.telegram.bot_token.get_secret_value() if n.telegram.bot_token else ""
        )
        self._telegram_chat.setText(n.telegram.chat_id or "")
        self._discord_enabled.setChecked(n.discord.enabled)
        self._discord_webhook.setText(
            n.discord.webhook_url.get_secret_value() if n.discord.webhook_url else ""
        )

        log = self._settings.logging
        idx = self._log_level.findText(log.level)
        self._log_level.setCurrentIndex(idx if idx >= 0 else 1)
        self._log_json.setChecked(log.json_logs)
        self._log_to_file.setChecked(log.log_to_file)

    def _on_save(self) -> None:
        try:
            self._collect_into_settings()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Settings", f"Invalid value: {exc}")
            return
        try:
            save_settings(self._paths.settings_path, self._settings)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Settings", f"Failed to save: {exc}")
            return
        bus: EventBus = self._container.resolve(EventBus)
        run_async(bus.publish(SETTINGS_CHANGED_TOPIC, self._settings))
        QMessageBox.information(
            self,
            "Settings",
            "Settings saved and applied. Browser will relaunch on the next workflow.",
        )

    def _on_test_channel(self, channel: str) -> None:
        from mansautomation.notifications.base import Notification, NotificationLevel
        from mansautomation.notifications.dispatcher import NotificationDispatcher

        # Persist + apply current values so the channel uses the latest creds.
        try:
            self._collect_into_settings()
            save_settings(self._paths.settings_path, self._settings)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Settings", f"Failed to save: {exc}")
            return
        bus: EventBus = self._container.resolve(EventBus)
        dispatcher: NotificationDispatcher = self._container.resolve(NotificationDispatcher)
        notification = Notification(
            title="MansAutomation test",
            message=f"This is a test {channel} notification.",
            level=NotificationLevel.INFO,
        )

        async def _run() -> None:
            await bus.publish(SETTINGS_CHANGED_TOPIC, self._settings)
            for ch in dispatcher.channels:
                if ch.name != channel:
                    continue
                if not ch.enabled:
                    QMessageBox.warning(
                        self,
                        "Channel disabled",
                        f"The {channel} channel is not enabled or missing credentials.",
                    )
                    return
                try:
                    await ch.send(notification)
                except Exception as exc:  # noqa: BLE001
                    QMessageBox.critical(self, "Notification failed", f"{channel}: {exc}")
                    return
                QMessageBox.information(
                    self, "Notification sent", f"Test notification sent via {channel}."
                )
                return
            QMessageBox.warning(
                self,
                "Channel not configured",
                f"No active {channel} channel is registered.",
            )

        run_async(_run())

    def _collect_into_settings(self) -> None:
        """Pull every field from the UI into ``self._settings`` in place."""

        self._settings.browser.engine = self._engine.currentText()  # type: ignore[assignment]
        self._settings.browser.headless = self._headless.isChecked()
        self._settings.browser.persistent_session = self._persistent.isChecked()
        self._settings.browser.block_resources = [
            entry.strip() for entry in self._block_resources.text().split(",") if entry.strip()
        ]
        self._settings.browser.user_agent = self._user_agent.text().strip() or None
        self._settings.browser.proxy_url = self._proxy.text().strip() or None
        self._settings.workflow.field_typing_delay_ms = self._typing_delay.value()
        self._settings.workflow.inter_field_delay_ms = self._inter_delay.value()
        self._settings.retry.max_attempts = self._max_attempts.value()
        self._settings.workflow.auto_recover = self._auto_recover.isChecked()
        self._settings.workflow.sync_speed_multiplier = float(self._sync_speed.currentData() or 1.0)
        self._settings.notifications.desktop.enabled = self._desktop_enabled.isChecked()
        self._settings.notifications.desktop.sound_enabled = self._desktop_sound.isChecked()
        self._settings.notifications.telegram.enabled = self._telegram_enabled.isChecked()
        self._settings.notifications.telegram.bot_token = (
            SecretStr(self._telegram_token.text()) if self._telegram_token.text() else None
        )
        self._settings.notifications.telegram.chat_id = self._telegram_chat.text().strip() or None
        self._settings.notifications.discord.enabled = self._discord_enabled.isChecked()
        self._settings.notifications.discord.webhook_url = (
            SecretStr(self._discord_webhook.text()) if self._discord_webhook.text() else None
        )
        self._settings.logging.level = self._log_level.currentText()  # type: ignore[assignment]
        self._settings.logging.json_logs = self._log_json.isChecked()
        self._settings.logging.log_to_file = self._log_to_file.isChecked()
