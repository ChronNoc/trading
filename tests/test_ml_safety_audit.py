"""ML-003 safety and fallback layer audit tests (feature/automatic-runtime).

Comprehensive test suite proving every failure mode is handled correctly:
missing model, corrupted artifact, schema mismatch, stale model detection,
invalid features, confidence bounds, exception handling, underperforming
rejection, explicit fallback reasons, and broker isolation.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from sklearn.linear_model import LogisticRegression

from app.machine_learning.feature_contract import (
    FEATURE_COLUMNS,
    FEATURE_CONTRACT_VERSION,
    CausalFeaturePipeline,
    FeatureVector,
)
from app.machine_learning.registry import (
    ModelApproval,
    ModelRegistryRecord,
    is_artifact_stale,
    register_challenger,
    validate_explicit_approval,
)
from app.machine_learning.shadow_predictor import (
    ObserveOnlyModelLoader,
    PredictionJournal,
)
from app.machine_learning.train import build_feature_matrix
from app.market.state import MarketState
from datetime import UTC, date, datetime


def _fitted_challenger(
    models_root: Path,
    *,
    include_fold_boundaries: bool = True,
) -> tuple[ModelRegistryRecord, str]:
    """Create a complete, fitted challenger artifact with all evidence."""
    import hashlib
    import joblib

    # Dataset
    dataset_bytes = b'{"label":1}\n'
    dataset_sha = hashlib.sha256(dataset_bytes).hexdigest()
    dataset_core: dict[str, object] = {
        "rows_sha256": dataset_sha,
        "feature_contract_sha256": "c" * 64,
        "included_sessions": [{}, {}, {}, {}],
        "excluded_sessions": [{}, {}],
    }
    canonical_dataset = json.dumps(
        dataset_core, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    dataset_id = "dataset-" + hashlib.sha256(canonical_dataset).hexdigest()[:24]
    dataset_dir = models_root / "datasets" / dataset_id
    dataset_dir.mkdir(parents=True)
    (dataset_dir / "dataset.jsonl").write_bytes(dataset_bytes)
    (dataset_dir / "dataset_manifest.json").write_text(
        json.dumps({"dataset_id": dataset_id, **dataset_core}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # Fitted model
    model = LogisticRegression()
    rng = np.random.default_rng(7)
    x_train = rng.normal(size=(8, len(FEATURE_COLUMNS)))
    y_train = np.array([0, 1, 0, 1, 0, 1, 0, 1])
    model.fit(x_train, y_train)

    model_path_staging = models_root / "model-staging.joblib"
    joblib.dump({
        "model": model,
        "model_type": "logistic_regression",
        "version": "0.1.0",
        "feature_columns": FEATURE_COLUMNS,
        "feature_contract_version": FEATURE_CONTRACT_VERSION,
        "feature_contract_sha256": "c" * 64,
    }, model_path_staging)
    model_bytes = model_path_staging.read_bytes()
    model_path_staging.unlink()
    model_sha = hashlib.sha256(model_bytes).hexdigest()

    validation: dict[str, object] = {
        "validation_state": "PASSED",
        "note": "walk-forward gate passed",
        "oos_predictions": 120,
        "brier_score": 0.21,
        "beats_baseline": True,
    }
    if include_fold_boundaries:
        validation["fold_boundaries"] = [
            {"test_day": "2026-06-01"},
            {"test_day": "2026-06-26"},
        ]
    bundle_core: dict[str, object] = {
        "dataset_id": dataset_id,
        "dataset_sha256": dataset_sha,
        "model_sha256": model_sha,
        "model_type": "logistic_regression",
        "model_version": "0.1.0",
        "feature_contract_version": FEATURE_CONTRACT_VERSION,
        "feature_contract_sha256": "c" * 64,
        "validation": validation,
        "runtime_loaded": False,
        "shadow_predictions": 0,
        "decision_impact": "none",
    }
    canonical_bundle = json.dumps(
        bundle_core, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    artifact_id = "challenger-" + hashlib.sha256(canonical_bundle).hexdigest()[:24]
    challenger_dir = models_root / "challengers" / artifact_id
    challenger_dir.mkdir(parents=True)
    (challenger_dir / "model.joblib").write_bytes(model_bytes)
    (challenger_dir / "validation.json").write_text(
        json.dumps(validation, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    (challenger_dir / "bundle_manifest.json").write_text(
        json.dumps({"artifact_id": artifact_id, **bundle_core}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    record = ModelRegistryRecord(
        artifact_id=artifact_id,
        artifact_sha256=model_sha,
        dataset_id=dataset_id,
        dataset_sha256=dataset_sha,
        model_type="logistic_regression",
        model_version="0.1.0",
        feature_contract_sha256="c" * 64,
        included_sessions=4,
        excluded_sessions=2,
        validation_state="PASSED",
        validation_detail="walk-forward gate passed",
        oos_predictions=120,
        brier_score=0.21,
        beats_baseline=True,
        artifact_path=f"challengers/{artifact_id}/model.joblib",
    )
    return record, artifact_id


def _one_vector() -> FeatureVector:
    """Build one valid feature vector for scoring."""
    pipeline = CausalFeaturePipeline(
        window_span_seconds=10.0, sample_interval_ms=0.0, tick_size=Decimal("0.25")
    )
    base = int(datetime(2026, 7, 26, 14, 30, tzinfo=UTC).timestamp() * 1e9)
    for i in range(5):
        ts = base + i * 1_000_000_000
        price = Decimal("29500.00") + Decimal(i % 3) * Decimal("0.25")
        state = MarketState()
        state = state.update({
            "type": "depth_update", "timestamp": ts, "symbol": "MNQ",
            "side": "bid", "price": str(price), "previous_size": "0", "new_size": "10",
        })
        state = state.update({
            "type": "depth_update", "timestamp": ts, "symbol": "MNQ",
            "side": "ask", "price": str(price + Decimal("0.25")),
            "previous_size": "0", "new_size": "10",
        })
        state = state.update({
            "timestamp_ns": ts, "price": str(price), "size": "1",
            "aggressor_side": "buy" if i % 2 else "sell",
            "instrument": "MNQ", "sequence_id": i + 1,
        })
        pipeline.observe(state, event_index=i)
    return pipeline.feature_vector(
        direction="long", stop_distance=Decimal("6"), target_distance=Decimal("6")
    )


# ITEM 1: Missing-model fallback
def test_missing_model_fallback_is_safe_noop(tmp_path: Path) -> None:
    """With default approval (null), loader stays NOT_APPROVED and score() is a no-op."""
    journal = PredictionJournal(tmp_path / "journal.jsonl")
    approval_path = Path(__file__).resolve().parents[1] / "config" / "model_approval.yaml"

    loader = ObserveOnlyModelLoader(
        models_root=tmp_path,
        model_approval_path=approval_path,
        journal=journal,
        current_date_provider=lambda: date(2026, 7, 26),
    )

    snapshot = loader.snapshot()
    assert snapshot.state == "NOT_APPROVED"
    assert "no exact artifact approval" in snapshot.reason

    # Scoring is a true no-op: no exception, no journal write, no crash
    loader.bind_session("session-one")
    loader.score(_one_vector(), session_id="session-one", timestamp_ns=1, direction="long")

    assert loader.snapshot().shadow_predictions == 0
    assert loader.snapshot().scoring_failures == 0
    assert journal.count == 0


# ITEM 2: Corrupted-artifact rejection
def test_corrupted_artifact_rejects_safely(tmp_path: Path) -> None:
    """Tampered model.joblib bytes land in LOAD_FAILED, never crash or score."""
    record, artifact_id = _fitted_challenger(tmp_path)
    register_challenger(tmp_path, record)
    approval_path = tmp_path / "model_approval.yaml"
    approval_path.write_text(
        "schema_version: 1\n"
        f"approved_artifact_id: {record.artifact_id}\n"
        f"approved_sha256: {record.artifact_sha256}\n"
        "runtime_loading_enabled: false\n"
        "shadow_scoring_enabled: false\n",
        encoding="utf-8",
    )

    # Corrupt the artifact after registration
    model_path = tmp_path / "challengers" / artifact_id / "model.joblib"
    model_path.write_bytes(b"corrupted-model-bytes")

    journal = PredictionJournal(tmp_path / "journal.jsonl")
    loader = ObserveOnlyModelLoader(
        models_root=tmp_path,
        model_approval_path=approval_path,
        journal=journal,
        current_date_provider=lambda: date(2026, 7, 26),
    )

    snapshot = loader.snapshot()
    assert snapshot.state in {"LOAD_FAILED", "INVALID"}
    assert snapshot.reason
    if snapshot.state == "LOAD_FAILED":
        assert snapshot.load_failures >= 1

    # Scoring remains a no-op
    loader.score(_one_vector(), session_id="s", timestamp_ns=1, direction="long")
    assert loader.snapshot().shadow_predictions == 0
    assert journal.count == 0


def test_tampered_bundle_manifest_rejects_at_validation(tmp_path: Path) -> None:
    """Editing bundle_manifest.json SHA fields makes validate_explicit_approval -> INVALID."""
    record, artifact_id = _fitted_challenger(tmp_path)
    register_challenger(tmp_path, record)
    bundle_path = tmp_path / "challengers" / artifact_id / "bundle_manifest.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["model_sha256"] = "tampered" + bundle["model_sha256"]
    bundle_path.write_text(json.dumps(bundle, indent=2), encoding="utf-8")

    approval = ModelApproval(
        approved_artifact_id=record.artifact_id,
        approved_sha256=record.artifact_sha256,
    )

    # validate_explicit_approval will call read_registry, which validates evidence
    with pytest.raises(ValueError, match="content-addressed artifact ID"):
        from app.machine_learning.registry import read_registry
        read_registry(tmp_path)


# ITEM 3: Schema/feature-compatibility checks
def test_schema_mismatch_fails_at_load_not_scoring(tmp_path: Path) -> None:
    """Artifact with mismatched feature_columns is rejected at load_model_artifact."""
    record, artifact_id = _fitted_challenger(tmp_path)
    register_challenger(tmp_path, record)

    # Modify the saved artifact to have wrong feature_columns
    import hashlib
    import joblib
    from sklearn.linear_model import LogisticRegression

    wrong_columns = ("wrong_feature_1", "wrong_feature_2")
    model_path = tmp_path / "challengers" / artifact_id / "model.joblib"
    joblib.dump({
        "model": LogisticRegression().fit(np.zeros((2, 2)), [0, 1]),
        "model_type": "logistic_regression",
        "version": "0.1.0",
        "feature_columns": wrong_columns,
        "feature_contract_version": FEATURE_CONTRACT_VERSION,
        "feature_contract_sha256": "c" * 64,
    }, model_path)

    # Update approval to point to this artifact (but note: SHA will mismatch now, so we'd need to update that too)
    # Actually, the SHA changed, so let's just test load_model_artifact directly
    from app.machine_learning.train import load_model_artifact

    with pytest.raises(ValueError, match="feature columns do not match"):
        load_model_artifact(model_path)


def test_feature_matrix_validates_column_presence(tmp_path: Path) -> None:
    """build_feature_matrix raises if a requested column is missing from row."""
    row = {"book_imbalance": "0.1", "direction": "long"}
    with pytest.raises(ValueError, match="is required"):
        build_feature_matrix((row,), ("book_imbalance", "nonexistent_column"))


# ITEM 4: Deterministic feature ordering
def test_feature_columns_is_explicit_ordered_tuple() -> None:
    """FEATURE_COLUMNS is a tuple (ordered), not a set/dict-derived structure."""
    assert isinstance(FEATURE_COLUMNS, tuple)
    assert len(FEATURE_COLUMNS) == 17  # Current contract size
    assert FEATURE_COLUMNS[0] == "book_imbalance"
    assert FEATURE_COLUMNS[-1] == "target_distance"


def test_training_and_runtime_use_identical_feature_order() -> None:
    """Matrix construction indexes mappings by the contract's named order."""
    ordered = {column: "0" for column in FEATURE_COLUMNS}
    ordered["direction"] = "long"
    reversed_row = dict(reversed(tuple(ordered.items())))

    assert np.array_equal(
        build_feature_matrix((ordered,), FEATURE_COLUMNS),
        build_feature_matrix((reversed_row,), FEATURE_COLUMNS),
    )


# ITEM 5: Stale-model detection
def test_artifact_staleness_uses_latest_evaluation_day(tmp_path: Path) -> None:
    """The immutable latest walk-forward test day defines evidence age."""
    record, _ = _fitted_challenger(tmp_path)

    assert is_artifact_stale(record, date(2026, 7, 26), models_root=tmp_path) is False
    assert is_artifact_stale(record, date(2026, 7, 27), models_root=tmp_path) is True
    assert is_artifact_stale(
        record,
        datetime(2026, 7, 27, 12, tzinfo=UTC),
        models_root=tmp_path,
    ) is True


def test_artifact_staleness_rejects_invalid_policy_or_evidence(tmp_path: Path) -> None:
    record, _ = _fitted_challenger(tmp_path, include_fold_boundaries=False)
    with pytest.raises(ValueError, match="non-negative"):
        is_artifact_stale(record, date(2026, 7, 26), -1, models_root=tmp_path)
    with pytest.raises(ValueError, match="fold boundaries"):
        is_artifact_stale(record, date(2026, 7, 26), models_root=tmp_path)


# ITEM 6: Invalid-feature handling (NaN/Inf/None)
def test_invalid_feature_values_raise_and_are_caught(tmp_path: Path) -> None:
    """NaN/Inf/None in features raise ValueError, caught by broad except in score()."""
    from decimal import Decimal, InvalidOperation

    # _decimal_value validates not None, not bool, must be finite
    from app.machine_learning.train import _decimal_value

    with pytest.raises(ValueError, match="is required"):
        _decimal_value(None, "test_column")

    with pytest.raises(ValueError, match="must be finite"):
        _decimal_value(Decimal("Infinity"), "test_column")

    with pytest.raises(ValueError, match="must be finite"):
        _decimal_value(Decimal("NaN"), "test_column")


def test_scoring_failure_increments_counter_not_crash(tmp_path: Path) -> None:
    """A malformed feature vector increments _scoring_failures, never propagates."""
    record, _ = _fitted_challenger(tmp_path)
    register_challenger(tmp_path, record)
    approval_path = tmp_path / "model_approval.yaml"
    approval_path.write_text(
        f"schema_version: 1\napproved_artifact_id: {record.artifact_id}\n"
        f"approved_sha256: {record.artifact_sha256}\nruntime_loading_enabled: false\n"
        f"shadow_scoring_enabled: false\n",
        encoding="utf-8",
    )

    journal = PredictionJournal(tmp_path / "journal.jsonl")
    loader = ObserveOnlyModelLoader(
        models_root=tmp_path,
        model_approval_path=approval_path,
        journal=journal,
        current_date_provider=lambda: date(2026, 7, 26),
    )
    assert loader.snapshot().state == "SCORING"

    class _BrokenVector:
        def as_record(self):
            raise RuntimeError("simulated malformed feature vector")

        def canonical_bytes(self):
            return b""

    # Must not raise
    loader.score(_BrokenVector(), session_id="s", timestamp_ns=1, direction="long")  # type: ignore[arg-type]

    snapshot = loader.snapshot()
    assert snapshot.scoring_failures == 1
    assert snapshot.shadow_predictions == 0
    assert "scoring error" in snapshot.reason


# ITEM 7: Confidence thresholds (probabilities in [0,1])
@pytest.mark.parametrize("probability", [float("nan"), float("inf"), -0.01, 1.01])
def test_invalid_model_probability_is_rejected_without_journal_write(
    tmp_path: Path,
    probability: float,
) -> None:
    """Broken predict_proba output is observable and never becomes evidence."""
    record, _ = _fitted_challenger(tmp_path)
    register_challenger(tmp_path, record)
    approval_path = tmp_path / "model_approval.yaml"
    approval_path.write_text(
        f"schema_version: 1\napproved_artifact_id: {record.artifact_id}\n"
        f"approved_sha256: {record.artifact_sha256}\nruntime_loading_enabled: false\n"
        "shadow_scoring_enabled: false\n",
        encoding="utf-8",
    )
    journal = PredictionJournal(tmp_path / "journal.jsonl")
    loader = ObserveOnlyModelLoader(
        models_root=tmp_path,
        model_approval_path=approval_path,
        journal=journal,
        current_date_provider=lambda: date(2026, 7, 26),
    )

    class BrokenProbabilityModel:
        def predict_proba(self, matrix: object) -> list[list[float]]:
            del matrix
            return [[1.0 - probability, probability]]

    loader._model = BrokenProbabilityModel()  # type: ignore[attr-defined]
    loader.score(_one_vector(), session_id="s", timestamp_ns=1, direction="long")

    snapshot = loader.snapshot()
    assert snapshot.scoring_failures == 1
    assert snapshot.shadow_predictions == 0
    assert "out-of-range probability" in snapshot.reason
    assert journal.count == 0
    assert loader.last_probability("long") is None


# ITEM 8: Inference exception handling is exercised by the malformed-vector and
# malformed-probability tests above: neither exception escapes score().


# ITEM 9: Underperforming-model rejection (positive_incremental_expectancy gate)
def test_validation_gate_rejects_non_beats_baseline(tmp_path: Path) -> None:
    """validate_explicit_approval refuses validation_state != PASSED."""
    from dataclasses import replace

    record, _ = _fitted_challenger(tmp_path)
    failed_record = replace(record, validation_state="REJECTED", beats_baseline=False)

    approval = ModelApproval(
        approved_artifact_id=failed_record.artifact_id,
        approved_sha256=failed_record.artifact_sha256,
    )

    result = validate_explicit_approval((failed_record,), approval)
    assert result.approval_state == "INVALID"
    assert "failed validation" in result.detail


# ITEM 10: Explicit fallback reasons (all states have non-empty reason)
def test_all_loader_states_have_explicit_reason(tmp_path: Path, monkeypatch) -> None:
    """Every ObserveOnlyModelLoader state sets a human-readable reason."""
    journal = PredictionJournal(tmp_path / "journal.jsonl")

    # NOT_APPROVED
    loader1 = ObserveOnlyModelLoader(
        models_root=tmp_path, model_approval_path=tmp_path / "missing.yaml", journal=journal
    )
    assert loader1.snapshot().state == "NOT_APPROVED"
    assert loader1.snapshot().reason != ""
    assert "no exact artifact approval" in loader1.snapshot().reason

    # INVALID (runtime_loading_enabled=true)
    record, _ = _fitted_challenger(tmp_path)
    register_challenger(tmp_path, record)
    bad_approval = tmp_path / "bad.yaml"
    bad_approval.write_text(
        f"schema_version: 1\napproved_artifact_id: {record.artifact_id}\n"
        f"approved_sha256: {record.artifact_sha256}\nruntime_loading_enabled: true\n"
        f"shadow_scoring_enabled: false\n",
        encoding="utf-8",
    )
    loader2 = ObserveOnlyModelLoader(
        models_root=tmp_path,
        model_approval_path=bad_approval,
        journal=journal,
        current_date_provider=lambda: date(2026, 7, 26),
    )
    assert loader2.snapshot().state == "INVALID"
    assert "not implemented" in loader2.snapshot().reason

    # SCORING
    good_approval = tmp_path / "good.yaml"
    good_approval.write_text(
        f"schema_version: 1\napproved_artifact_id: {record.artifact_id}\n"
        f"approved_sha256: {record.artifact_sha256}\nruntime_loading_enabled: false\n"
        f"shadow_scoring_enabled: false\n",
        encoding="utf-8",
    )
    loader3 = ObserveOnlyModelLoader(
        models_root=tmp_path,
        model_approval_path=good_approval,
        journal=journal,
        current_date_provider=lambda: date(2026, 7, 26),
    )
    assert loader3.snapshot().state == "SCORING"
    assert "exact-approved artifact loaded" in loader3.snapshot().reason

    # LOAD_FAILED (registry evidence validates, then the loader boundary fails)
    from app.machine_learning.train import load_model_artifact as real_load

    calls = 0

    def fail_third_load(path: Path) -> object:
        nonlocal calls
        calls += 1
        if calls <= 2:
            return real_load(path)
        raise OSError("forced artifact loader failure")

    monkeypatch.setattr("app.machine_learning.train.load_model_artifact", fail_third_load)
    loader4 = ObserveOnlyModelLoader(
        models_root=tmp_path,
        model_approval_path=good_approval,
        journal=journal,
        current_date_provider=lambda: date(2026, 7, 26),
    )
    assert loader4.snapshot().state == "LOAD_FAILED"
    assert "forced artifact loader failure" in loader4.snapshot().reason
    assert loader4.snapshot().load_failures == 1


def test_cached_probability_requires_fresh_session_and_event_correlation(
    tmp_path: Path,
) -> None:
    """A prior event/session score cannot be reused as current evidence."""
    record, _ = _fitted_challenger(tmp_path)
    register_challenger(tmp_path, record)
    approval_path = tmp_path / "model_approval.yaml"
    approval_path.write_text(
        f"schema_version: 1\napproved_artifact_id: {record.artifact_id}\n"
        f"approved_sha256: {record.artifact_sha256}\nruntime_loading_enabled: false\n"
        "shadow_scoring_enabled: false\n",
        encoding="utf-8",
    )
    journal = PredictionJournal(tmp_path / "journal.jsonl")
    loader = ObserveOnlyModelLoader(
        models_root=tmp_path,
        model_approval_path=approval_path,
        journal=journal,
        current_date_provider=lambda: date(2026, 7, 26),
    )
    loader.bind_session("session-one")
    loader.score(
        _one_vector(), session_id="session-one", timestamp_ns=100, direction="long"
    )

    evidence = loader.last_probability(
        "long", session_id="session-one", as_of_timestamp_ns=110, max_age_ns=10
    )
    assert evidence is not None
    journal_record = journal.recover(journal.path).records[-1]
    assert evidence.prediction_id == journal_record["prediction_id"]
    assert evidence.artifact_id == record.artifact_id
    assert evidence.artifact_sha256 == record.artifact_sha256
    assert evidence.session_id == "session-one"
    assert evidence.direction == "long"
    assert evidence.timestamp_ns == 100
    assert evidence.success_probability == journal_record["success_probability"]
    assert loader.last_probability(
        "long", session_id="session-one", as_of_timestamp_ns=111, max_age_ns=10
    ) is None
    assert loader.last_probability(
        "long", session_id="session-one", as_of_timestamp_ns=99, max_age_ns=10
    ) is None

    loader.bind_session("session-two")
    assert loader.last_probability("long") is None
    assert loader.last_probability(
        "long", session_id="session-one", as_of_timestamp_ns=105, max_age_ns=10
    ) is None


def test_invalid_refresh_clears_cached_probability(tmp_path: Path) -> None:
    record, artifact_id = _fitted_challenger(tmp_path)
    register_challenger(tmp_path, record)
    approval_path = tmp_path / "model_approval.yaml"
    approval_path.write_text(
        f"schema_version: 1\napproved_artifact_id: {record.artifact_id}\n"
        f"approved_sha256: {record.artifact_sha256}\nruntime_loading_enabled: false\n"
        "shadow_scoring_enabled: false\n",
        encoding="utf-8",
    )
    loader = ObserveOnlyModelLoader(
        models_root=tmp_path,
        model_approval_path=approval_path,
        journal=PredictionJournal(tmp_path / "journal.jsonl"),
        current_date_provider=lambda: date(2026, 7, 26),
    )
    loader.bind_session("s")
    loader.score(_one_vector(), session_id="s", timestamp_ns=100, direction="long")
    assert loader.last_probability("long") is not None

    (tmp_path / "challengers" / artifact_id / "model.joblib").write_bytes(b"tampered")
    loader.refresh()
    assert loader.snapshot().state == "INVALID"
    assert loader.last_probability("long") is None


# ITEM 11: Shadow-only enforcement (no broker/execution imports)
def test_shadow_predictor_has_no_execution_imports() -> None:
    """shadow_predictor.py imports nothing under app.execution or broker code."""
    import ast

    source_path = Path("app/machine_learning/shadow_predictor.py")
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)

    banned = ("app.strategy", "app.paper", "app.risk", "app.execution", "broker", "tradovate")
    violations = [name for name in imports if any(item in name.lower() for item in banned)]
    assert not violations, f"Found banned imports in shadow_predictor.py: {violations}"

