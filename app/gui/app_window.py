"""The redesigned MNQ window: left sidebar, eight screens, snapshot-only refresh.

Built additively beside the legacy 13-tab ``MainWindow`` so the working
application stays available until this one is proven. The Qt thread here does
exactly two things: swap the visible screen, and format an immutable
:class:`~app.gui.view_models.AppSnapshot`. It performs no disk, network,
research, automation, or catalog work — that is what starved the receiver and
froze the old window.

The snapshot arrives from a provider callable that a background worker owns. If
no provider is supplied the window still renders a truthful "not started" state.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
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

from app.gui.screens import SCREEN_ORDER, build_screens
from app.gui.snapshot_worker import SnapshotWorker
from app.gui.theme import DASHBOARD_THEMES
from app.gui.view_models import AppSnapshot, Health
from app.gui.widgets import StatusBadge

SnapshotProvider = Callable[[], AppSnapshot]

# 5-10 Hz: fast enough to feel live, slow enough to never fight the receiver.
SNAPSHOT_INTERVAL_MS = 150

_DARK = """
QWidget { background: #14171c; color: #e6e9ef; font-size: 13px; }
QLabel[role="headline"] { font-size: 18px; font-weight: 600; color: #f2f4f8; }
QLabel[role="caption"] { color: #9aa4b2; }
QLabel[role="locked"] { color: #0f1115; background: #d9a441; padding: 3px 10px;
                        border-radius: 4px; font-weight: 700; }
QGroupBox { border: 1px solid #2a2f39; border-radius: 6px; margin-top: 10px;
            padding: 10px 8px 8px 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #9aa4b2; }
QListWidget#sidebar { background: #0f1115; border: none; outline: none;
                      font-size: 14px; padding: 8px 4px; }
QListWidget#sidebar::item { padding: 9px 12px; border-radius: 5px; margin: 2px 6px; }
QListWidget#sidebar::item:selected { background: #2b6cb0; color: #ffffff; }
QListWidget#sidebar::item:hover:!selected { background: #1b2029; }
QProgressBar { border: 1px solid #2a2f39; border-radius: 4px; text-align: center;
               background: #0f1115; height: 18px; }
QProgressBar::chunk { background: #2f855a; border-radius: 3px; }
QStatusBar { background: #0f1115; color: #9aa4b2; }
"""

_LIGHT = """
QWidget { background: #f6f7f9; color: #1a1d23; font-size: 13px; }
QLabel[role="headline"] { font-size: 18px; font-weight: 600; color: #12141a; }
QLabel[role="caption"] { color: #5b6472; }
QLabel[role="locked"] { color: #3a2b00; background: #f0c14b; padding: 3px 10px;
                        border-radius: 4px; font-weight: 700; }
QGroupBox { border: 1px solid #d6dae1; border-radius: 6px; margin-top: 10px;
            padding: 10px 8px 8px 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #5b6472; }
QListWidget#sidebar { background: #eceff3; border: none; outline: none;
                      font-size: 14px; padding: 8px 4px; }
QListWidget#sidebar::item { padding: 9px 12px; border-radius: 5px; margin: 2px 6px; }
QListWidget#sidebar::item:selected { background: #2b6cb0; color: #ffffff; }
QProgressBar { border: 1px solid #d6dae1; border-radius: 4px; text-align: center;
               background: #ffffff; height: 18px; }
QProgressBar::chunk { background: #2f855a; border-radius: 3px; }
QStatusBar { background: #eceff3; color: #5b6472; }
"""

THEMES = DASHBOARD_THEMES


class AppWindow(QMainWindow):
    """The redesigned eight-screen window."""

    def __init__(
        self,
        *,
        snapshot_provider: SnapshotProvider | None = None,
        execution_commander: object | None = None,
        theme: str = "dark",
        gui_scale: float = 1.0,
        start_timer: bool = True,
    ) -> None:
        """Create the window; ``snapshot_provider`` is owned by a background worker."""
        super().__init__()
        self.setWindowTitle("MNQ Order-Flow Assistant")
        self.resize(1280, 720)
        self.setMinimumSize(900, 640)
        self._snapshot_provider = snapshot_provider
        self._execution_commander = execution_commander
        self._snapshot_worker: SnapshotWorker | None = None
        self._snapshot = AppSnapshot()
        self._theme = theme
        self._gui_scale = gui_scale
        self._compact_navigation = False

        root = QWidget()
        root.setObjectName("root")
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.sidebar = QListWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sidebar.setFixedWidth(208)
        for name in SCREEN_ORDER:
            item = QListWidgetItem(name, self.sidebar)
            item.setToolTip(name)
        self.sidebar.setAccessibleName("Application sections")
        self.sidebar.currentRowChanged.connect(self._on_navigate)
        layout.addWidget(self.sidebar)

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(14, 10, 14, 8)
        body_layout.setSpacing(8)
        self.trust_strip = QWidget()
        trust_layout = QHBoxLayout(self.trust_strip)
        trust_layout.setContentsMargins(0, 0, 0, 0)
        self._source_badge = StatusBadge("SOURCE UNKNOWN", "trust_source", "neutral")
        self._integrity_badge = StatusBadge("CAPTURE IDLE", "trust_integrity", "neutral")
        self._drops_badge = StatusBadge("SESSION DROPS 0", "trust_drops", "ok")
        self._live_badge = StatusBadge("LIVE LOCKED", "trust_live", "locked")
        for badge in (self._source_badge, self._integrity_badge, self._drops_badge):
            trust_layout.addWidget(badge)
        trust_layout.addStretch(1)
        trust_layout.addWidget(self._live_badge)
        body_layout.addWidget(self.trust_strip)

        self.stack = QStackedWidget()
        self.stack.setObjectName("screen_stack")
        self._screens = build_screens()
        execution = self._screens.get("Execution")
        if execution is not None and hasattr(execution, "set_commander"):
            execution.set_commander(self._execution_commander)
        for name in SCREEN_ORDER:
            self.stack.addWidget(self._screens[name])
        body_layout.addWidget(self.stack, 1)
        layout.addWidget(body, 1)

        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())
        self._status_label = QLabel("Starting…")
        self._status_label.setObjectName("status_bar_text")
        self._status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.statusBar().addWidget(self._status_label, 1)

        self.apply_theme(theme)
        self.set_gui_scale(gui_scale)
        self.sidebar.setCurrentRow(0)
        self._update_responsive_layout()

        if start_timer and snapshot_provider is not None:
            self._snapshot_worker = SnapshotWorker(
                snapshot_provider,
                interval_seconds=SNAPSHOT_INTERVAL_MS / 1000,
            )
            self._snapshot_worker.snapshot_ready.connect(self.submit_snapshot)
            self._snapshot_worker.start()
        self.refresh_from_snapshot()

    # -- navigation ---------------------------------------------------------------

    def _on_navigate(self, row: int) -> None:
        if 0 <= row < self.stack.count():
            self.stack.setCurrentIndex(row)
            self._render_current()

    def navigate_to(self, name: str) -> None:
        """Select a screen by its sidebar name (used by tests and shortcuts)."""
        self.sidebar.setCurrentRow(SCREEN_ORDER.index(name))

    @property
    def current_screen_name(self) -> str:
        """Return the visible screen's name."""
        return SCREEN_ORDER[self.stack.currentIndex()]

    # -- rendering ----------------------------------------------------------------

    def resizeEvent(self, event: object) -> None:  # noqa: N802 - Qt API
        """Switch navigation density at stable logical-pixel breakpoints."""
        super().resizeEvent(event)  # type: ignore[arg-type]
        self._update_responsive_layout()

    def _update_responsive_layout(self) -> None:
        compact = self.width() < 1180
        if compact == self._compact_navigation and self.sidebar.width() in (72, 208):
            return
        self._compact_navigation = compact
        self.sidebar.setFixedWidth(72 if compact else 208)
        for index, name in enumerate(SCREEN_ORDER):
            item = self.sidebar.item(index)
            item.setText(name[:2].upper() if compact else name)
            item.setToolTip(name)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, name)

    def refresh_from_snapshot(self) -> None:
        """Repaint from the latest immutable snapshot.

        ``start_timer=False`` is an explicit deterministic test/manual mode and
        permits a synchronous in-memory provider.  Production mode always owns
        a :class:`SnapshotWorker`, so this Qt path never invokes the provider.
        """
        if self._snapshot_worker is None and self._snapshot_provider is not None:
            try:
                self._snapshot = self._snapshot_provider()
            except Exception:  # noqa: BLE001 - a bad snapshot must never kill the GUI
                pass
        self._render_current()
        self._render_status_bar()

    def submit_snapshot(self, snapshot: object) -> None:
        """Accept one typed snapshot delivered through the background-worker signal."""
        if not isinstance(snapshot, AppSnapshot):
            return
        self._snapshot = snapshot
        self._render_current()
        self._render_status_bar()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        """Stop only the GUI snapshot reader; the backend remains independent."""
        if self._snapshot_worker is not None:
            self._snapshot_worker.stop()
        super().closeEvent(event)

    def _render_current(self) -> None:
        # Only the visible screen is formatted: hidden screens cost nothing.
        screen = self._screens[self.current_screen_name]
        screen.render(self._snapshot)

    def _render_status_bar(self) -> None:
        snap = self._snapshot
        capture, market = snap.capture, snap.market
        source_state = "warn" if market.is_delayed else "ok"
        self._source_badge.set_status(market.provenance_text, source_state)
        integrity_state = (
            "fail" if capture.current_session_drops
            else "ok" if capture.recording
            else "neutral"
        )
        self._integrity_badge.set_status(
            f"CAPTURE {capture.health.value.upper()}", integrity_state,
        )
        self._drops_badge.set_status(
            f"SESSION DROPS {capture.current_session_drops:,}",
            "fail" if capture.current_session_drops else "ok",
        )
        message = snap.blocker or snap.next_action or "Awaiting backend state"
        self._status_label.setText(
            f"{snap.plain_state}  ·  {message}  ·  Session drops: "
            f"{capture.current_session_drops:,}  ·  LIVE LOCKED",
        )

    # -- appearance ---------------------------------------------------------------

    def apply_theme(self, theme: str) -> None:
        """Apply the dark or light palette."""
        self._theme = theme if theme in THEMES else "dark"
        self.setStyleSheet(THEMES[self._theme])

    @property
    def theme(self) -> str:
        """Return the active theme name."""
        return self._theme

    def set_gui_scale(self, scale: float) -> None:
        """Apply bounded accessibility zoom using an installed system font."""
        from PySide6.QtGui import QFontDatabase

        scale = max(0.8, min(2.0, scale))
        self._gui_scale = scale
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
        font.setPointSizeF(max(8.0, font.pointSizeF()) * scale)
        self.setFont(font)

    @property
    def gui_scale(self) -> float:
        """Return the GUI scale factor."""
        return self._gui_scale

    def reset_layout(self) -> None:
        """Restore safe defaults."""
        self.resize(1280, 720)
        self.apply_theme("dark")
        self.set_gui_scale(1.0)
        self.sidebar.setCurrentRow(0)
