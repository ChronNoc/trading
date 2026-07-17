"""Tests must never write into the user's real, tracked data.

This was violated silently: MainWindow defaults its automation output to
``data/prototype``, so every full-suite run appended ~291 rows to the tracked
``data/prototype/automation/decisions_log.csv`` and rewrote the decision
snapshots. Nothing failed, so real committed files were being corrupted by the
mere act of running the tests.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_API", "pyside6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.gui.main_window import MainWindow

from tests.conftest import PROTECTED_DIRS

PROTOTYPE_AUTOMATION = Path("data/prototype/automation")


def _snapshot(directory: Path) -> dict[str, tuple[int, int]]:
    """Map every file under ``directory`` to (size, mtime_ns)."""
    if not directory.is_dir():
        return {}
    return {
        str(path): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_a_default_constructed_window_cannot_touch_real_user_data(
    qtbot: object, tmp_path: Path,
) -> None:
    """The exact call that was corrupting data/prototype: MainWindow() bare."""
    before = _snapshot(PROTOTYPE_AUTOMATION)
    window = MainWindow()
    qtbot.addWidget(window)  # type: ignore[attr-defined]

    automation_dir = window._automation_engine.automation_dir  # noqa: SLF001
    assert str(automation_dir).startswith(str(tmp_path)), (
        f"automation output escaped the sandbox into {automation_dir}"
    )
    assert _snapshot(PROTOTYPE_AUTOMATION) == before, (
        "constructing a window must not modify tracked user data"
    )


def test_the_decisions_log_is_not_appended_to_by_tests(qtbot: object) -> None:
    """The concrete regression: ~291 rows appended per full-suite run."""
    log = PROTOTYPE_AUTOMATION / "decisions_log.csv"
    if not log.is_file():
        return
    before = log.stat().st_size
    window = MainWindow()
    qtbot.addWidget(window)  # type: ignore[attr-defined]
    assert log.stat().st_size == before, "tests must not grow the user's decisions log"


def test_conftest_guard_is_autouse_so_it_covers_future_tests() -> None:
    """Pinning one call site would leave the next default window free to offend."""
    source = Path("tests/conftest.py").read_text(encoding="utf-8")
    assert "autouse=True" in source
    assert "automation_output_root" in source
    assert "review_output_root" in source


def test_protected_directories_are_named_and_repo_relative() -> None:
    """The guard's protected list must reference real project data locations."""
    assert "data/prototype" in PROTECTED_DIRS
    assert "data/raw" in PROTECTED_DIRS
    for name in PROTECTED_DIRS:
        assert not Path(name).is_absolute(), "protected paths are repo-relative"
