"""Standalone drop-in window for importing Databento files (offline, read-only on the source).

Drop Databento ``mbp-10`` CSV files (or folders, or a ``.zip`` of them) on the window: every
day inside them becomes a recorded session that the rest of the app can replay. The import
itself is ``app/importers/databento.py``; this module is only the page around it.

Like the Bidirectional Lab page, it is self-driven: a worker thread does all the file work and
reports through signals, so the Qt thread only formats immutable results. It holds no backend
snapshot, touches no runtime state, and imports no live-execution code. ``render`` is a
deliberate no-op.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDragLeaveEvent, QDropEvent, QShowEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.gui import design_tokens as dt
from app.gui.screens import DashboardScreen, _value
from app.gui.theme import DASHBOARD_THEMES
from app.gui.view_models import AppSnapshot
from app.gui.widgets import (
    CapabilityEmptyState,
    Card,
    EvidenceTable,
    MetricMeter,
    StatTile,
    StatusBadge,
    stat_grid,
)
from app.importers.databento import ImportOptions, ImportResult, ProgressEvent, import_many

DEFAULT_OUT_ROOT = Path("data/raw")

_RESULT_TEXT = {
    "imported": "IMPORTED", "duplicate": "ALREADY IMPORTED", "skipped": "SKIPPED",
    "refused": "NOT IMPORTED", "failed": "FAILED", "cancelled": "STOPPED",
}
_EMPTY_ROW = ("—", "no files imported yet", "—", "—")
_FILE_FILTER = "Databento files (*.csv *.csv.zst *.csv.gz *.zip);;All files (*)"


class _ImportWorker(QThread):
    """Runs the import off the GUI thread; reports through signals and never raises."""

    progress = Signal(object)  # ProgressEvent
    result = Signal(object)    # ImportResult, as soon as each exists
    done = Signal(object, str)  # every ImportResult, and a closing note

    def __init__(self, paths: Sequence[Path], options: ImportOptions) -> None:
        super().__init__()
        self._paths = tuple(paths)
        self._options = options
        self._stop = threading.Event()

    def stop(self) -> None:
        """Ask the import to stop at its next safe point (finished days are kept)."""
        self._stop.set()

    def run(self) -> None:  # noqa: D401 - QThread entry point
        results: list[ImportResult] = []
        note = ""
        try:
            results = import_many(
                self._paths, self._options,
                progress=self.progress.emit, cancel=self._stop.is_set, on_result=self.result.emit,
            )
            if any(item.status == "imported" for item in results) and not self._stop.is_set():
                self.progress.emit(ProgressEvent("finishing", message="Updating the volatility history"))
                note = _refresh_volatility(self._options.out_root)
        except Exception as error:  # noqa: BLE001 - report, never crash the GUI
            failed = ImportResult(
                "failed", Path("."), f"The import stopped unexpectedly: {type(error).__name__}: {error}",
            )
            self.result.emit(failed)
            results.append(failed)
        self.done.emit(results, note)


def _refresh_volatility(raw_root: Path) -> str:
    """Bring the volatility-regime history up to date so the new days can be labelled.

    Offline and read-only on the recordings; a failure here never fails the import.
    """
    try:
        from app.research.session_volatility import default_cache_path, update_cache

        summary = update_cache(raw_root, default_cache_path(raw_root))
    except Exception as error:  # noqa: BLE001 - optional step
        return (
            f"The volatility history was not updated ({type(error).__name__}). "
            "Run py -m tools.session_volatility later."
        )
    unreadable = f" {len(summary.errors)} could not be read." if summary.errors else ""
    return f"Volatility history updated: {summary.analysed} session(s) analysed.{unreadable}"


def _detail(result: ImportResult) -> str:
    """What happened to one session or file, in plain words."""
    if result.status == "imported":
        stats = result.stats
        parts = [f"{stats.get('symbol', '')}: {int(stats.get('depth_changes', 0)):,} book changes, "
                 f"{int(stats.get('trades', 0)):,} trades."]
        if result.replay_ok:
            parts.append("Usable for replay." + (" Model training allowed." if result.training_ok else ""))
            if result.problems:
                parts.append("Problems: " + " ".join(result.problems))
        else:
            parts.append("Not usable for replay: " + "; ".join(result.replay_reasons or result.problems) + ".")
        return " ".join(parts)
    prefix = f"{result.day}: "
    text = result.headline[len(prefix):] if result.day and result.headline.startswith(prefix) else result.headline
    # The File column already names the file, so the sentence need not start with it again.
    return re.sub(r"^.{1,200}? was not imported\. ", "", text)


def _result_text(result: ImportResult) -> str:
    text = _RESULT_TEXT.get(result.status, result.status.upper())
    if result.status == "imported" and (result.problems or not result.replay_ok):
        return "IMPORTED WITH PROBLEMS"
    return text


class DatabentoImportScreen(DashboardScreen):
    """Drop Databento files here; every day in them becomes a replayable session."""

    def __init__(self, out_root: Path = DEFAULT_OUT_ROOT) -> None:
        super().__init__(
            "screen_databento_import", "Import Databento data",
            "Drop your Databento files here. Every day in them becomes a recorded session you can replay. "
            "Your original files are only read, never changed.",
        )
        self._out_root = Path(out_root)
        self._worker: _ImportWorker | None = None
        self._results: list[ImportResult] = []
        self._note = ""

        add = Card("Add files", "import_drop_card")
        self.drop_area = CapabilityEmptyState("import_drop_area")
        add.body.addWidget(self.drop_area)
        buttons = QHBoxLayout()
        self.files_button = QPushButton("Choose files…")
        self.files_button.setObjectName("import_choose_files")
        self.files_button.setProperty("variant", "primary")
        self.files_button.clicked.connect(self._choose_files)
        self.folder_button = QPushButton("Choose folder…")
        self.folder_button.setObjectName("import_choose_folder")
        self.folder_button.setProperty("variant", "secondary")
        self.folder_button.clicked.connect(self._choose_folder)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("import_stop")
        self.stop_button.setProperty("variant", "secondary")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_clicked)
        for button in (self.files_button, self.folder_button, self.stop_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        add.body.addLayout(buttons)
        self.training_option = QCheckBox("Let machine learning train on this data")
        self.training_option.setObjectName("import_allow_training")
        add.body.addWidget(self.training_option)
        training_note = QLabel(
            "Off by default. Imported data can always be replayed and analysed; tick this only if you want "
            "models trained on it. Imported order books show 10 price levels, live recordings up to 20."
        )
        training_note.setObjectName("import_training_note")
        training_note.setWordWrap(True)
        training_note.setProperty("role", "muted")
        add.body.addWidget(training_note)
        self.content_layout.addWidget(add)

        progress = Card("Progress", "import_progress_card")
        state_row = QHBoxLayout()
        self.state_badge = StatusBadge("READY", "import_state", "neutral")
        self.status_line = _value("import_status", "Waiting for files.")
        self.status_line.setWordWrap(True)
        state_row.addWidget(self.state_badge, 0, Qt.AlignmentFlag.AlignVCenter)  # keep the pill its own height
        state_row.addWidget(self.status_line, 1)
        progress.body.addLayout(state_row)
        self.rows_meter = MetricMeter("Rows", "import_rows_meter")
        progress.body.addWidget(self.rows_meter)
        self.content_layout.addWidget(progress)

        results = Card("Results", "import_results_card")
        self.imported_tile = StatTile("Sessions imported", "import_imported")
        self.already_tile = StatTile("Already imported", "import_already")
        self.refused_tile = StatTile("Not imported", "import_refused")
        self.trades_tile = StatTile("Trades imported", "import_trades")
        results.body.addLayout(stat_grid(
            (self.imported_tile, self.already_tile, self.refused_tile, self.trades_tile), 4))
        self.summary = _value("import_summary", "Nothing imported yet.")
        self.summary.setWordWrap(True)
        results.body.addWidget(self.summary)
        self.results_table = EvidenceTable(("Day", "File", "Result", "What happened"), "import_results_table")
        self.results_table.setMinimumHeight(dt.SPACE_HUGE * 5)  # a month of days must not be squeezed to a few rows
        results.body.addWidget(self.results_table)
        self.content_layout.addWidget(results)

        info = Card("Where it goes", "import_info_card")
        info_text = QLabel(
            f"Sessions are saved under {self._out_root}, one folder per day, and show up on the Sessions and "
            "Replay page of the main app within about half a minute. Importing the same data twice is skipped, "
            "so it is safe to drop a file again."
        )
        info_text.setObjectName("import_info_text")
        info_text.setWordWrap(True)
        info.body.addWidget(info_text)
        self.content_layout.addWidget(info)
        self.content_layout.addStretch(1)  # short cards keep their own height instead of filling the window

        self._set_idle()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        # The table measured its columns before the theme's header padding existed.
        self.results_table.set_rows(self._table_rows())

    # -- the window feeds this page, not the backend snapshot ------------------------------

    def render(self, snapshot: AppSnapshot) -> None:
        """Self-driven: the import worker feeds this page; nothing is rendered per frame."""
        return None

    @property
    def running(self) -> bool:
        """True while an import is in progress."""
        return self._worker is not None and self._worker.isRunning()

    def show_drag(self, active: bool) -> None:
        """Tell the user the files will be taken if they let go (text only; no extra styling)."""
        if self.running:
            return
        if active:
            self.drop_area.set_reason("Release to import", "The files will be read and imported straight away.")
        else:
            self._set_idle()

    def start_import(self, paths: Sequence[Path]) -> None:
        """Begin importing what was dropped or chosen. A second request while busy is declined."""
        if not paths:
            return
        if self.running:
            self.status_line.setText("An import is already running. Drop the files again when it has finished.")
            return
        options = ImportOptions(out_root=self._out_root, allow_model_training=self.training_option.isChecked())
        self._results = []
        self._note = ""
        self.results_table.set_rows(self._table_rows())
        self._update_tiles()
        self._set_busy()
        self.state_badge.set_status("READING", "neutral")
        self.status_line.setText("Looking at your files…")
        self.rows_meter.set_fraction(0.0, "—")
        worker = _ImportWorker(paths, options)
        worker.progress.connect(self.apply_progress)
        worker.result.connect(self.apply_result)
        worker.done.connect(self.apply_done)
        self._worker = worker
        worker.start()

    def stop_import(self) -> None:
        """Stop at the next safe point; days already imported stay, the day in progress is dropped."""
        if self.running and self._worker is not None:
            self._worker.stop()
            self.stop_button.setEnabled(False)
            self.state_badge.set_status("STOPPING", "warn")
            self.status_line.setText("Stopping… the day in progress will not be saved.")

    def wait_until_stopped(self, timeout_ms: int = 20_000) -> bool:
        """Block (used when the window closes) until the worker has ended; False on timeout."""
        return self._worker is None or self._worker.wait(timeout_ms)

    # -- applying immutable results (Qt thread, no I/O) ----------------------------------------

    def apply_progress(self, event: ProgressEvent) -> None:
        where = f"File {event.file_index} of {event.file_count} · " if event.file_count > 1 else ""
        if event.stage == "scanning":
            self.state_badge.set_status("READING", "neutral")
            self.status_line.setText(f"{where}Reading {event.file} to see which days it holds…")
            self.rows_meter.set_fraction(0.0, f"{event.done:,} rows scanned")
        elif event.stage == "converting":
            self.state_badge.set_status("IMPORTING", "neutral")
            self.status_line.setText(f"{where}{event.message}")
            fraction = event.done / event.total if event.total else 0.0
            self.rows_meter.set_fraction(fraction, f"{event.done:,} of {event.total:,}")
        elif event.stage == "finishing":
            self.status_line.setText(event.message or "Saving…")
        else:
            self.status_line.setText(event.message or "Checking your files…")

    def apply_result(self, result: ImportResult) -> None:
        self._results.append(result)
        self.results_table.set_rows(self._table_rows())
        self._update_tiles()

    def apply_done(self, results: list[ImportResult], note: str) -> None:
        self._note = note
        self._set_idle()
        counts = self._counts()
        if counts["cancelled"]:
            state, text = ("STOPPED", "warn"), "Stopped."
        elif counts["failed"]:
            state, text = ("FAILED", "fail"), "Finished with errors."
        elif not counts["imported"] and not counts["duplicate"]:
            state, text = ("NOTHING IMPORTED", "warn"), "Nothing was imported."
        elif counts["with_problems"]:
            state, text = ("FINISHED WITH PROBLEMS", "warn"), "Finished."
        else:
            state, text = ("FINISHED", "ok"), "Finished."
        self.state_badge.set_status(*state)
        self.status_line.setText(text)
        if counts["cancelled"]:
            self.rows_meter.set_fraction(self.rows_meter.bar.value() / 1000, "stopped")
        elif counts["imported"] or counts["duplicate"]:
            self.rows_meter.set_fraction(1.0, "done")
        else:
            self.rows_meter.set_fraction(0.0, "—")
        parts = [f"{counts['imported']} session(s) imported ({counts['usable']} usable for replay)"]
        if counts["duplicate"]:
            parts.append(f"{counts['duplicate']} already imported")
        if counts["refused"] or counts["failed"]:
            parts.append(f"{counts['refused'] + counts['failed']} not imported")
        if counts["skipped"]:
            parts.append(f"{counts['skipped']} file(s) skipped because they are not needed")
        self.summary.setText(", ".join(parts) + "." + (f" {note}" if note else ""))
        self.results_table.set_rows(self._table_rows())
        self._sync_summary()

    # -- internals ----------------------------------------------------------------------------

    def _table_rows(self) -> tuple[tuple[str, ...], ...]:
        return tuple(
            (item.day or "—", item.source.name or "—", _result_text(item), _detail(item))
            for item in self._results
        ) or (_EMPTY_ROW,)

    def _counts(self) -> dict[str, int]:
        by_status = {status: sum(r.status == status for r in self._results) for status in _RESULT_TEXT}
        imported = [r for r in self._results if r.status == "imported"]
        return {
            **by_status,
            "usable": sum(bool(r.replay_ok) for r in imported),
            "with_problems": sum(bool(r.problems) or not r.replay_ok for r in imported),
        }

    def _update_tiles(self) -> None:
        counts = self._counts()
        trades = sum(int(r.stats.get("trades", 0)) for r in self._results if r.status == "imported")
        self.imported_tile.set_value(f"{counts['imported']:,}", f"{counts['usable']:,} usable for replay")
        self.already_tile.set_value(f"{counts['duplicate']:,}", "skipped, nothing repeated")
        self.refused_tile.set_value(f"{counts['refused'] + counts['failed']:,}", "see the table below")
        self.trades_tile.set_value(f"{trades:,}", "across the imported sessions")
        self._sync_summary()

    def _set_busy(self) -> None:
        self.files_button.setEnabled(False)
        self.folder_button.setEnabled(False)
        self.training_option.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.drop_area.set_reason("Importing…", "You can drop more files when this finishes.")

    def _set_idle(self) -> None:
        self.files_button.setEnabled(True)
        self.folder_button.setEnabled(True)
        self.training_option.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.drop_area.set_reason(
            "Drop files here",
            "Databento .csv, .csv.zst or .csv.gz files, folders of them, or a .zip. One file can hold a whole month.",
        )
        self.results_table.set_rows(self._table_rows())

    def _sync_summary(self) -> None:
        counts = self._counts()
        lines = [
            f"Import: {self.state_badge.text()}   {self.status_line.text()}",
            f"Sessions imported {counts['imported']}   already imported {counts['duplicate']}   "
            f"not imported {counts['refused'] + counts['failed']}",
            *[f"  {r.day or '—'}  {r.source.name}  {_result_text(r)}  {_detail(r)}" for r in self._results[:40]],
        ]
        if self._note:
            lines += ["", self._note]
        self.set_summary(lines)

    def _choose_files(self) -> None:
        downloads = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        chosen, _ = QFileDialog.getOpenFileNames(self, "Choose Databento files", downloads, _FILE_FILTER)
        self.start_import([Path(name) for name in chosen])

    def _choose_folder(self) -> None:
        downloads = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        chosen = QFileDialog.getExistingDirectory(self, "Choose a folder of Databento files", downloads)
        if chosen:
            self.start_import([Path(chosen)])

    def _stop_clicked(self) -> None:
        self.stop_import()


class DatabentoImportWindow(QMainWindow):
    """The drop-in window: the page above, the shared dashboard theme, and drag-and-drop."""

    def __init__(
        self,
        *,
        out_root: Path = DEFAULT_OUT_ROOT,
        initial_paths: Sequence[Path] = (),
        theme: str = "dark",
    ) -> None:
        super().__init__()
        self.setWindowTitle("Import Databento data")
        self.resize(980, 760)
        self.setMinimumSize(720, 560)
        self.setAcceptDrops(True)
        self._theme = "dark"

        root = QWidget()
        root.setObjectName("root")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(dt.SPACE_MD, dt.SPACE_SM, dt.SPACE_MD, dt.SPACE_XS)
        layout.setSpacing(dt.SPACE_XS)
        self.page = DatabentoImportScreen(out_root)
        layout.addWidget(self.page, 1)
        self.setCentralWidget(root)
        self.apply_theme(theme)
        if initial_paths:
            paths = list(initial_paths)
            QTimer.singleShot(0, lambda: self.page.start_import(paths))

    # -- drag and drop: only reads the dropped paths; the worker does every file operation -------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - Qt API
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.page.show_drag(True)
        else:
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:  # noqa: N802 - Qt API
        self.page.show_drag(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt API
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        event.acceptProposedAction()
        self.page.show_drag(False)
        self.page.start_import(paths)

    # -- appearance and lifecycle ----------------------------------------------------------------

    def apply_theme(self, theme: str) -> None:
        """Apply the authoritative dark or light dashboard palette."""
        self._theme = theme if theme in DASHBOARD_THEMES else "dark"
        self.setStyleSheet(DASHBOARD_THEMES[self._theme])

    @property
    def theme(self) -> str:
        """Return the active theme name."""
        return self._theme

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        """Never close on a running worker: ask it to stop (finished days stay) and wait for it."""
        if self.page.running:
            self.page.stop_import()
            if not self.page.wait_until_stopped():
                event.ignore()
                return
        super().closeEvent(event)
