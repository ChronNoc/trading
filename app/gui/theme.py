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


DASHBOARD_DARK = """
QWidget { background:#0b1017; color:#dbe7f5; }
QWidget#root { background:#0b1017; }
QFrame[role="card"] { background:#121923; border:1px solid #263244; border-radius:10px; }
QFrame[role="stat_tile"] { background:#0e151e; border:1px solid #253246; border-radius:8px; }
QFrame[role="empty_state"] { background:#101720; border:1px dashed #40506a; border-radius:8px; }
QLabel[role="headline"] { font-size:22px; font-weight:700; color:#f5f8fc; }
QLabel[role="section_title"] { font-size:14px; font-weight:650; color:#f5f8fc; }
QLabel[role="metric"] { font-size:20px; font-weight:700; color:#f5f8fc; }
QLabel[role="caption"], QLabel[role="muted"] { color:#8fa1b7; }
QLabel[state="ok"] { background:#153c32; color:#a8f0d6; border:1px solid #2b6d59; border-radius:10px; padding:4px 9px; }
QLabel[state="warn"] { background:#45351b; color:#ffe0a3; border:1px solid #7a5c28; border-radius:10px; padding:4px 9px; }
QLabel[state="fail"] { background:#48252b; color:#ffbdc4; border:1px solid #82414a; border-radius:10px; padding:4px 9px; }
QLabel[state="locked"] { background:#49391b; color:#ffd98c; border:1px solid #a57b25; border-radius:10px; padding:4px 9px; font-weight:700; }
QLabel[state="neutral"] { background:#172131; color:#b9c9dc; border:1px solid #34445d; border-radius:10px; padding:4px 9px; }
QProgressBar { background:#0a0f16; border:1px solid #2c394c; border-radius:4px; }
QProgressBar::chunk { background:#4aa8ff; border-radius:3px; }
QTableWidget { background:#101720; alternate-background-color:#141d29; border:1px solid #263244; border-radius:7px; gridline-color:#263244; }
QHeaderView::section { background:#172131; color:#b9c9dc; border:0; border-bottom:1px solid #34445d; padding:6px; font-weight:600; }
QListWidget#sidebar { background:#080d13; border:0; border-right:1px solid #1f2b3a; padding:8px 6px; outline:0; }
QListWidget#sidebar::item { min-height:38px; padding:0 10px; border-radius:7px; color:#aebed0; }
QListWidget#sidebar::item:selected { background:#1f5e9e; color:#ffffff; }
QListWidget#sidebar::item:hover { background:#152237; }
QStatusBar { background:#080d13; border-top:1px solid #1f2b3a; color:#9aabc0; }
QPushButton { background:#172131; border:1px solid #34445d; border-radius:7px; padding:7px 12px; }
QPushButton:hover { border-color:#4aa8ff; }
QPushButton:focus, QTableWidget:focus, QListWidget:focus { border:2px solid #78bdff; }
"""

DASHBOARD_LIGHT = """
QWidget { background:#f4f7fb; color:#172235; }
QWidget#root { background:#f4f7fb; }
QFrame[role="card"] { background:#ffffff; border:1px solid #d8e0eb; border-radius:10px; }
QFrame[role="stat_tile"] { background:#f8fafc; border:1px solid #d8e0eb; border-radius:8px; }
QFrame[role="empty_state"] { background:#f8fafc; border:1px dashed #9aa8bb; border-radius:8px; }
QLabel[role="headline"] { font-size:22px; font-weight:700; color:#101928; }
QLabel[role="section_title"] { font-size:14px; font-weight:650; color:#101928; }
QLabel[role="metric"] { font-size:20px; font-weight:700; color:#101928; }
QLabel[role="caption"], QLabel[role="muted"] { color:#5e6d82; }
QLabel[state="ok"] { background:#e1f5ed; color:#195b46; border:1px solid #83c8af; border-radius:10px; padding:4px 9px; }
QLabel[state="warn"], QLabel[state="locked"] { background:#fff1d4; color:#714b05; border:1px solid #d5a348; border-radius:10px; padding:4px 9px; font-weight:700; }
QLabel[state="fail"] { background:#fde8ea; color:#812c38; border:1px solid #d98892; border-radius:10px; padding:4px 9px; }
QLabel[state="neutral"] { background:#e9eef5; color:#34445b; border:1px solid #b7c3d1; border-radius:10px; padding:4px 9px; }
QProgressBar { background:#e9eef5; border:1px solid #c5cfdb; border-radius:4px; }
QProgressBar::chunk { background:#1768ac; border-radius:3px; }
QTableWidget { background:#ffffff; alternate-background-color:#f5f8fb; border:1px solid #d8e0eb; border-radius:7px; gridline-color:#e1e6ee; }
QHeaderView::section { background:#edf2f7; color:#3e4c61; border:0; border-bottom:1px solid #d8e0eb; padding:6px; font-weight:600; }
QListWidget#sidebar { background:#162235; border:0; border-right:1px solid #101928; padding:8px 6px; outline:0; }
QListWidget#sidebar::item { min-height:38px; padding:0 10px; border-radius:7px; color:#dbe7f5; }
QListWidget#sidebar::item:selected { background:#1768ac; color:#ffffff; }
QStatusBar { background:#162235; color:#dbe7f5; }
QPushButton { background:#ffffff; border:1px solid #b7c3d1; border-radius:7px; padding:7px 12px; }
QPushButton:focus, QTableWidget:focus, QListWidget:focus { border:2px solid #1768ac; }
"""

DASHBOARD_THEMES = {"dark": DASHBOARD_DARK, "light": DASHBOARD_LIGHT}
