"""Capture reviewable screenshots of the redesigned window with real fonts.

The offscreen QPA platform used by the test suite ships **zero** font families
(``QFontDatabase.families() == []``), so its screenshots render every glyph as a
tofu box. Those tests still prove geometry, clipping, and dead-space budgets, but
they cannot prove the window is *readable*. This tool renders with the native
Windows platform so the saved PNGs show real text.

    .venv\\Scripts\\python.exe -m tools.capture_gui_screenshots

Writes reports/screenshots/*.png. Does not touch market data.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Must be chosen before QApplication is constructed; native platform = real fonts.
os.environ.pop("QT_QPA_PLATFORM", None)

OUTPUT_DIR = Path("reports/screenshots")
RESOLUTIONS = (("1366x768", 1366, 768, 1.0), ("1920x1080", 1920, 1080, 1.0),
               ("compact_900x640", 900, 640, 1.0), ("zoom_150_1366x768", 1366, 768, 1.5))


def main() -> int:
    """Render the window at every required resolution and save PNGs."""
    from PySide6.QtGui import QFontDatabase
    from PySide6.QtWidgets import QApplication

    from app.gui.app_window import AppWindow
    from app.gui.review_snapshot import build_review_snapshot
    from app.gui.screens import SCREEN_ORDER

    app = QApplication.instance() or QApplication(sys.argv)
    if not QFontDatabase.families():
        raise RuntimeError("native screenshot capture has no usable fonts; refusing tofu output")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []

    for label, width, height, scale in RESOLUTIONS:
        window = AppWindow(snapshot_provider=build_review_snapshot, start_timer=False, gui_scale=scale)
        window.resize(width, height)
        window.navigate_to("Overview")
        window.refresh_from_snapshot()
        window.show()
        app.processEvents()
        path = OUTPUT_DIR / f"overview_{label}.png"
        window.grab().save(str(path))
        saved.append(str(path))
        window.close()

    # One screenshot per screen at the smallest supported display.
    window = AppWindow(snapshot_provider=build_review_snapshot, start_timer=False)
    window.resize(1366, 768)
    window.show()
    for name in SCREEN_ORDER:
        window.navigate_to(name)
        window.refresh_from_snapshot()
        app.processEvents()
        path = OUTPUT_DIR / f"screen_{name.lower().replace(' ', '_')}.png"
        window.grab().save(str(path))
        saved.append(str(path))
    window.close()

    print(f"saved {len(saved)} screenshots to {OUTPUT_DIR}")
    for path in saved:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
