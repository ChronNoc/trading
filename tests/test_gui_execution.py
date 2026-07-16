"""GUI tests for the Execution Connection panel (PAPER default, LIVE locked)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_API", "pyside6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

from PySide6.QtWidgets import QComboBox, QListWidget, QPushButton

from app.discovery.supervisor import ModeSupervisor
from app.gui.main_window import MainWindow


def _window(qtbot: object, tmp_path: Path) -> MainWindow:
    window = MainWindow(
        replay_data_root=tmp_path / "raw",
        processed_root=tmp_path / "processed",
        mode_supervisor=ModeSupervisor(tmp_path / "production_config.yaml"),
    )
    qtbot.addWidget(window)
    return window


def _panel_text(window: MainWindow) -> str:
    panel = window.findChild(QListWidget, "execution_status_list")
    assert panel is not None
    return " | ".join(panel.item(i).text() for i in range(panel.count()))


def test_execution_tab_starts_paper_and_disarmed(qtbot: object, tmp_path: Path) -> None:
    """Startup: PAPER environment, disarmed, prop rules honestly unresolved."""
    window = _window(qtbot, tmp_path)
    combo = window.findChild(QComboBox, "execution_environment_combo")
    assert combo is not None and combo.currentText() == "PAPER"
    text = _panel_text(window)
    assert "DEMO disarmed" in text
    assert "LIVE disarmed (locked)" in text
    assert "UNRESOLVED - automated execution blocked" in text
    assert "no broker transport exists" in text


def test_arm_live_lists_every_unmet_requirement(qtbot: object, tmp_path: Path) -> None:
    """Arm LIVE never arms: it renders the full list of unmet gate requirements."""
    window = _window(qtbot, tmp_path)
    button = window.findChild(QPushButton, "execution_arm_live_button")
    assert button is not None
    window._execution_arm_live()
    text = _panel_text(window)
    assert "LIVE arming BLOCKED" in text
    assert "live_enabled is not true" in text
    assert "prop-firm rule profile is unresolved" in text
    assert "confirmation phrase" in text
    assert window._execution_arming.live_armed is False


def test_demo_connect_without_credentials_is_honest(qtbot: object, tmp_path: Path, monkeypatch) -> None:
    """DEMO connect with no env credentials reports what is missing, shows no secrets."""
    for name in ("TRADOVATE_DEMO_USERNAME", "TRADOVATE_DEMO_PASSWORD", "TRADOVATE_DEMO_APP_ID",
                 "TRADOVATE_DEMO_APP_VERSION", "TRADOVATE_DEMO_CID", "TRADOVATE_DEMO_SECRET"):
        monkeypatch.delenv(name, raising=False)
    window = _window(qtbot, tmp_path)
    combo = window.findChild(QComboBox, "execution_environment_combo")
    combo.setCurrentText("TRADOVATE DEMO")
    window._execution_connect()
    text = _panel_text(window)
    assert "Missing Tradovate demo credentials" in text
    assert window._demo_gateway is None


def test_paper_connect_needs_nothing_and_safety_buttons_do_not_crash(qtbot: object, tmp_path: Path) -> None:
    """On PAPER, connect/cancel/flatten stay local and responsive."""
    window = _window(qtbot, tmp_path)
    window._execution_connect()
    assert "PAPER needs no connection" in _panel_text(window)
    window._execution_cancel_all()  # environment gate: points user to DEMO selection
    assert "Select TRADOVATE DEMO" in _panel_text(window)
