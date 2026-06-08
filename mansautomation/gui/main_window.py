"""Top-level main window with sidebar navigation."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from mansautomation.core.container import Container
from mansautomation.gui.automation_panel import AutomationPanel
from mansautomation.gui.notifications_panel import NotificationsPanel
from mansautomation.gui.profile_panel import ProfilePanel
from mansautomation.gui.settings_panel import SettingsPanel
from mansautomation.gui.widgets import make_muted, make_section_header
from mansautomation.utils.async_qt import run_async


class MainWindow(QMainWindow):
    def __init__(self, container: Container) -> None:
        super().__init__()
        self._container = container
        self.setWindowTitle("MansAutomation")
        self.resize(1280, 820)
        self.setMinimumSize(900, 600)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        sidebar = self._build_sidebar()
        layout.addWidget(sidebar, stretch=0)

        self._stack = QStackedWidget()
        layout.addWidget(self._stack, stretch=1)

        self._automation_panel = AutomationPanel(container)
        self._profile_panel = ProfilePanel(container)
        self._notifications_panel = NotificationsPanel(container)
        self._settings_panel = SettingsPanel(container)

        for widget in (
            self._automation_panel,
            self._profile_panel,
            self._notifications_panel,
            self._settings_panel,
        ):
            self._stack.addWidget(widget)

        self._sidebar_list.currentRowChanged.connect(self._stack.setCurrentIndex)
        self._sidebar_list.setCurrentRow(0)

        self.setCentralWidget(central)

        status_bar = QStatusBar()
        status_bar.showMessage("MansAutomation - ready")
        self.setStatusBar(status_bar)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(220)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 20, 18, 20)
        layout.setSpacing(12)

        title = QLabel("MansAutomation")
        title.setObjectName("title")
        layout.addWidget(title)
        layout.addWidget(make_muted("Desktop automation assistant"))
        layout.addWidget(make_section_header("Sections"))

        self._sidebar_list = QListWidget()
        self._sidebar_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        for label in ("Automation", "Profiles", "Notifications", "Settings"):
            item = QListWidgetItem(label)
            self._sidebar_list.addItem(item)
        self._sidebar_list.setStyleSheet(
            "QListWidget { border: none; background: transparent; padding: 4px 0; }"
        )
        layout.addWidget(self._sidebar_list, stretch=1)

        layout.addWidget(make_muted("v1.0.0"), alignment=Qt.AlignmentFlag.AlignBottom)
        return sidebar

    async def load_initial_data(self) -> None:
        """Run after the container's async services have started."""

        await self._profile_panel.load_initial_data()
        await self._automation_panel.load_initial_data()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        run_async(self._container.stop_async_services())
        super().closeEvent(event)
