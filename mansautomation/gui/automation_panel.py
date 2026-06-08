"""Automation control panel: select plugin/profile and run workflows."""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from mansautomation.automation.runner import (
    WORKFLOW_EVENT_TOPIC,
    WORKFLOW_STATUS_TOPIC,
    WorkflowRunner,
)
from mansautomation.core.container import Container
from mansautomation.core.events import EventBus
from mansautomation.core.models import Profile, WorkflowJob, WorkflowStatus
from mansautomation.gui.widgets import (
    configure_form,
    make_danger_button,
    make_form_row,
    make_ghost_button,
    make_muted,
    make_primary_button,
    make_scroll_container,
    make_section_header,
    make_title,
)
from mansautomation.plugins.base import AutomationPlugin
from mansautomation.plugins.manager import PLUGINS_TOPIC, PluginManager
from mansautomation.profiles.manager import PROFILES_TOPIC, ProfileManager
from mansautomation.utils.async_qt import run_async


class AutomationPanel(QWidget):
    event_received = pyqtSignal(dict)
    status_received = pyqtSignal(dict)
    profiles_changed = pyqtSignal(list)
    plugins_changed = pyqtSignal(list)

    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container
        self._runner: WorkflowRunner = container.resolve(WorkflowRunner)
        self._plugins: PluginManager = container.resolve(PluginManager)
        self._profiles: ProfileManager = container.resolve(ProfileManager)
        self._build_ui()
        bus: EventBus = container.resolve(EventBus)
        bus.subscribe(WORKFLOW_EVENT_TOPIC, self._on_event)
        bus.subscribe(WORKFLOW_STATUS_TOPIC, self._on_status)
        bus.subscribe(PROFILES_TOPIC, self._on_profiles_changed)
        bus.subscribe(PLUGINS_TOPIC, self._on_plugins_changed)
        self.event_received.connect(self._render_event)
        self.status_received.connect(self._render_status)
        self.profiles_changed.connect(self._render_profiles)
        self.plugins_changed.connect(self._render_plugins)

    async def load_initial_data(self) -> None:
        plugins = list(self._plugins.plugins.values())
        self.plugins_changed.emit(plugins)
        profiles = await self._profiles.list_profiles()
        self.profiles_changed.emit(profiles)

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(14)
        outer.addWidget(make_title("Automation"))
        outer.addWidget(
            make_section_header("Run a workflow against a target URL using a plugin and profile.")
        )

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 4, 0, 4)
        body_layout.setSpacing(16)
        body_layout.addWidget(self._build_workflow_group())
        body_layout.addWidget(self._build_status_group())
        body_layout.addWidget(self._build_logs_group(), stretch=1)

        outer.addWidget(make_scroll_container(body), stretch=1)

    def _build_workflow_group(self) -> QGroupBox:
        group = QGroupBox("Workflow")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(16, 24, 16, 16)
        layout.setSpacing(12)

        form = QFormLayout()
        configure_form(form)
        self._plugin_combo = QComboBox()
        self._plugin_combo.setMinimumWidth(260)
        self._profile_combo = QComboBox()
        self._profile_combo.setMinimumWidth(260)
        self._url_edit = QLineEdit()
        self._url_edit.setPlaceholderText("https://example.com/checkout")
        self._url_edit.setMinimumWidth(260)
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Event keyword (used by site plugins, e.g. tiket.com)")
        self._search_edit.setMinimumWidth(260)
        self._event_edit = QLineEdit()
        self._event_edit.setPlaceholderText("Event title (substring) - tiket.com only")
        self._event_edit.setMinimumWidth(260)
        self._package_edit = QLineEdit()
        self._package_edit.setPlaceholderText("Comma-separated fallback, e.g. 'CAT 1, VIP A, FESTIVAL'")
        self._package_edit.setMinimumWidth(260)
        self._quantity_spin = QSpinBox()
        self._quantity_spin.setRange(1, 20)
        self._quantity_spin.setValue(1)
        self._presale_wait = QCheckBox("Auto-wait for pre-sale & resume at sale time")
        self._presale_wait.setChecked(True)
        self._queue_wait = QCheckBox("Auto-wait in queue / waiting room")
        self._queue_wait.setChecked(True)
        self._login_first = QCheckBox("Sign in (use profile login credentials)")
        self._login_first.setChecked(True)
        for label, widget in (
            ("Plugin", self._plugin_combo),
            ("Profile", self._profile_combo),
            ("Target URL", self._url_edit),
            ("Search query", self._search_edit),
            ("Event title", self._event_edit),
            ("Package", self._package_edit),
            ("Quantity", self._quantity_spin),
            ("Pre-sale", self._presale_wait),
            ("Queue", self._queue_wait),
            ("Login", self._login_first),
        ):
            make_form_row(form, label, widget)
        layout.addLayout(form)

        button_row = QHBoxLayout()
        button_row.setSpacing(10)
        self._start_btn = make_primary_button("Start workflow")
        self._search_btn = make_primary_button("Search events")
        self._buy_btn = make_primary_button("Buy ticket")
        self._abort_btn = make_danger_button("Abort")
        self._abort_btn.setEnabled(False)
        self._human_btn = make_ghost_button("I'm done - resume")
        self._human_btn.setEnabled(False)
        for btn in (
            self._start_btn,
            self._search_btn,
            self._buy_btn,
            self._abort_btn,
            self._human_btn,
        ):
            btn.setMinimumHeight(36)
            button_row.addWidget(btn)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self._start_btn.clicked.connect(self._on_start)
        self._search_btn.clicked.connect(self._on_search)
        self._buy_btn.clicked.connect(self._on_buy)
        self._abort_btn.clicked.connect(self._on_abort)
        self._human_btn.clicked.connect(self._on_human_resume)
        return group

    def _build_status_group(self) -> QGroupBox:
        group = QGroupBox("Status")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(16, 24, 16, 16)
        layout.setSpacing(6)
        self._status_label = QLabel("Idle")
        self._status_label.setObjectName("title")
        self._status_detail = make_muted("No active workflow.")
        layout.addWidget(self._status_label)
        layout.addWidget(self._status_detail)
        return group

    def _build_logs_group(self) -> QGroupBox:
        group = QGroupBox("Real-time logs")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(16, 24, 16, 16)
        layout.setSpacing(6)
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(2000)
        self._log_view.setMinimumHeight(160)
        layout.addWidget(self._log_view)
        return group

    def _on_profiles_changed(self, profiles: list[Profile]) -> None:
        self.profiles_changed.emit(list(profiles))

    def _on_plugins_changed(self, plugins: list[AutomationPlugin]) -> None:
        self.plugins_changed.emit(list(plugins))

    def _render_profiles(self, profiles: list[Profile]) -> None:
        current = self._profile_combo.currentData()
        self._profile_combo.blockSignals(True)
        self._profile_combo.clear()
        for profile in profiles:
            self._profile_combo.addItem(profile.name, profile.id)
        if current:
            index = self._profile_combo.findData(current)
            if index >= 0:
                self._profile_combo.setCurrentIndex(index)
        self._profile_combo.blockSignals(False)

    def _render_plugins(self, plugins: list[AutomationPlugin]) -> None:
        current = self._plugin_combo.currentData()
        self._plugin_combo.blockSignals(True)
        self._plugin_combo.clear()
        for plugin in plugins:
            self._plugin_combo.addItem(
                f"{plugin.metadata.name} (v{plugin.metadata.version})",
                plugin.metadata.id,
            )
        if current:
            index = self._plugin_combo.findData(current)
            if index >= 0:
                self._plugin_combo.setCurrentIndex(index)
        self._plugin_combo.blockSignals(False)
        if not self._url_edit.text().strip():
            plugin = self._plugins.get(str(self._plugin_combo.currentData() or ""))
            default_url = self._default_url_for_plugin(plugin)
            if default_url:
                self._url_edit.setPlaceholderText(default_url)

    def _on_start(self) -> None:
        plugin_id = self._plugin_combo.currentData()
        profile_id = self._profile_combo.currentData()
        url = self._normalize_url(self._url_edit.text())
        if not plugin_id or not profile_id:
            QMessageBox.warning(self, "Missing inputs", "Select a plugin and a profile.")
            return
        if not url:
            QMessageBox.warning(self, "Missing URL", "Provide a target URL.")
            return
        if self._runner.is_running:
            QMessageBox.information(self, "Workflow running", "A workflow is already in progress.")
            return
        parameters: dict[str, Any] = {
            "login": self._login_first.isChecked(),
        }
        query = self._search_edit.text().strip()
        if query:
            parameters["search_query"] = query
            parameters["action"] = "search"
        job = WorkflowJob(
            plugin_id=str(plugin_id),
            profile_id=str(profile_id),
            target_url=url,
            parameters=parameters,
        )
        run_async(self._submit(job))

    def _on_search(self) -> None:
        plugin_id = self._plugin_combo.currentData()
        profile_id = self._profile_combo.currentData()
        query = self._search_edit.text().strip()
        if not plugin_id or not profile_id:
            QMessageBox.warning(self, "Missing inputs", "Select a plugin and a profile.")
            return
        if not query:
            QMessageBox.warning(self, "Missing query", "Enter an event keyword to search.")
            return
        if self._runner.is_running:
            QMessageBox.information(self, "Workflow running", "A workflow is already in progress.")
            return
        plugin = self._plugins.get(str(plugin_id))
        url = self._normalize_url(self._url_edit.text())
        if not url:
            url = self._default_url_for_plugin(plugin)
        if not url:
            QMessageBox.warning(
                self,
                "Missing URL",
                "Provide a target URL or pick a plugin that ships a default landing page.",
            )
            return
        job = WorkflowJob(
            plugin_id=str(plugin_id),
            profile_id=str(profile_id),
            target_url=url,
            parameters={
                "action": "search",
                "search_query": query,
                "login": self._login_first.isChecked(),
            },
        )
        run_async(self._submit(job))

    def _on_buy(self) -> None:
        plugin_id = self._plugin_combo.currentData()
        profile_id = self._profile_combo.currentData()
        if not plugin_id or not profile_id:
            QMessageBox.warning(self, "Missing inputs", "Select a plugin and a profile.")
            return
        event_title = self._event_edit.text().strip()
        package_text = self._package_edit.text().strip()
        category_text = self._category_edit.text().strip()
        query = self._search_edit.text().strip()
        packages = [p.strip() for p in package_text.split(",") if p.strip()]
        categories = [c.strip() for c in category_text.split(",") if c.strip()]
        if not event_title and not query:
            QMessageBox.warning(
                self,
                "Missing event",
                "Enter an event keyword or an event title for the booking workflow.",
            )
            return
        if not packages:
            QMessageBox.warning(
                self,
                "Missing package",
                "Specify at least one package row, e.g. 'CAT 1 RIGHT - GENERAL SALE'. "
                "Use a comma-separated list to fall back across packages.",
            )
            return
        if self._runner.is_running:
            QMessageBox.information(self, "Workflow running", "A workflow is already in progress.")
            return
        plugin = self._plugins.get(str(plugin_id))
        url = self._normalize_url(self._url_edit.text()) or self._default_url_for_plugin(plugin)
        if not url:
            QMessageBox.warning(self, "Missing URL", "Provide a target URL.")
            return
        job = WorkflowJob(
            plugin_id=str(plugin_id),
            profile_id=str(profile_id),
            target_url=url,
            parameters={
                "action": "book",
                "search_query": query or event_title,
                "event_title": event_title,
                "packages": packages,
                "categories": categories,
                # Backward-compatible single-value fields used by older plugin versions:
                "package": packages[0],
                "category": categories[0] if categories else "",
                "quantity": int(self._quantity_spin.value()),
                "login": self._login_first.isChecked(),
                "presale_wait": self._presale_wait.isChecked(),
                "presale_max_wait_minutes": int(self._presale_max.value()),
                "queue_wait": self._queue_wait.isChecked(),
                "queue_max_wait_minutes": int(self._queue_max.value()),
            },
        )
        run_async(self._submit(job))

    @staticmethod
    def _default_url_for_plugin(plugin: AutomationPlugin | None) -> str:
        if plugin is None:
            return ""
        if plugin.metadata.id == "tiket.com":
            return "https://www.tiket.com/"
        for domain in plugin.metadata.target_domains:
            if domain:
                return f"https://www.{domain.lstrip('www.')}"
        return ""

    @staticmethod
    def _normalize_url(value: str) -> str:
        """Best-effort fix for missing scheme. Returns '' for empty input."""

        candidate = (value or "").strip()
        if not candidate:
            return ""
        if candidate.startswith(("http://", "https://")):
            return candidate
        if candidate.startswith("//"):
            return "https:" + candidate
        # Bare domain or path: prepend https://. Don't prefix www. so the
        # caller can supply 'tiket.com', 'www.tiket.com', or 'subdomain.tiket.com'.
        return "https://" + candidate.lstrip("/")

    async def _submit(self, job: WorkflowJob) -> None:
        try:
            await self._runner.submit(job)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Failed to start", str(exc))
            return
        self._abort_btn.setEnabled(True)
        self._start_btn.setEnabled(False)

    def _on_abort(self) -> None:
        run_async(self._runner.abort())

    def _on_human_resume(self) -> None:
        self._runner.acknowledge_human()
        self._human_btn.setEnabled(False)

    def _on_event(self, payload: dict[str, Any]) -> None:
        self.event_received.emit(payload)

    def _on_status(self, payload: dict[str, Any]) -> None:
        self.status_received.emit(payload)

    def _render_event(self, payload: dict[str, Any]) -> None:
        event = payload.get("event") or {}
        timestamp = event.get("timestamp", "")
        level = event.get("level", "info").upper()
        message = event.get("message", "")
        line = f"[{timestamp}] {level:<5} {message}"
        self._log_view.appendPlainText(line)

    def _render_status(self, payload: dict[str, Any]) -> None:
        status = payload.get("status", "idle")
        self._status_label.setText(status.replace("_", " ").title())
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        if status == WorkflowStatus.HUMAN_REQUIRED.value:
            signal = self._runner.human_signal
            detail = signal.detail if signal else "Manual interaction required."
            self._status_detail.setText(detail)
            self._human_btn.setEnabled(True)
        elif status in {
            WorkflowStatus.COMPLETED.value,
            WorkflowStatus.FAILED.value,
            WorkflowStatus.ABORTED.value,
        }:
            self._status_detail.setText(f"Workflow ended: {status}.")
            self._abort_btn.setEnabled(False)
            self._start_btn.setEnabled(True)
            self._human_btn.setEnabled(False)
        elif status == WorkflowStatus.RUNNING.value:
            self._status_detail.setText("Workflow in progress.")
            self._human_btn.setEnabled(False)
        elif status == WorkflowStatus.STARTING.value:
            self._status_detail.setText("Starting workflow...")
