from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from app.database.recorder import MarketSessionRecorder
from app.machine_learning.challenger_pipeline import (
    DatasetBuildConfig,
    build_challenger_dataset,
)
from app.machine_learning.session_training import SessionTrainingConfig

import pytest
UTC = timezone.utc
_REQUIRED = "aggregated_depth,trades,aggressor_side,source_timestamps"


def _session(raw: Path, day: int, *, protocol: str = "1.2", provider: str = "pytest") -> Path:
    start = datetime(2026, 7, day, 14, 30, tzinfo=UTC)
    rec = MarketSessionRecorder(
        root_dir=raw, session_start_utc=start, compact_on_finalize=False,
    )
    rec.record_control_event({
        "type": "delayed_mode", "timestamp_ns": 1, "delay_minutes": 15,
    })
    rec.record_control_event({
        "type": "connected", "timestamp_ns": 2, "source_mode": "delayed",
        "protocol_version": protocol, "provider": provider,
        "stream_id": f"stream-{day}", "connection_id": f"connection-{day}",
        "session_id": f"bridge-session-{day}", "capabilities": _REQUIRED,
        "handshake_accepted": protocol == "1.2", "dropped_message_count": 0,
        "stream_sequence": 1,
    })
    base = int(start.timestamp() * 1_000_000_000)
    sequence = 1
    for index in range(420):
        phase = index % 80
        ticks = phase if phase <= 40 else 80 - phase
        price = Decimal("29500") + Decimal("0.25") * ticks
        timestamp = base + index * 500_000_000
        sequence += 1
        rec.record({
            "type": "depth_update", "timestamp": timestamp, "symbol": "MNQ",
            "side": "bid" if index % 2 else "ask", "price": str(price),
            "previous_size": "0", "new_size": str(index % 20 + 1),
            "stream_sequence": sequence,
        })
        sequence += 1
        rec.record({
            "type": "trade", "timestamp_ns": timestamp + 1, "price": str(price),
            "size": "2", "aggressor_side": "buy" if index % 2 else "sell",
            "instrument": "MNQ", "sequence_id": index + 1,
            "stream_sequence": sequence,
        })
    rec.finalize(clean_shutdown=True)
    return rec.session_dir


def _config() -> DatasetBuildConfig:
    return DatasetBuildConfig(SessionTrainingConfig(
        target_ticks=Decimal("6"), stop_ticks=Decimal("6"),
        horizon_seconds=20.0, sample_interval_seconds=5.0,
        window_span_seconds=20.0, warmup_seconds=10.0,
    ))


def test_only_model_eligible_sessions_enter_the_dataset(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    eligible = _session(raw, 15)
    ineligible = _session(raw, 16, protocol="1.1")

    result = build_challenger_dataset(raw, config=_config())

    included = result.manifest["included_sessions"]
    excluded = result.manifest["excluded_sessions"]
    assert [item["session_id"] for item in included] == [eligible.name]
    assert [item["session_id"] for item in excluded] == [ineligible.name]
    assert not Path(excluded[0]["manifest_path"]).is_absolute()
    assert any("protocol 1.2" in reason for reason in excluded[0]["reasons"])
    assert result.rows
    assert {row["source_session_id"] for row in result.rows} == {eligible.name}


def test_unchanged_inputs_rebuild_to_identical_dataset_id(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    _session(raw, 15)
    output = tmp_path / "models"

    first = build_challenger_dataset(raw, output_root=output, config=_config())
    second = build_challenger_dataset(raw, output_root=output, config=_config())

    assert first.dataset_id == second.dataset_id
    assert first.manifest["rows_sha256"] == second.manifest["rows_sha256"]
    assert first.dataset_path == second.dataset_path
    assert first.dataset_path is not None and first.dataset_path.is_file()
    assert first.manifest_path is not None and first.manifest_path.is_file()


def test_dataset_id_is_independent_of_raw_root_location(tmp_path: Path) -> None:
    """Relocating identical eligible/excluded evidence must not change its ID."""
    first_root = tmp_path / "first" / "raw"
    _session(first_root, 15)
    _session(first_root, 16, protocol="1.1")
    second_root = tmp_path / "second" / "raw"
    shutil.copytree(first_root, second_root)

    first = build_challenger_dataset(first_root, config=_config())
    second = build_challenger_dataset(second_root, config=_config())

    assert first.dataset_id == second.dataset_id
    assert first.manifest == second.manifest


def test_parts_only_finalized_session_is_supported(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    session = _session(raw, 15)
    assert not (session / "trades.parquet").exists()
    assert (session / "trade_parts").is_dir()

    result = build_challenger_dataset(raw, config=_config())

    files = result.manifest["included_sessions"][0]["input_files"]
    assert any("trade_parts/part-" in item["path"] for item in files)
    assert any("depth_parts/part-" in item["path"] for item in files)


def test_dataset_manifest_contains_reproducibility_contract(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    _session(raw, 15)

    result = build_challenger_dataset(raw, config=_config())
    manifest = result.manifest

    assert manifest["schema_version"] == 1
    assert manifest["feature_contract_version"] == "shared-causal-market-features-v2"
    assert manifest["feature_contract"]["columns"] == manifest["feature_columns"]
    assert manifest["feature_contract"]["runtime_effect"] == "none; feature construction only"
    assert manifest["feature_contract_sha256"]
    assert manifest["label_contract_version"] == "causal-triple-barrier-v2-resolution-provenance"
    assert manifest["row_count"] == len(result.rows)
    assert manifest["positive_labels"] + manifest["negative_labels"] == len(result.rows)
    assert manifest["minimum_timestamp_ns"] < manifest["maximum_timestamp_ns"]
    assert manifest["dataset_id"].startswith("dataset-")


def test_immutable_dataset_path_refuses_different_content(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    _session(raw, 15)
    output = tmp_path / "models"
    first = build_challenger_dataset(raw, output_root=output, config=_config())
    assert first.manifest_path is not None
    first.manifest_path.write_text(json.dumps({"tampered": True}), encoding="utf-8")

    try:
        build_challenger_dataset(raw, output_root=output, config=_config())
    except FileExistsError as error:
        assert "immutable dataset path" in str(error)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("a modified immutable manifest must be rejected")


def test_validation_barriers_must_match_dataset_labels(tmp_path: Path) -> None:
    from app.machine_learning.challenger_pipeline import build_validated_challenger

    raw = tmp_path / "raw"
    _session(raw, 15)
    try:
        build_validated_challenger(
            raw, tmp_path / "models", config=_config(),
            target_ticks=12.0, stop_ticks=6.0,
        )
    except ValueError as error:
        assert "target_ticks must match" in str(error)
    else:  # pragma: no cover
        raise AssertionError("validation geometry must match label geometry")


def test_validation_rejects_negative_costs_before_building(tmp_path: Path) -> None:
    from app.machine_learning.challenger_pipeline import build_validated_challenger

    with pytest.raises(ValueError, match="cost_ticks must be non-negative"):
        build_validated_challenger(
            tmp_path / "raw",
            tmp_path / "models",
            cost_ticks=-1.0,
        )


def test_source_parts_added_during_build_abort_publication(tmp_path: Path, monkeypatch) -> None:
    import shutil
    import app.machine_learning.challenger_pipeline as pipeline

    raw = tmp_path / "raw"
    session = _session(raw, 15)
    original = pipeline.build_session_training_rows

    def mutate_after_read(*args, **kwargs):
        result = original(*args, **kwargs)
        source = next((session / "trade_parts").glob("part-*.parquet"))
        shutil.copy2(source, session / "trade_parts/part-999999.parquet")
        return result

    monkeypatch.setattr(pipeline, "build_session_training_rows", mutate_after_read)
    try:
        pipeline.build_challenger_dataset(raw, output_root=tmp_path / "models", config=_config())
    except RuntimeError as error:
        assert "changed during dataset build" in str(error)
    else:  # pragma: no cover
        raise AssertionError("a newly added source part must abort publication")


def test_failed_validation_writes_attempt_but_no_registry(tmp_path: Path) -> None:
    from app.machine_learning.challenger_pipeline import build_validated_challenger

    raw = tmp_path / "raw"
    _session(raw, 15)
    models = tmp_path / "models"
    result = build_validated_challenger(
        raw, models, config=_config(), min_train_days=3, min_oos_predictions=50,
        target_ticks=6.0, stop_ticks=6.0,
    )
    assert result["status"] == "rejected"
    assert list((models / "attempts").glob("*.json"))
    assert not (models / "registry").exists()
    assert not (models / "challengers").exists()


def test_passing_fixture_creates_loadable_unapproved_challenger(
    tmp_path: Path, monkeypatch,
) -> None:
    from app.machine_learning.challenger_pipeline import build_validated_challenger
    from app.machine_learning.pooled_training import WalkForwardResult
    from app.machine_learning.registry import read_registry
    from app.machine_learning.train import load_model_artifact

    raw = tmp_path / "raw"
    for day in range(14, 19):
        _session(raw, day)
    passed = WalkForwardResult(
        total_rows=400, trading_days=5, evaluated_days=2, oos_predictions=80,
        base_rate=0.5, model_accuracy=0.6, brier_score=0.2,
        taken_trades=40, taken_win_rate=0.7, expectancy_ticks=2.0,
        baseline_expectancy_ticks=0.0, beats_baseline=True,
        fold_boundaries=({"train_end_day": "2026-07-16", "test_day": "2026-07-17"},),
        note="fixture passed predeclared gates",
    )
    monkeypatch.setattr(
        "app.machine_learning.pooled_training.walk_forward_evaluate",
        lambda *args, **kwargs: passed,
    )
    models = tmp_path / "models"
    result = build_validated_challenger(
        raw, models, config=_config(), min_train_days=2, min_oos_predictions=1,
        target_ticks=6.0, stop_ticks=6.0, cost_ticks=2.0,
    )
    assert result["status"] == "challenger"
    payload = load_model_artifact(Path(result["model_path"]))
    assert payload["model_type"] == "logistic_regression"
    records = read_registry(models)
    assert len(records) == 1
    assert records[0].artifact_id == result["artifact_id"]
    assert records[0].runtime_loaded is False
    assert records[0].shadow_predictions == 0
    assert records[0].decision_impact == "none"
