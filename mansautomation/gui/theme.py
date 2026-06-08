"""Dark theme + global stylesheet for the application."""

from __future__ import annotations

from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication

_QSS = """
* { font-family: 'Segoe UI', 'Inter', system-ui, sans-serif; font-size: 13px; }
QMainWindow, QDialog, QWidget { background-color: #0f1115; color: #e6e8eb; }
QFrame#sidebar {
    background-color: #131722;
    border-right: 1px solid #1f2530;
}
QFrame#statusBarFrame { background-color: #11151c; border-top: 1px solid #1f2530; }
QListWidget, QTreeWidget, QTableWidget, QListView {
    background-color: #161b25;
    color: #e6e8eb;
    border: 1px solid #1f2530;
    border-radius: 6px;
    padding: 4px;
    selection-background-color: #2563eb;
    selection-color: #ffffff;
}
QPlainTextEdit, QTextEdit {
    background-color: #161b25;
    color: #e6e8eb;
    border: 1px solid #1f2530;
    border-radius: 6px;
    padding: 8px 10px;
    selection-background-color: #2563eb;
    selection-color: #ffffff;
}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateEdit, QDateTimeEdit, QTimeEdit {
    background-color: #161b25;
    color: #e6e8eb;
    border: 1px solid #1f2530;
    border-radius: 6px;
    padding: 6px 10px;
    min-height: 22px;
    selection-background-color: #2563eb;
    selection-color: #ffffff;
}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus,
QDateEdit:focus, QDateTimeEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {
    border-color: #2563eb;
}
QComboBox::drop-down { border: none; width: 22px; }
QComboBox::down-arrow { image: none; }
QCheckBox { spacing: 8px; padding: 4px 0; }
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #2a3340;
    border-radius: 3px;
    background-color: #161b25;
}
QCheckBox::indicator:checked {
    background-color: #2563eb;
    border-color: #2563eb;
}
QLabel { color: #c9cdd4; background: transparent; }
QLabel#sectionHeader { color: #8a93a4; text-transform: uppercase; letter-spacing: 1px; font-size: 11px; }
QLabel#title { font-size: 18px; font-weight: 600; color: #f5f7fa; }
QLabel#muted { color: #9aa2b1; }
QLabel#fieldLabel { color: #9aa2b1; padding-top: 8px; }
QPushButton {
    background-color: #1f2937;
    color: #f5f7fa;
    border: 1px solid #1f2530;
    border-radius: 6px;
    padding: 9px 16px;
    min-height: 18px;
}
QPushButton:hover { background-color: #2a3340; }
QPushButton:pressed { background-color: #1a2230; }
QPushButton:disabled { color: #5b6473; background-color: #161b25; }
QPushButton#primary { background-color: #2563eb; color: #ffffff; border-color: #2563eb; }
QPushButton#primary:hover { background-color: #1d4ed8; }
QPushButton#danger { background-color: #b91c1c; border-color: #b91c1c; }
QPushButton#danger:hover { background-color: #991b1b; }
QPushButton#ghost { background-color: transparent; border-color: #1f2530; }
QPushButton#ghost:hover { background-color: #1a2230; }
QGroupBox {
    border: 1px solid #1f2530;
    border-radius: 8px;
    margin-top: 18px;
    padding: 22px 16px 16px 16px;
    color: #c9cdd4;
    background-color: #11151c;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    padding: 0 6px;
    color: #8a93a4;
    background-color: #11151c;
}
QStatusBar { background-color: #11151c; color: #8a93a4; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #1f2530; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #2a3340; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; background: transparent; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 10px; }
QScrollBar::handle:horizontal { background: #1f2530; border-radius: 4px; min-width: 30px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; background: transparent; }
QTabBar::tab {
    background: transparent;
    color: #c9cdd4;
    padding: 8px 14px;
    border: none;
    border-bottom: 2px solid transparent;
}
QTabBar::tab:selected { color: #ffffff; border-bottom: 2px solid #2563eb; }
QTabWidget::pane { border: 1px solid #1f2530; border-radius: 8px; }
QHeaderView::section {
    background-color: #131722;
    color: #8a93a4;
    padding: 6px;
    border: none;
    border-bottom: 1px solid #1f2530;
}
QToolTip {
    color: #f5f7fa;
    background-color: #1f2937;
    border: 1px solid #2a3340;
    padding: 4px;
    border-radius: 4px;
}
QListWidget::item { padding: 8px; border-radius: 4px; }
QListWidget::item:selected { background-color: #1f2a44; color: #ffffff; }
QSplitter::handle { background-color: transparent; }
QSplitter::handle:horizontal { width: 6px; }
QSplitter::handle:vertical { height: 6px; }
"""


def apply_dark_theme(app: QApplication) -> None:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#0f1115"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#e6e8eb"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#161b25"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#131722"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#e6e8eb"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#1f2937"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#f5f7fa"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#2563eb"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#1f2937"))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#f5f7fa"))
    app.setPalette(palette)
    app.setStyleSheet(_QSS)
