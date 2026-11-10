"""More kinds of model in the offline pipeline, and the dataset reuse that keeps them cheap."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.machine_learning.pooled_training import MODEL_KINDS, make_classifier, walk_forward_evaluate
from tests.test_challenger_pipeline import _config, _session


@pytest.fixture(scope="module")
def raw_sessions(tmp_path_factory) -> Path:
    raw = tmp_path_factory.mktemp("kinds") / "raw"
    for day in (13, 14, 15, 16, 17):
        _session(raw, day)
    return raw


def test_every_model_kind_is_deterministic_and_single_threaded() -> None:
    assert MODEL_KINDS == ("logistic_regression", "gradient_boosting", "random_forest")
    forest = make_classifier("random_forest")
    assert forest.n_jobs == 1 and forest.random_state == 7
    assert make_classifier("gradient_boosting").early_stopping is False
    logistic = make_classifier("logistic_regression")
    assert (logistic.solver, logistic.max_iter, logistic.random_state) == ("liblinear", 1_000, 7)  # unchanged
    with pytest.raises(ValueError, match="unknown model kind"):
        make_classifier("neural_network")


@pytest.mark.parametrize("kind", MODEL_KINDS)
def test_every_kind_trains_on_earlier_days_and_scores_later_ones(raw_sessions: Path, kind: str) -> None:
    from app.machine_learning.challenger_pipeline import build_challenger_dataset

    rows = list(build_challenger_dataset(raw_sessions, config=_config()).rows)
    result = walk_forward_evaluate(rows, target_ticks=6.0, stop_ticks=6.0, min_train_days=3, model_kind=kind)
    assert result.oos_predictions > 0 and result.evaluated_days >= 1
    again = walk_forward_evaluate(rows, target_ticks=6.0, stop_ticks=6.0, min_train_days=3, model_kind=kind)
    assert again == result     # fixed seeds: the same answer every time


def test_a_model_kind_is_recorded_on_the_attempt(raw_sessions: Path, tmp_path: Path) -> None:
    from app.machine_learning.challenger_pipeline import build_validated_challenger

    models = tmp_path / "models"
    result = build_validated_challenger(raw_sessions, models, config=_config(), min_train_days=3,
                                        min_oos_predictions=10_000, target_ticks=6.0, stop_ticks=6.0,
                                        model_kind="random_forest")
    attempt = json.loads(next((models / "attempts").glob("*.json")).read_text(encoding="utf-8"))
    assert result["status"] == "rejected" and attempt["model_type"] == "random_forest"


# --- a dataset is built once per size and reused only when its inputs are identical -----------------


def test_an_identical_dataset_is_reused_with_the_same_rows_and_results(raw_sessions: Path, tmp_path: Path) -> None:
    from app.machine_learning.challenger_pipeline import build_challenger_dataset
    from app.machine_learning.training_progress import TrainingProgress

    models = tmp_path / "models"
    built = build_challenger_dataset(raw_sessions, output_root=models, config=_config())
    progress = TrainingProgress(tmp_path / "progress.json")
    progress.plan(1)
    progress.begin_candidate("x")
    reused = build_challenger_dataset(raw_sessions, output_root=models, config=_config(), progress=progress)
    assert json.loads(progress.path.read_text())["stage"].startswith("reusing")
    assert reused.dataset_id == built.dataset_id and reused.manifest["rows_sha256"] == built.manifest["rows_sha256"]
    fresh = walk_forward_evaluate(list(built.rows), target_ticks=6.0, stop_ticks=6.0, min_train_days=3)
    again = walk_forward_evaluate(list(reused.rows), target_ticks=6.0, stop_ticks=6.0, min_train_days=3)
    assert again == fresh


def test_a_changed_session_file_or_setting_means_a_fresh_build(tmp_path: Path) -> None:
    from app.machine_learning.challenger_pipeline import DatasetBuildConfig, build_challenger_dataset
    from app.machine_learning.session_training import SessionTrainingConfig
    from decimal import Decimal

    raw = tmp_path / "raw"
    for day in (13, 14, 15):
        _session(raw, day)
    models = tmp_path / "models"
    first = build_challenger_dataset(raw, output_root=models, config=_config())
    other = DatasetBuildConfig(SessionTrainingConfig(
        target_ticks=Decimal("8"), stop_ticks=Decimal("6"), horizon_seconds=20.0, sample_interval_seconds=5.0,
        window_span_seconds=20.0, warmup_seconds=10.0))
    assert build_challenger_dataset(raw, output_root=models, config=other).dataset_id != first.dataset_id
    _session(raw, 16)                                                         # a new eligible session
    assert build_challenger_dataset(raw, output_root=models, config=_config()).dataset_id != first.dataset_id


def test_a_tampered_published_dataset_is_never_reused(tmp_path: Path) -> None:
    from app.machine_learning.challenger_pipeline import build_challenger_dataset

    raw = tmp_path / "raw"
    for day in (13, 14, 15):
        _session(raw, day)
    models = tmp_path / "models"
    first = build_challenger_dataset(raw, output_root=models, config=_config())
    rows_file = first.dataset_path
    rows_file.write_text(rows_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    # The rows hash no longer matches, so the file is not trusted: the dataset is built again from
    # the sessions, and publishing then refuses to overwrite the changed file (published datasets
    # are immutable), loudly instead of training on altered rows.
    with pytest.raises(FileExistsError, match="immutable dataset path already differs"):
        build_challenger_dataset(raw, output_root=models, config=_config())


# --- the program proposes every kind at every size --------------------------------------------------


def test_the_research_grid_covers_every_model_kind_and_keeps_logistic_ids(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import RESEARCH_GRID, _candidate, build_store, run_proposer

    proposed = run_proposer(store_root=tmp_path / "store", now_ns=100, software_revision="rev")
    assert proposed == len(RESEARCH_GRID) * len(MODEL_KINDS)
    candidates = build_store(tmp_path / "store").list_candidates()
    assert {c.definition["model"] for c in candidates} == set(MODEL_KINDS)
    logistic = _candidate(12, 8, now_ns=100, software_revision="rev")
    assert logistic.definition["model"] == "logistic_regression"
    assert logistic.hypothesis.startswith("A logistic model on pooled")     # unchanged wording, unchanged id
    assert logistic.candidate_id in {c.candidate_id for c in candidates}


# --- the header shows training on every page --------------------------------------------------------


def test_the_header_shows_training_on_every_page_only_while_a_run_exists(qtbot) -> None:
    from dataclasses import replace

    from app.gui.app_window import AppWindow
    from app.gui.screens import SCREEN_ORDER
    from app.gui.view_models import AppSnapshot
    from app.machine_learning.training_progress import TrainingProgressView

    window = AppWindow(snapshot_provider=AppSnapshot, start_timer=False)
    qtbot.addWidget(window)
    window.refresh_from_snapshot()
    assert window._training_badge.isHidden()
    view = TrainingProgressView("running", 0.42, "Model 2 of 9 (random forest, 8-tick target / 8-tick stop)",
                                "42% - about 2 h 10 min left", ())
    window.submit_snapshot(replace(AppSnapshot(), training=view))
    for name in SCREEN_ORDER:                       # the header strip sits above every page
        window.navigate_to(name)
        assert not window._training_badge.isHidden()
    assert window._training_badge.text() == "TRAINING 42%"
    assert "random forest" in window._training_badge.toolTip()
    assert "Training: 42% - about 2 h 10 min left" in window._status_label.text()
    window.submit_snapshot(replace(AppSnapshot(), training=replace(view, state="finished", fraction=1.0)))
    assert window._training_badge.text() == "TRAINING DONE" and window._training_badge.property("state") == "ok"
    assert "Training:" not in window._status_label.text()


def test_training_can_be_limited_to_one_code_versions_candidates(tmp_path: Path) -> None:
    """Every code version proposes the grid again; a run trains its own version's copies only."""
    from app.research.autonomous_proposer import (
        advance_data_validation,
        advance_offline_training,
        build_store,
        run_proposer,
    )
    from tests.test_autonomous_proposer import _raw_with_sessions

    store_root = tmp_path / "store"
    raw = _raw_with_sessions(tmp_path / "raw", 2)
    run_proposer(store_root=store_root, now_ns=1, software_revision="old")
    run_proposer(store_root=store_root, now_ns=2, software_revision="new")
    store = build_store(store_root)
    advance_data_validation(store, raw_root=raw, now_ns=3)
    trained: list[str] = []

    def trainer(candidate) -> dict[str, float]:
        trained.append(candidate.software_revision)
        return {"oos_predictions": 60.0}

    advance_offline_training(store, trainer=trainer, now_ns=4, software_revision="new")
    assert trained and set(trained) == {"new"} and len(trained) == len(MODEL_KINDS) * 3
