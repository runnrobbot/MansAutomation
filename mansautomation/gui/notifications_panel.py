"""Notification center panel."""

from __future__ import annotations

from datetime import datetime, timezone

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from mansautomation.core.container import Container
from mansautomation.core.events import EventBus
from mansautomation.gui.widgets import make_muted, make_section_header, make_title
from mansautomation.notifications.base import Notification, NotificationLevel
from mansautomation.notifications.dispatcher import NOTIFICATION_TOPIC


class NotificationsPanel(QWidget):
    notification_received = pyqtSignal(object)

    def __init__(self, container: Container, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._container = container
        self._build_ui()
        bus: EventBus = container.resolve(EventBus)
        bus.subscribe(NOTIFICATION_TOPIC, self._on_received)
        self.notification_received.connect(self._render)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)
        layout.addWidget(make_title("Notifications"))
        layout.addWidget(make_section_header("Recent application notifications"))
        layout.addWidget(make_muted("All triggers are also dispatched to your configured channels."))
        self._list = QListWidget()
        self._list.setMinimumHeight(360)
        layout.addWidget(self._list, stretch=1)

    def _on_received(self, notification: Notification) -> None:
        self.notification_received.emit(notification)

    def _render(self, notification: Notification) -> None:
        timestamp = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
        item = QListWidgetItem(f"[{timestamp}] {notification.title} - {notification.message}")
        item.setData(Qt.ItemDataRole.UserRole, notification.level.value)
        if notification.level == NotificationLevel.ERROR:
            item.setForeground(Qt.GlobalColor.red)
        elif notification.level == NotificationLevel.WARNING:
            item.setForeground(Qt.GlobalColor.yellow)
        elif notification.level == NotificationLevel.SUCCESS:
            item.setForeground(Qt.GlobalColor.green)
        self._list.insertItem(0, item)
        if self._list.count() > 200:
            self._list.takeItem(self._list.count() - 1)
