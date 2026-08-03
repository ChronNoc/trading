"""Explicitly regenerate the 24 deterministic offscreen GUI baselines.

Run only when the visual change is intentional and has been reviewed::

    .venv\\Scripts\\python.exe -m tools.bless_gui_baselines
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Callable

# Baselines deliberately use the same deterministic platform as pytest.
os.environ["QT_API"] = "pyside6"
os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication

from app.gui.app_window import AppWindow
from app.gui.review_snapshot import (
    build_empty_snapshot,
    build_partial_snapshot,
    build_review_snapshot,
)
from app.gui.screens import SCREEN_ORDER
from app.gui.view_models import AppSnapshot
from app.gui.visual_regression import capture_window_image, save_image_array, screen_slug

BASELINE_DIR = Path("tests/fixtures/gui_baselines")
SnapshotBuilder = Callable[[], AppSnapshot]
STATES: tuple[tuple[str, SnapshotBuilder], ...] = (
    ("empty", build_empty_snapshot),
    ("partial", build_partial_snapshot),
    ("full", build_review_snapshot),
)


def main() -> int:
    """Replace every blessed baseline with a fresh deterministic capture."""
    app = QApplication.instance() or QApplication(sys.argv)
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    expected_names: set[str] = set()

    for state_name, snapshot_builder in STATES:
        for screen_name in SCREEN_ORDER:
            fixture_name = f"{state_name}__{screen_slug(screen_name)}.png"
            expected_names.add(fixture_name)
            window = AppWindow(
                snapshot_provider=snapshot_builder,
                start_timer=False,
            )
            image = capture_window_image(window, screen_name)
            save_image_array(image, BASELINE_DIR / fixture_name)
            window.close()
            app.processEvents()

    stale = tuple(
        path for path in BASELINE_DIR.glob("*.png") if path.name not in expected_names
    )
    for path in stale:
        path.unlink()

    print(f"blessed {len(expected_names)} GUI baselines in {BASELINE_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
