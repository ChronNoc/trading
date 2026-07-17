"""Shared test guards.

The rule this enforces: **tests must never write into the user's real data.**

It was being violated silently. ``MainWindow()`` defaults its automation output
to ``data/prototype``, so every full-suite run appended ~291 rows to the tracked
``data/prototype/automation/decisions_log.csv`` and rewrote the decision
snapshots. Nothing failed, so it went unnoticed while it corrupted real files
that are committed to the repository.

A guard beats a fix here: pinning the one bad call site would leave the next
default-constructed window free to do it again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Real directories holding user-owned data that a test must never touch.
PROTECTED_DIRS = ("data/prototype", "data/raw", "data/processed", "data/paper", "data/reports")


@pytest.fixture(autouse=True)
def _never_write_to_real_user_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect GUI default output roots into this test's temp directory.

    Autouse so it protects every test, including ones not yet written.
    """
    try:
        import app.gui.main_window as main_window
    except Exception:  # noqa: BLE001 - PySide6 absent: nothing to guard
        return

    sandbox = tmp_path / "prototype"
    sandbox.mkdir(parents=True, exist_ok=True)
    original = main_window.MainWindow.__init__

    def guarded_init(self, *args: object, **kwargs: object) -> None:  # noqa: ANN001
        kwargs.setdefault("automation_output_root", sandbox)
        kwargs.setdefault("review_output_root", sandbox / "reports")
        original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(main_window.MainWindow, "__init__", guarded_init)
