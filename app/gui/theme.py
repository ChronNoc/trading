"""Theme-owned stylesheets for the snapshot-only MNQ assistant GUI.

``DASHBOARD_DARK`` and ``DASHBOARD_LIGHT`` are the active palettes. Reusable
widgets retain only semantic roles, layout, and accessibility behavior; these
stylesheets own their colors, surfaces, borders, and state presentation.
``APP_STYLESHEET`` remains solely for the legacy window.

Design language (2026 refresh): one restrained blue accent, flat surfaces,
neutral hairline borders, desaturated status pills, and generous radius - no
gradients, glows, or neon. Cyan/violet stay only in chart data series.
"""

APP_STYLESHEET = """
QMainWindow, QWidget {
    background-color: #0c0e12;
    color: #e8eaee;
    font-size: 13px;
}
QLabel#window_title {
    color: #f4f6f9;
}
QGroupBox {
    border: 1px solid #23262e;
    border-radius: 12px;
    margin-top: 14px;
    padding: 10px;
    background-color: #14161c;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
    color: #9aa0ab;
}
QTabWidget::pane {
    border: 1px solid #23262e;
    border-radius: 10px;
}
QTabBar::tab {
    background: transparent;
    color: #9aa0ab;
    padding: 7px 14px;
    border-radius: 8px;
    margin-right: 4px;
}
QTabBar::tab:selected {
    background: #18223f;
    color: #cdd8ff;
}
QPushButton {
    background-color: #5b8cff;
    color: white;
    border: none;
    border-radius: 9px;
    padding: 8px 14px;
    font-weight: 600;
}
QPushButton:hover {
    background-color: #6f9bff;
}
QPushButton:disabled {
    background-color: #222733;
    color: #565c66;
}
QLineEdit, QTextEdit, QSpinBox, QComboBox {
    background-color: #0f1116;
    border: 1px solid #2a2e38;
    border-radius: 9px;
    padding: 6px 9px;
    color: #e8eaee;
    selection-background-color: #5b8cff;
}
QTableWidget, QListWidget {
    background-color: #101217;
    border: 1px solid #23262e;
    border-radius: 10px;
    gridline-color: #1c1f27;
}
QHeaderView::section {
    background-color: #14161c;
    color: #8b919c;
    border: none;
    padding: 6px;
    font-weight: 500;
}
QSlider::groove:horizontal {
    height: 6px;
    background: #23262e;
    border-radius: 3px;
}
QSlider::handle:horizontal {
    background: #5b8cff;
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
    "background-color: #18223f; color: #9fb7ff; border: 1px solid #2d3b63; "
    "border-radius: 9px; padding: 5px 11px; font-weight: 600;"
)
COACH_STYLE = "color: #e6bd6a; font-weight: 600;"


DASHBOARD_DARK = """
QWidget { background:#0c0e12; color:#e8eaee; }
QWidget#root { background:#0c0e12; }
QLabel, QCheckBox, QRadioButton, QWidget[role="plain"] { background:transparent; }
QFrame[role="card"] { background:#14161c; border:1px solid #23262e; border-radius:12px; }
QFrame[role="stat_tile"] { background:#101217; border:1px solid #1e2129; border-radius:12px; }
QFrame[role="empty_state"] { background:#101217; border:1px dashed #3a4150; border-radius:12px; }
QLabel[role="headline"] { font-weight:600; color:#f0f2f5; }
QLabel[role="section_title"] { font-weight:600; color:#e8eaee; }
QLabel[role="metric"] { font-weight:600; color:#f0f2f5; }
QLabel[role="meter_value"] { color:#e8eaee; font-weight:500; }
QLabel[role="caption"], QLabel[role="muted"] { color:#8b919c; }
QLabel[role="page_subtitle"] { color:#cbd5e1; }
QLabel[state="ok"] { background:#13241d; color:#5fd3a0; border:1px solid #214034; border-radius:999px; padding:3px 11px; }
QLabel[state="warn"] { background:#272014; color:#e6bd6a; border:1px solid #45391f; border-radius:999px; padding:3px 11px; }
QLabel[state="fail"] { background:#271619; color:#ef8a93; border:1px solid #48272c; border-radius:999px; padding:3px 11px; }
QLabel[state="locked"] { background:#2a2113; color:#e8c06a; border:1px solid #5a4a24; border-radius:999px; padding:3px 11px; font-weight:600; }
QLabel[state="neutral"] { background:#181b22; color:#aab0ba; border:1px solid #282c35; border-radius:999px; padding:3px 11px; }
QProgressBar { background:#0f1116; border:1px solid #23262e; border-radius:999px; }
QProgressBar::chunk { background:#5b8cff; border-radius:999px; }
QTableWidget { background:#101217; alternate-background-color:#131620; border:1px solid #23262e; border-radius:10px; gridline-color:#1c1f27; }
QTableWidget::item { padding:5px 8px; color:#dfe2e8; border:0; }
QTableWidget::item:selected { background:#1d2a4d; color:#ffffff; }
QHeaderView::section { background:#14161c; color:#8b919c; border:0; border-bottom:1px solid #23262e; padding:7px 8px; font-weight:500; }
QListWidget#sidebar { background:#0a0b0f; border:0; border-right:1px solid #1a1d23; padding:10px 8px; outline:0; }
QListWidget#sidebar::item { min-height:36px; padding:0 12px; border-radius:9px; color:#9aa0ab; }
QListWidget#sidebar::item:selected { background:#18223f; color:#9fb7ff; }
QListWidget#sidebar::item:hover { background:#14161c; }
QStatusBar { background:#0a0b0f; border-top:1px solid #1a1d23; color:#8b919c; }
QGroupBox { background:#14161c; border:1px solid #23262e; border-radius:12px; margin-top:14px; padding:12px 12px 12px; font-weight:600; color:#e8eaee; }
QGroupBox::title { subcontrol-origin:margin; left:12px; top:1px; padding:0 4px; color:#9aa0ab; }
QGroupBox::indicator { width:15px; height:15px; }
QTabWidget::pane { border:1px solid #23262e; border-radius:10px; top:-1px; }
QTabBar::tab { background:transparent; color:#9aa0ab; padding:7px 14px; margin-right:4px; border:0; border-radius:8px; }
QTabBar::tab:selected { background:#18223f; color:#cdd8ff; }
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox { background:#0f1116; border:1px solid #2a2e38; border-radius:9px; padding:6px 9px; color:#e8eaee; selection-background-color:#5b8cff; selection-color:#ffffff; }
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border:1px solid #5b8cff; }
QComboBox { padding:6px 28px 6px 9px; }
QComboBox QAbstractItemView { background:#14161c; border:1px solid #2a2e38; selection-background-color:#18223f; selection-color:#cdd8ff; outline:0; }
QPushButton { background:#181b22; color:#e8eaee; border:1px solid #2a2e38; border-radius:9px; padding:8px 14px; font-weight:500; }
QPushButton:hover { background:#1e222b; border-color:#3a4150; }
QPushButton:pressed { background:#15181f; }
QPushButton:disabled { background:#131620; border-color:#23262e; color:#565c66; }
QPushButton:focus, QTableWidget:focus, QListWidget:focus, QPlainTextEdit:focus { border:1px solid #5b8cff; outline:0; }
QPushButton[variant="primary"] { background:#5b8cff; border:1px solid #5b8cff; color:#ffffff; font-weight:600; }
QPushButton[variant="primary"]:hover { background:#6f9bff; border-color:#6f9bff; }
QPushButton[variant="primary"]:disabled { background:#222733; border-color:#222733; color:#565c66; }
QPushButton[variant="secondary"] { background:#181b22; border:1px solid #2d313b; color:#e8eaee; }
QPushButton[variant="secondary"]:hover { background:#1e222b; }
QPushButton[variant="danger"] { background:#e5484d; border:1px solid #e5484d; color:#ffffff; font-weight:600; }
QPushButton[variant="danger"]:hover { background:#ef6368; border-color:#ef6368; }
QPushButton[variant="danger"]:disabled { background:#2a1c1e; border-color:#2a1c1e; color:#7a5256; }
QPushButton[variant="ghost"] { background:transparent; border:1px solid #2a2e38; color:#9aa0ab; }
QPushButton[variant="ghost"]:hover { background:#14161c; color:#e8eaee; }
QCheckBox { spacing:8px; color:#e8eaee; }
QCheckBox::indicator { width:16px; height:16px; border:1px solid #2d313b; border-radius:5px; background:#0f1116; }
QCheckBox::indicator:checked { background:#5b8cff; border-color:#5b8cff; }
QScrollBar:vertical { background:transparent; width:10px; margin:2px; }
QScrollBar::handle:vertical { background:#2a2e38; border-radius:5px; min-height:28px; }
QScrollBar::handle:vertical:hover { background:#3a4150; }
QScrollBar:horizontal { background:transparent; height:10px; margin:2px; }
QScrollBar::handle:horizontal { background:#2a2e38; border-radius:5px; min-width:28px; }
QScrollBar::handle:horizontal:hover { background:#3a4150; }
QScrollBar::add-line, QScrollBar::sub-line { height:0; width:0; }
QScrollBar::add-page, QScrollBar::sub-page { background:transparent; }
QWidget#flow_price_chart:focus, QWidget#flow_cvd_chart:focus, QWidget#flow_aggressor_bar:focus { border:1px solid #5b8cff; border-radius:8px; }
"""

DASHBOARD_LIGHT = """
QWidget { background:#f6f7f9; color:#1a1d24; }
QWidget#root { background:#f6f7f9; }
QLabel, QCheckBox, QRadioButton, QWidget[role="plain"] { background:transparent; }
QFrame[role="card"] { background:#ffffff; border:1px solid #e7e9ee; border-radius:12px; }
QFrame[role="stat_tile"] { background:#fafbfc; border:1px solid #e7e9ee; border-radius:12px; }
QFrame[role="empty_state"] { background:#fafbfc; border:1px dashed #cdd2da; border-radius:12px; }
QLabel[role="headline"] { font-weight:600; color:#14171d; }
QLabel[role="section_title"] { font-weight:600; color:#1a1d24; }
QLabel[role="metric"] { font-weight:600; color:#14171d; }
QLabel[role="meter_value"] { color:#1a1d24; font-weight:500; }
QLabel[role="caption"], QLabel[role="muted"] { color:#767d88; }
QLabel[role="page_subtitle"] { color:#4a5160; }
QLabel[state="ok"] { background:#e7f6ef; color:#1c7a56; border:1px solid #b7e0cd; border-radius:999px; padding:3px 11px; }
QLabel[state="warn"], QLabel[state="locked"] { background:#fcf2dc; color:#9a6b12; border:1px solid #ecd5a0; border-radius:999px; padding:3px 11px; font-weight:600; }
QLabel[state="fail"] { background:#fdecee; color:#c0404a; border:1px solid #f2c6cb; border-radius:999px; padding:3px 11px; }
QLabel[state="neutral"] { background:#eef0f4; color:#5c626d; border:1px solid #dde0e6; border-radius:999px; padding:3px 11px; }
QProgressBar { background:#eef0f4; border:1px solid #dde0e6; border-radius:999px; }
QProgressBar::chunk { background:#3f6fff; border-radius:999px; }
QTableWidget { background:#ffffff; alternate-background-color:#f7f8fb; border:1px solid #e7e9ee; border-radius:10px; gridline-color:#eceef2; }
QTableWidget::item { padding:5px 8px; color:#2a2e36; border:0; }
QTableWidget::item:selected { background:#3f6fff; color:#ffffff; }
QHeaderView::section { background:#f3f5f8; color:#767d88; border:0; border-bottom:1px solid #e7e9ee; padding:7px 8px; font-weight:500; }
QListWidget#sidebar { background:#ffffff; border:0; border-right:1px solid #eceef1; padding:10px 8px; outline:0; }
QListWidget#sidebar::item { min-height:36px; padding:0 12px; border-radius:9px; color:#5c626d; }
QListWidget#sidebar::item:selected { background:#eaf0ff; color:#2a4fd0; }
QListWidget#sidebar::item:hover { background:#f3f5f8; }
QStatusBar { background:#ffffff; border-top:1px solid #eceef1; color:#767d88; }
QGroupBox { background:#ffffff; border:1px solid #e7e9ee; border-radius:12px; margin-top:14px; padding:12px 12px 12px; font-weight:600; color:#1a1d24; }
QGroupBox::title { subcontrol-origin:margin; left:12px; top:1px; padding:0 4px; color:#767d88; }
QGroupBox::indicator { width:15px; height:15px; }
QTabWidget::pane { border:1px solid #e7e9ee; border-radius:10px; top:-1px; }
QTabBar::tab { background:transparent; color:#5c626d; padding:7px 14px; margin-right:4px; border:0; border-radius:8px; }
QTabBar::tab:selected { background:#eaf0ff; color:#2a4fd0; }
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox { background:#ffffff; border:1px solid #d7dae1; border-radius:9px; padding:6px 9px; color:#1a1d24; selection-background-color:#3f6fff; selection-color:#ffffff; }
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border:1px solid #3f6fff; }
QComboBox { padding:6px 28px 6px 9px; }
QComboBox QAbstractItemView { background:#ffffff; border:1px solid #d7dae1; selection-background-color:#eaf0ff; selection-color:#2a4fd0; outline:0; }
QPushButton { background:#ffffff; color:#1a1d24; border:1px solid #d7dae1; border-radius:9px; padding:8px 14px; font-weight:500; }
QPushButton:hover { background:#f3f5f8; border-color:#c6cbd4; }
QPushButton:pressed { background:#eceef2; }
QPushButton:disabled { background:#f3f5f8; border-color:#e7e9ee; color:#a9afb9; }
QPushButton:focus, QTableWidget:focus, QListWidget:focus, QPlainTextEdit:focus { border:1px solid #3f6fff; outline:0; }
QPushButton[variant="primary"] { background:#3f6fff; border:1px solid #3f6fff; color:#ffffff; font-weight:600; }
QPushButton[variant="primary"]:hover { background:#5580ff; border-color:#5580ff; }
QPushButton[variant="primary"]:disabled { background:#c9d6ff; border-color:#c9d6ff; color:#ffffff; }
QPushButton[variant="secondary"] { background:#ffffff; border:1px solid #d7dae1; color:#1a1d24; }
QPushButton[variant="secondary"]:hover { background:#f3f5f8; }
QPushButton[variant="danger"] { background:#d83a40; border:1px solid #d83a40; color:#ffffff; font-weight:600; }
QPushButton[variant="danger"]:hover { background:#e2545a; border-color:#e2545a; }
QPushButton[variant="danger"]:disabled { background:#f2c6cb; border-color:#f2c6cb; color:#ffffff; }
QPushButton[variant="ghost"] { background:transparent; border:1px solid #e7e9ee; color:#5c626d; }
QPushButton[variant="ghost"]:hover { background:#f3f5f8; color:#1a1d24; }
QCheckBox { spacing:8px; color:#1a1d24; }
QCheckBox::indicator { width:16px; height:16px; border:1px solid #c6cbd4; border-radius:5px; background:#ffffff; }
QCheckBox::indicator:checked { background:#3f6fff; border-color:#3f6fff; }
QScrollBar:vertical { background:transparent; width:10px; margin:2px; }
QScrollBar::handle:vertical { background:#d7dae1; border-radius:5px; min-height:28px; }
QScrollBar::handle:vertical:hover { background:#c6cbd4; }
QScrollBar:horizontal { background:transparent; height:10px; margin:2px; }
QScrollBar::handle:horizontal { background:#d7dae1; border-radius:5px; min-width:28px; }
QScrollBar::handle:horizontal:hover { background:#c6cbd4; }
QScrollBar::add-line, QScrollBar::sub-line { height:0; width:0; }
QScrollBar::add-page, QScrollBar::sub-page { background:transparent; }
QWidget#flow_price_chart:focus, QWidget#flow_cvd_chart:focus, QWidget#flow_aggressor_bar:focus { border:1px solid #3f6fff; border-radius:8px; }
"""

DASHBOARD_THEMES = {"dark": DASHBOARD_DARK, "light": DASHBOARD_LIGHT}
