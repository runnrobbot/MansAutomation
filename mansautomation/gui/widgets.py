"""Reusable Qt widgets and helpers."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFormLayout,
    QFrame,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


def make_label(text: str, *, object_name: str | None = None) -> QLabel:
    label = QLabel(text)
    if object_name:
        label.setObjectName(object_name)
    return label


def make_section_header(text: str) -> QLabel:
    label = make_label(text, object_name="sectionHeader")
    label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    return label


def make_title(text: str) -> QLabel:
    return make_label(text, object_name="title")


def make_muted(text: str) -> QLabel:
    label = make_label(text, object_name="muted")
    label.setWordWrap(True)
    return label


def make_primary_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setObjectName("primary")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def make_ghost_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setObjectName("ghost")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def make_danger_button(text: str) -> QPushButton:
    btn = QPushButton(text)
    btn.setObjectName("danger")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


class CardWidget(QFrame):
    """Container widget with vertical layout used as a card section."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(14)

    @property
    def layout_v(self) -> QVBoxLayout:
        return self._layout


def configure_form(layout: QFormLayout) -> None:
    """Apply consistent spacing/sizing to a QFormLayout."""

    layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    layout.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    # Wrap to a stacked layout when the window is narrow so labels never collide
    # with their fields. Combined with ``AllNonFixedFieldsGrow`` this keeps
    # labels readable at any viewport width.
    layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    layout.setHorizontalSpacing(18)
    layout.setVerticalSpacing(12)
    layout.setContentsMargins(4, 4, 4, 4)


def make_form_row(layout: QFormLayout, label: str, widget: QWidget) -> None:
    label_widget = make_label(label, object_name="fieldLabel")
    label_widget.setMinimumWidth(150)
    label_widget.setMaximumWidth(220)
    label_widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
    label_widget.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, widget.sizePolicy().verticalPolicy())
    widget.setMinimumWidth(220)
    layout.addRow(label_widget, widget)


def make_scroll_container(content: QWidget) -> QScrollArea:
    """Wrap a widget in a vertically-scrolling area with sane defaults."""

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    scroll.setWidget(content)
    return scroll
