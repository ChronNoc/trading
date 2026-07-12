"""Dark trading-terminal stylesheet for the MNQ assistant GUI.

One QSS string applied to the main window. Colors follow a slate/blue
terminal palette: dark panels, cyan group titles, blue primary buttons,
green/red reserved strictly for accepted/rejected decision states.
"""

APP_STYLESHEET = """
QMainWindow, QWidget {
    background-color: #0f172a;
    color: #e2e8f0;
    font-size: 13px;
}
QLabel#window_title {
    color: #f8fafc;
}
QGroupBox {
    border: 1px solid #1e293b;
    border-radius: 8px;
    margin-top: 14px;
    padding: 8px;
    background-color: #111c33;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: #7dd3fc;
}
QTabWidget::pane {
    border: 1px solid #1e293b;
    border-radius: 6px;
}
QTabBar::tab {
    background: #111c33;
    color: #94a3b8;
    padding: 7px 14px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
    margin-right: 2px;
}
QTabBar::tab:selected {
    background: #1d4ed8;
    color: #ffffff;
}
QPushButton {
    background-color: #1d4ed8;
    color: white;
    border: none;
    border-radius: 6px;
    padding: 6px 12px;
    font-weight: 600;
}
QPushButton:hover {
    background-color: #2563eb;
}
QPushButton:disabled {
    background-color: #1e293b;
    color: #64748b;
}
QLineEdit, QTextEdit, QSpinBox, QComboBox {
    background-color: #0b1220;
    border: 1px solid #1e293b;
    border-radius: 6px;
    padding: 4px 6px;
    color: #e2e8f0;
    selection-background-color: #1d4ed8;
}
QTableWidget, QListWidget {
    background-color: #0b1220;
    border: 1px solid #1e293b;
    border-radius: 6px;
    gridline-color: #1e293b;
}
QHeaderView::section {
    background-color: #111c33;
    color: #94a3b8;
    border: none;
    padding: 4px;
    font-weight: 600;
}
QSlider::groove:horizontal {
    height: 6px;
    background: #1e293b;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    background: #38bdf8;
    width: 14px;
    margin: -5px 0;
    border-radius: 7px;
}
QCheckBox {
    spacing: 8px;
}
"""

BANNER_STYLE = "font-weight: 700; color: #fb923c;"
WATCHDOG_OK_STYLE = "color: #4ade80;"
WATCHDOG_INFO_STYLE = "color: #94a3b8;"
WATCHDOG_WARNING_STYLE = "color: #f87171; font-weight: 600;"
AUTOMATION_BANNER_STYLE = (
    "background-color: #172554; color: #93c5fd; border: 1px solid #1d4ed8; "
    "border-radius: 6px; padding: 4px 10px; font-weight: 600;"
)
COACH_STYLE = "color: #fbbf24; font-weight: 600;"
