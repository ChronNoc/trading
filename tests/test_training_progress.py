"""The training progress bar: the run's progress file, how it is read, and how the page shows it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.machine_learning.training_progress import (
    STALE_AFTER_SECONDS,
    TrainingProgress,
    TrainingProgressView,
    read_training_progress,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def run_halfway(path: Path, clock: Clock) -> TrainingProgress:
    progress = TrainingProgress(path, clock=clock)
    progress.plan(2)
    progress.begin_candidate("12-tick target / 8-tick stop")
    progress.plan_sessions([("a", 600), ("b", 400)])
    progress.begin_session("a", 0)
    clock.now += 60
    progress.events(300)
    return progress


def test_progress_is_events_replayed_over_every_size(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "training_progress.json"
    run_halfway(path, clock)
    view = read_training_progress(path, now=clock.now)
    assert view is not None and view.state == "running"
    # size 1 of 2, 300 of 1000 events: 0.3 x 0.97 of the first half
    assert view.fraction == pytest.approx(0.3 * 0.97 / 2)
    assert view.headline == "Model 1 of 2 (12-tick target / 8-tick stop) - building data: session 1 of 2"
    assert view.detail.startswith("15% - about ") and view.detail.endswith("min left")


def test_frequent_reports_are_written_at_most_about_once_a_second(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "training_progress.json"
    progress = run_halfway(path, clock)
    written = json.loads(path.read_text())["events_done"]
    progress.events(350)                    # same instant: not written yet
    assert json.loads(path.read_text())["events_done"] == written
    clock.now += 1.5
    progress.events(400)
    assert json.loads(path.read_text())["events_done"] == 400


def test_later_stages_finished_sizes_and_the_end(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "training_progress.json"
    progress = run_halfway(path, clock)
    progress.stage("training and testing on unseen days")
    view = read_training_progress(path, now=clock.now)
    assert view.fraction == pytest.approx(0.97 / 2) and "training and testing" in view.headline
    progress.end_candidate("does not beat take-everything after costs (40 out-of-sample trades)")
    progress.begin_candidate("8-tick target / 8-tick stop")
    view = read_training_progress(path, now=clock.now)
    assert view.headline.startswith("Model 2 of 2") and view.fraction == pytest.approx(0.5)
    assert view.results == ("12-tick target / 8-tick stop: does not beat take-everything after costs "
                            "(40 out-of-sample trades)",)
    progress.finish("1 size(s) trained")
    view = read_training_progress(path, now=clock.now)
    assert (view.state, view.fraction, view.headline) == ("finished", 1.0, "Training finished")


def test_a_run_that_stopped_writing_shows_as_stopped_and_a_failure_says_why(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "training_progress.json"
    progress = run_halfway(path, clock)
    later = clock.now + STALE_AFTER_SECONDS + 120
    view = read_training_progress(path, now=later)
    assert view.state == "stopped" and "closed or the PC restarted" in view.detail
    progress.fail("MemoryError: out of memory")
    view = read_training_progress(path, now=clock.now)
    assert view.state == "failed" and view.detail == "MemoryError: out of memory"


@pytest.mark.parametrize("content", [None, "", "not json", "[]", '{"candidates_total": "x"}'])
def test_no_file_or_an_unreadable_one_shows_nothing(tmp_path: Path, content: str | None) -> None:
    path = tmp_path / "training_progress.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    assert read_training_progress(path) is None


def test_the_dataset_build_reports_each_session_and_its_events(tmp_path: Path) -> None:
    from app.machine_learning.challenger_pipeline import build_challenger_dataset
    from tests.test_challenger_pipeline import _config, _session

    raw = tmp_path / "raw"
    _session(raw, 15)
    _session(raw, 16)
    clock = Clock()
    path = tmp_path / "training_progress.json"
    progress = TrainingProgress(path, clock=clock)
    progress.plan(1)
    progress.begin_candidate("test size")
    plain = build_challenger_dataset(raw, config=_config())
    reported = build_challenger_dataset(raw, config=_config(), progress=progress)
    assert reported.manifest == plain.manifest and reported.rows == plain.rows   # reporting changes nothing
    state = json.loads(path.read_text())
    assert state["sessions_total"] == 2 and state["session_index"] == 2
    assert 0 < state["events_total"] and state["events_before_session"] < state["events_total"]


# --- the page ---------------------------------------------------------------------------------------


def test_the_status_file_never_carries_training_progress_and_old_files_still_decode() -> None:
    from dataclasses import replace

    from app.gui.snapshot_codec import decode_snapshot, encode_snapshot
    from app.gui.view_models import AppSnapshot

    view = TrainingProgressView("running", 0.4, "Model 1 of 3", "40%", ())
    encoded = encode_snapshot(replace(AppSnapshot(), training=view))
    assert '"training"' not in encoded
    assert decode_snapshot(encoded).training is None


def test_the_gui_reads_training_progress_beside_the_backend_status(tmp_path: Path) -> None:
    import time

    from app.gui.file_snapshot import FileSnapshotProvider

    clock = Clock()
    clock.now = time.time() - 60          # the provider reads with the real clock
    run_halfway(tmp_path / "training_progress.json", clock)
    snapshot = FileSnapshotProvider(tmp_path)()     # no backend status file at all
    assert snapshot.training is not None and snapshot.training.headline.startswith("Model 1 of 2")


@pytest.mark.parametrize(("view", "badge", "state"), [
    (None, "NOT RUNNING", "neutral"),
    (TrainingProgressView("running", 0.25, "Model 1 of 3 - building data: session 2 of 6", "25% - about 40 min left",
                          ()), "RUNNING", "neutral"),
    (TrainingProgressView("finished", 1.0, "Training finished", "3 size(s) trained", ("12/8: beats",)),
     "FINISHED", "ok"),
    (TrainingProgressView("stopped", 0.3, "Training stopped at 30%", "no update for 9 min", ()), "STOPPED", "warn"),
    (TrainingProgressView("failed", 0.3, "Training failed at 30%", "MemoryError", ()), "FAILED", "fail"),
])
def test_the_research_page_shows_the_bar_badge_and_results(qtbot, view, badge: str, state: str) -> None:
    from dataclasses import replace

    from app.gui.screens import ResearchHealthScreen
    from app.gui.view_models import AppSnapshot

    screen = ResearchHealthScreen()
    qtbot.addWidget(screen)
    screen.render(replace(AppSnapshot(), training=view))
    assert screen.training_badge.text() == badge and screen.training_badge.property("state") == state
    if view is None:
        assert screen.training_meter.bar.value() == 0 and screen.training_meter.value.text() == "—"
    else:
        assert screen.training_meter.bar.value() == round(view.fraction * 1000)
        assert screen.training_meter.value.text() == view.detail
        assert screen.training_headline.text() == view.headline
        assert ("Finished — 12/8: beats" in screen.training_results.text()) is bool(view.results)


def test_a_refused_write_never_stops_the_run(tmp_path: Path, monkeypatch) -> None:
    """2026-11-10: Windows refused to replace the file while the GUI read it; that error failed
    two training sizes. Reporting must never raise."""
    import os as os_module

    import app.machine_learning.training_progress as module

    clock = Clock()
    path = tmp_path / "training_progress.json"
    progress = run_halfway(path, clock)
    real_replace = os_module.replace
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args)
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(module.os, "replace", refuse)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    progress.stage("training and testing on unseen days")       # must not raise
    progress.end_candidate("x")
    assert len(calls) == 2 * module._REPLACE_ATTEMPTS            # retried, then skipped
    monkeypatch.setattr(module.os, "replace", real_replace)
    progress.finish("done")
    assert json.loads(path.read_text())["status"] == "finished"  # the next write lands


def test_a_size_beyond_the_plan_grows_the_total(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "training_progress.json"
    progress = TrainingProgress(path, clock=clock)
    progress.plan(1)
    progress.begin_candidate("a")
    progress.end_candidate("failed (PermissionError)")
    progress.begin_candidate("b")
    view = read_training_progress(path, now=clock.now)
    assert view.headline.startswith("Model 2 of 2")


def test_the_gui_keeps_the_last_reading_when_a_read_is_caught_mid_replace(tmp_path: Path, monkeypatch) -> None:
    import time

    import app.gui.file_snapshot as file_snapshot

    clock = Clock()
    clock.now = time.time() - 60
    run_halfway(tmp_path / "training_progress.json", clock)
    provider = file_snapshot.FileSnapshotProvider(tmp_path)
    first = provider().training
    monkeypatch.setattr(file_snapshot, "read_training_progress", lambda path: None)
    assert provider().training == first
