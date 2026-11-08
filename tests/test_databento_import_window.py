"""The Databento drop-in window: drops, refusals, Stop, closing, and the shared theme."""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

os.environ.setdefault("QT_API", "pyside6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication

import app.gui.databento_import_window as module
from app.gui.databento_import_window import DatabentoImportWindow
from app.gui.theme import DASHBOARD_THEMES
from app.importers.databento import ImportResult, ProgressEvent
from tests.databento_fixtures import Gen, day_rows, write_csv

WAIT_MS = 60_000


def drop(window: DatabentoImportWindow, *paths: Path) -> None:
    """Deliver a real drop event carrying local file URLs (no file is touched here)."""
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
    event = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    window.dropEvent(event)


def finish(qtbot: object, window: DatabentoImportWindow) -> None:
    """Wait for the running import to end and for its queued signals to be applied."""
    worker = window.page._worker
    assert worker is not None
    # Wait on the thread, not on a signal: a fast import can finish before a signal wait attaches.
    qtbot.waitUntil(lambda: not worker.isRunning(), timeout=WAIT_MS)  # type: ignore[attr-defined]
    worker.wait()
    QApplication.processEvents()  # deliver the queued result and done signals


def make(qtbot: object, tmp_path: Path, **kwargs: object) -> DatabentoImportWindow:
    window = DatabentoImportWindow(out_root=tmp_path / "raw", **kwargs)  # type: ignore[arg-type]
    qtbot.addWidget(window)  # type: ignore[attr-defined]
    return window


# --- the idle page --------------------------------------------------------------------------------


def test_the_window_opens_ready_with_nothing_imported_and_nothing_to_stop(qtbot: object, tmp_path: Path) -> None:
    window = make(qtbot, tmp_path)
    page = window.page
    assert page.state_badge.text() == "READY" and not page.running
    assert not page.stop_button.isEnabled() and page.files_button.isEnabled() and page.folder_button.isEnabled()
    assert page.results_table.rowCount() == 1 and page.results_table.item(0, 1).text() == "no files imported yet"
    assert not page.training_option.isChecked(), "machine-learning training must be off by default"
    assert page.files_button.property("variant") == "primary"


def test_the_window_uses_the_shared_dashboard_theme_in_both_schemes(qtbot: object, tmp_path: Path) -> None:
    window = make(qtbot, tmp_path)
    assert window.theme == "dark" and window.styleSheet() == DASHBOARD_THEMES["dark"]
    window.apply_theme("light")
    assert window.theme == "light" and window.styleSheet() == DASHBOARD_THEMES["light"]
    window.apply_theme("nonsense")
    assert window.theme == "dark"


def test_the_page_writes_no_colour_or_style_of_its_own() -> None:
    source = Path("app/gui/databento_import_window.py").read_text(encoding="utf-8")
    # The one style call is applying the shared theme, exactly as AppWindow does.
    assert re.findall(r"setStyleSheet\((.*)\)", source) == ["DASHBOARD_THEMES[self._theme]"]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", source), "every colour must come from the theme"


def test_dragging_files_over_the_window_changes_only_the_drop_text(qtbot: object, tmp_path: Path) -> None:
    window = make(qtbot, tmp_path)
    page = window.page
    page.show_drag(True)
    assert page.drop_area.title.text() == "Release to import"
    page.show_drag(False)
    assert page.drop_area.title.text() == "Drop files here"


# --- importing -------------------------------------------------------------------------------------


def test_dropping_a_multi_day_file_imports_every_day_and_reports_it(qtbot: object, tmp_path: Path) -> None:
    window = make(qtbot, tmp_path)
    drop(window, write_csv(tmp_path / "month.mbp-10.csv", day_rows(2, 400)))
    assert window.page.running and window.page.state_badge.text() in {"READING", "IMPORTING"}
    assert not window.page.files_button.isEnabled() and window.page.stop_button.isEnabled()
    finish(qtbot, window)
    page = window.page
    assert page.state_badge.text() == "FINISHED" and not page.running
    assert page.imported_tile.value.text() == "2" and page.already_tile.value.text() == "0"
    results = [page.results_table.item(row, 2).text() for row in range(page.results_table.rowCount())]
    assert results == ["IMPORTED", "IMPORTED"]
    assert page.files_button.isEnabled() and not page.stop_button.isEnabled()
    assert len(list((tmp_path / "raw").glob("*/*/session_manifest.json"))) == 2
    assert "2 session(s) imported" in page.summary.text()


def test_dropping_the_same_file_again_says_already_imported_and_changes_nothing(qtbot: object, tmp_path: Path) -> None:
    window = make(qtbot, tmp_path)
    source = write_csv(tmp_path / "day.mbp-10.csv", Gen(seed=3).run(500)[0])
    drop(window, source)
    finish(qtbot, window)
    drop(window, source)
    finish(qtbot, window)
    page = window.page
    assert page.already_tile.value.text() == "1" and page.imported_tile.value.text() == "0"
    assert page.results_table.item(0, 2).text() == "ALREADY IMPORTED"
    assert len(list((tmp_path / "raw").glob("*/*/session_manifest.json"))) == 1


def test_files_it_cannot_import_are_explained_in_the_table_not_hidden(qtbot: object, tmp_path: Path) -> None:
    window = make(qtbot, tmp_path)
    binary = tmp_path / "x.dbn.zst"
    binary.write_bytes(b"DBN")
    trades = tmp_path / "t.trades.csv"
    trades.write_text("ts_event,price,size,side\n", encoding="utf-8")
    drop(window, binary, trades)
    finish(qtbot, window)
    page = window.page
    rows = {page.results_table.item(r, 1).text(): (page.results_table.item(r, 2).text(), page.results_table.item(r, 3).text())
            for r in range(page.results_table.rowCount())}
    assert rows["x.dbn.zst"][0] == "NOT IMPORTED" and "choose CSV" in rows["x.dbn.zst"][1]
    assert rows["t.trades.csv"][0] == "SKIPPED" and "already contains every trade" in rows["t.trades.csv"][1]
    assert page.state_badge.text() == "NOTHING IMPORTED" and page.refused_tile.value.text() == "1"
    assert not (tmp_path / "raw").exists()


def test_the_training_option_is_passed_to_the_import_when_ticked(qtbot: object, tmp_path: Path) -> None:
    window = make(qtbot, tmp_path)
    window.page.training_option.setChecked(True)
    drop(window, write_csv(tmp_path / "day.mbp-10.csv", Gen(seed=4).run(500)[0]))
    finish(qtbot, window)
    manifest = next((tmp_path / "raw").glob("*/*/session_manifest.json")).read_text(encoding="utf-8")
    assert '"allow_model_training": true' in manifest


def test_a_file_given_at_start_up_is_imported_straight_away(qtbot: object, tmp_path: Path) -> None:
    source = write_csv(tmp_path / "day.mbp-10.csv", Gen(seed=5).run(500)[0])
    window = make(qtbot, tmp_path, initial_paths=[source])
    qtbot.waitUntil(lambda: window.page._worker is not None, timeout=10_000)  # type: ignore[attr-defined]
    finish(qtbot, window)
    assert window.page.imported_tile.value.text() == "1"


# --- a controllable fake import for Stop, closing and a second drop ------------------------------------


@pytest.fixture
def blocking_import(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """An import that runs until cancelled or released, so timing-sensitive paths are deterministic."""
    release = threading.Event()

    def fake(paths, options, *, progress=None, cancel=None, on_result=None):  # noqa: ANN001, ANN202
        results: list[ImportResult] = []
        if progress is not None:
            progress(ProgressEvent("converting", 10, 100, "Converting", "x.csv", 1, 1))
        while not release.is_set():
            if cancel is not None and cancel():
                results.append(ImportResult("cancelled", Path("x.csv"), "Stopped. Nothing more was saved from x.csv."))
                if on_result is not None:
                    on_result(results[-1])
                return results
            release.wait(0.01)
        return results

    monkeypatch.setattr(module, "import_many", fake)
    return release


def test_a_second_drop_while_busy_is_declined_not_queued_or_mixed_in(
    qtbot: object, tmp_path: Path, blocking_import: threading.Event,
) -> None:
    window = make(qtbot, tmp_path)
    drop(window, tmp_path / "a.csv")
    first = window.page._worker
    drop(window, tmp_path / "b.csv")
    assert window.page._worker is first
    assert "already running" in window.page.status_line.text()
    blocking_import.set()
    finish(qtbot, window)


def test_stop_ends_the_import_and_says_so(qtbot: object, tmp_path: Path, blocking_import: threading.Event) -> None:
    window = make(qtbot, tmp_path)
    drop(window, tmp_path / "a.csv")
    qtbot.waitUntil(lambda: window.page.state_badge.text() == "IMPORTING", timeout=10_000)  # type: ignore[attr-defined]
    window.page.stop_button.click()
    assert window.page.state_badge.text() == "STOPPING" and not window.page.stop_button.isEnabled()
    finish(qtbot, window)
    page = window.page
    assert page.state_badge.text() == "STOPPED" and not page.running
    assert page.files_button.isEnabled()


def test_closing_the_window_mid_import_stops_the_worker_before_it_closes(
    qtbot: object, tmp_path: Path, blocking_import: threading.Event,
) -> None:
    window = make(qtbot, tmp_path)
    window.show()
    drop(window, tmp_path / "a.csv")
    qtbot.waitUntil(lambda: window.page.running, timeout=10_000)  # type: ignore[attr-defined]
    worker = window.page._worker
    assert window.close() is True
    assert worker is not None and not worker.isRunning()


def test_a_crash_inside_the_import_becomes_a_failed_row_not_a_dead_window(
    qtbot: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(module, "import_many", boom)
    window = make(qtbot, tmp_path)
    drop(window, tmp_path / "a.csv")
    finish(qtbot, window)
    page = window.page
    assert page.state_badge.text() == "FAILED" and "disk full" in page.results_table.item(0, 3).text()
    assert page.files_button.isEnabled()


# --- the launcher ----------------------------------------------------------------------------------------


def test_the_launcher_resolves_paths_before_changing_directory_and_has_a_start_up_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tools import databento_import_app as launcher

    args = launcher.parse_args(["a.csv", "--theme", "light", "--smoke"])
    assert args.paths == [Path("a.csv")] and args.theme == "light" and args.smoke is True
    monkeypatch.chdir(tmp_path)
    assert launcher.main(["--smoke"]) == 0
