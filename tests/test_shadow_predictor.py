"""Exact-approval shadow model loader safety contract (ML-003).

Mirrors tests/test_feature_observer.py in structure: activation is gated
strictly on exact ID+SHA approval, scoring never propagates an exception,
and the module carries no dependency on strategy/paper/risk/execution code.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.machine_learning.feature_contract import (
    CausalFeaturePipeline,
    ObserveOnlyFeatureSink,
)
from app.machine_learning.registry import register_challenger
from app.machine_learning.session_training import SessionTrainingConfig
from app.machine_learning.shadow_predictor import (
    ObserveOnlyModelLoader,
    PredictionJournal,
    ShadowPrediction,
)
from app.market.state import MarketState

from app.machine_learning.registry import ModelRegistryRecord
from tests.test_model_registry import _evidence, _record


def _fitted_evidence(
    models_root: Path,
    *,
    include_fold_boundaries: bool = True,
) -> ModelRegistryRecord:
    """Like tests.test_model_registry._evidence, but the model is actually fit.

    The registry fixture's model is a bare, unfitted LogisticRegression()
    because those tests never call predict_proba. Scoring here does, so this
    loader-specific fixture fits it on tiny synthetic data first.
    """
    import hashlib
    import json

    import joblib
    import numpy as np
    from sklearn.linear_model import LogisticRegression

    from app.machine_learning.feature_contract import FEATURE_COLUMNS, FEATURE_CONTRACT_VERSION

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
        validation["fold_boundaries"] = [{"test_day": "2026-07-24"}]
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
    return _record(
        artifact_id=artifact_id,
        artifact_sha256=model_sha,
        dataset_id=dataset_id,
        dataset_sha256=dataset_sha,
        artifact_path=f"challengers/{artifact_id}/model.joblib",
    )


def _config() -> SessionTrainingConfig:
    return SessionTrainingConfig(
        target_ticks=Decimal("6"),
        stop_ticks=Decimal("6"),
        warmup_seconds=2.0,
        sample_interval_seconds=1.0,
        window_span_seconds=10.0,
        sample_interval_ms=0.0,
    )


def _states(base_ns: int, count: int) -> tuple[MarketState, ...]:
    state = MarketState()
    states: list[MarketState] = []
    for index in range(count):
        ts = base_ns + index * 1_000_000_000
        price = Decimal("29500.00") + Decimal(index % 3) * Decimal("0.25")
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
            "aggressor_side": "buy" if index % 2 else "sell",
            "instrument": "MNQ", "sequence_id": index + 1,
        })
        states.append(state)
    return tuple(states)


def _one_feature_vector():
    pipeline = CausalFeaturePipeline(window_span_seconds=10.0, sample_interval_ms=0.0, tick_size=Decimal("0.25"))
    base = int(datetime(2026, 7, 24, 14, 30, tzinfo=UTC).timestamp() * 1e9)
    for index, state in enumerate(_states(base, 5)):
        pipeline.observe(state, event_index=index)
    return pipeline.feature_vector(direction="long", stop_distance=Decimal("6"), target_distance=Decimal("6"))


def _approved(models_root: Path) -> tuple[str, str]:
    """Register one passing, actually-fitted challenger and write exact approval. Returns (id, sha)."""
    record = _fitted_evidence(models_root)
    register_challenger(models_root, record)
    approval_path = models_root / "model_approval.yaml"
    approval_path.write_text(
        "schema_version: 1\n"
        f"approved_artifact_id: {record.artifact_id}\n"
        f"approved_sha256: {record.artifact_sha256}\n"
        "runtime_loading_enabled: false\n"
        "shadow_scoring_enabled: false\n",
        encoding="utf-8",
    )
    return record.artifact_id, record.artifact_sha256


def test_no_approval_configured_stays_unloaded_and_never_scores(tmp_path: Path) -> None:
    journal = PredictionJournal(tmp_path / "journal.jsonl")
    loader = ObserveOnlyModelLoader(
        models_root=tmp_path, model_approval_path=tmp_path / "missing.yaml", journal=journal,
    )
    snapshot = loader.snapshot()
    assert snapshot.state == "NOT_APPROVED"
    assert snapshot.shadow_predictions == 0

    loader.bind_session("session-one")
    loader.score(_one_feature_vector(), session_id="session-one", timestamp_ns=1, direction="long")

    assert loader.snapshot().shadow_predictions == 0
    assert journal.count == 0


def test_registered_but_unapproved_challenger_never_scores(tmp_path: Path) -> None:
    record = _evidence(tmp_path)
    register_challenger(tmp_path, record)
    journal = PredictionJournal(tmp_path / "journal.jsonl")
    loader = ObserveOnlyModelLoader(
        models_root=tmp_path, model_approval_path=tmp_path / "missing.yaml", journal=journal,
    )
    assert loader.snapshot().state == "NOT_APPROVED"
    loader.score(_one_feature_vector(), session_id="session-one", timestamp_ns=1, direction="long")
    assert loader.snapshot().shadow_predictions == 0


def test_exact_approval_scores_and_preserves_legacy_direct_impact_marker(tmp_path: Path) -> None:
    artifact_id, artifact_sha = _approved(tmp_path)
    journal = PredictionJournal(tmp_path / "journal.jsonl")
    loader = ObserveOnlyModelLoader(
        models_root=tmp_path,
        model_approval_path=tmp_path / "model_approval.yaml",
        journal=journal,
        current_date_provider=lambda: datetime(2026, 7, 24, tzinfo=UTC).date(),
    )
    snapshot = loader.snapshot()
    assert snapshot.state == "SCORING"
    assert snapshot.artifact_id == artifact_id
    assert snapshot.artifact_sha256 == artifact_sha

    loader.bind_session("session-one")
    loader.score(_one_feature_vector(), session_id="session-one", timestamp_ns=123, direction="long")

    scored = loader.snapshot()
    assert scored.shadow_predictions == 1
    assert scored.decision_impact == "none"
    assert journal.count == 1

    records = json.loads((tmp_path / "journal.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert records["artifact_id"] == artifact_id
    assert records["artifact_sha256"] == artifact_sha
    assert records["session_id"] == "session-one"
    assert records["direction"] == "long"
    assert records["decision_impact"] == "none"
    assert isinstance(records["success_probability"], float)


def test_stale_exact_approval_unloads_and_never_scores(tmp_path: Path) -> None:
    artifact_id, artifact_sha = _approved(tmp_path)
    journal = PredictionJournal(tmp_path / "journal.jsonl")
    loader = ObserveOnlyModelLoader(
        models_root=tmp_path,
        model_approval_path=tmp_path / "model_approval.yaml",
        journal=journal,
        current_date_provider=lambda: datetime(2026, 8, 24, tzinfo=UTC).date(),
    )

    snapshot = loader.snapshot()
    assert snapshot.state == "STALE"
    assert snapshot.artifact_id == ""
    assert snapshot.artifact_sha256 == ""
    assert "30 days" in snapshot.reason
    assert artifact_id not in snapshot.reason
    assert artifact_sha not in snapshot.reason

    loader.score(
        _one_feature_vector(),
        session_id="session-one",
        timestamp_ns=123,
        direction="long",
    )
    assert journal.count == 0


def test_refresh_unloads_artifact_when_immutable_evidence_becomes_stale(tmp_path: Path) -> None:
    artifact_id, _ = _approved(tmp_path)
    as_of = datetime(2026, 8, 23, tzinfo=UTC).date()
    loader = ObserveOnlyModelLoader(
        models_root=tmp_path,
        model_approval_path=tmp_path / "model_approval.yaml",
        journal=PredictionJournal(tmp_path / "journal.jsonl"),
        current_date_provider=lambda: as_of,
    )
    assert loader.snapshot().state == "SCORING"
    assert loader.snapshot().artifact_id == artifact_id

    as_of = datetime(2026, 8, 24, tzinfo=UTC).date()
    loader.refresh()

    snapshot = loader.snapshot()
    assert snapshot.state == "STALE"
    assert snapshot.artifact_id == ""
    assert loader.last_probability("long") is None


def test_missing_staleness_evidence_is_invalid_and_never_scores(tmp_path: Path) -> None:
    record = _fitted_evidence(tmp_path, include_fold_boundaries=False)
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
    journal = PredictionJournal(tmp_path / "journal.jsonl")
    loader = ObserveOnlyModelLoader(
        models_root=tmp_path,
        model_approval_path=approval_path,
        journal=journal,
        current_date_provider=lambda: datetime(2026, 7, 24, tzinfo=UTC).date(),
    )

    snapshot = loader.snapshot()
    assert snapshot.state == "INVALID"
    assert "fold boundaries" in snapshot.reason
    loader.score(
        _one_feature_vector(),
        session_id="session-one",
        timestamp_ns=123,
        direction="long",
    )
    assert journal.count == 0


def test_negative_maximum_artifact_age_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        ObserveOnlyModelLoader(
            models_root=tmp_path,
            model_approval_path=tmp_path / "missing.yaml",
            journal=PredictionJournal(tmp_path / "journal.jsonl"),
            max_artifact_age_days=-1,
        )


def test_invalid_current_date_provider_fails_closed(tmp_path: Path) -> None:
    _approved(tmp_path)
    loader = ObserveOnlyModelLoader(
        models_root=tmp_path,
        model_approval_path=tmp_path / "model_approval.yaml",
        journal=PredictionJournal(tmp_path / "journal.jsonl"),
        current_date_provider=lambda: "2026-07-24",  # type: ignore[return-value]
    )

    snapshot = loader.snapshot()
    assert snapshot.state == "INVALID"
    assert "current_date_provider must return datetime.date" in snapshot.reason


def test_runtime_loading_and_shadow_scoring_flags_remain_rejected(tmp_path: Path) -> None:
    """The loader activates on exact approval alone; the two booleans stay a future cycle's gate."""
    record = _evidence(tmp_path)
    register_challenger(tmp_path, record)
    approval_path = tmp_path / "model_approval.yaml"
    approval_path.write_text(
        "schema_version: 1\n"
        f"approved_artifact_id: {record.artifact_id}\n"
        f"approved_sha256: {record.artifact_sha256}\n"
        "runtime_loading_enabled: true\n"
        "shadow_scoring_enabled: true\n",
        encoding="utf-8",
    )
    journal = PredictionJournal(tmp_path / "journal.jsonl")
    loader = ObserveOnlyModelLoader(models_root=tmp_path, model_approval_path=approval_path, journal=journal)
    snapshot = loader.snapshot()
    assert snapshot.state == "INVALID"
    assert "not implemented" in snapshot.reason


def test_tampered_artifact_bytes_report_load_failed_and_never_raise(tmp_path: Path) -> None:
    artifact_id, _sha = _approved(tmp_path)
    tampered_path = tmp_path / "challengers" / artifact_id / "model.joblib"
    tampered_path.write_bytes(b"tampered-model-bytes")

    journal = PredictionJournal(tmp_path / "journal.jsonl")
    loader = ObserveOnlyModelLoader(
        models_root=tmp_path,
        model_approval_path=tmp_path / "model_approval.yaml",
        journal=journal,
        current_date_provider=lambda: datetime(2026, 7, 24, tzinfo=UTC).date(),
    )
    snapshot = loader.snapshot()
    assert snapshot.state in {"LOAD_FAILED", "INVALID"}

    loader.score(_one_feature_vector(), session_id="s", timestamp_ns=1, direction="long")
    assert loader.snapshot().shadow_predictions == 0
    assert journal.count == 0


def test_scoring_exception_is_caught_counted_and_never_propagates(tmp_path: Path) -> None:
    _approved(tmp_path)
    journal = PredictionJournal(tmp_path / "journal.jsonl")
    loader = ObserveOnlyModelLoader(
        models_root=tmp_path,
        model_approval_path=tmp_path / "model_approval.yaml",
        journal=journal,
        current_date_provider=lambda: datetime(2026, 7, 24, tzinfo=UTC).date(),
    )
    assert loader.snapshot().state == "SCORING"

    class _BrokenVector:
        def as_record(self):
            raise RuntimeError("simulated malformed feature vector")

        def canonical_bytes(self):
            return b""

    # Must not raise.
    loader.score(_BrokenVector(), session_id="s", timestamp_ns=1, direction="long")  # type: ignore[arg-type]

    snapshot = loader.snapshot()
    assert snapshot.scoring_failures == 1
    assert snapshot.shadow_predictions == 0
    assert "scoring error" in snapshot.reason


def test_journal_tolerates_a_torn_final_line(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = PredictionJournal(path)
    journal.append(ShadowPrediction(
        prediction_id="p-1", artifact_id="challenger-x", artifact_sha256="a" * 64,
        session_id="s", timestamp_ns=1, direction="long",
        feature_vector_sha256="b" * 64, success_probability=0.5,
    ))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"broken json')  # torn tail, no trailing newline

    recovery = PredictionJournal.recover(path)
    assert recovery.damaged_tail is True
    assert len(recovery.records) == 1

    reopened = PredictionJournal(path)
    assert reopened.count == 1


def test_no_strategy_paper_risk_execution_or_broker_imports() -> None:
    source = Path("app/machine_learning/shadow_predictor.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    banned = ("app.strategy", "app.paper", "app.risk", "app.execution", "broker", "tradovate")
    assert not [name for name in imports if any(item in name.lower() for item in banned)]


def test_feature_sink_forwards_vectors_to_attached_loader(tmp_path: Path) -> None:
    _approved(tmp_path)
    journal = PredictionJournal(tmp_path / "journal.jsonl")
    loader = ObserveOnlyModelLoader(
        models_root=tmp_path,
        model_approval_path=tmp_path / "model_approval.yaml",
        journal=journal,
        current_date_provider=lambda: datetime(2026, 7, 24, tzinfo=UTC).date(),
    )
    sink = ObserveOnlyFeatureSink(config=_config(), prediction_sink=loader)
    sink.bind_session("session-one")
    base = int(datetime(2026, 7, 24, 14, 30, tzinfo=UTC).timestamp() * 1e9)

    for index, state in enumerate(_states(base, 5)):
        sink.ingest({"index": index}, state)

    feature_snapshot = sink.snapshot()
    loader_snapshot = loader.snapshot()
    assert feature_snapshot.feature_observations == 6  # three cadence ticks, long + short
    assert loader_snapshot.shadow_predictions == feature_snapshot.feature_observations
    assert loader_snapshot.decision_impact == "none"


def test_feature_sink_without_a_loader_is_unaffected() -> None:
    """Default (no prediction_sink) reproduces prior ObserveOnlyFeatureSink behavior exactly."""
    sink = ObserveOnlyFeatureSink(config=_config())
    sink.bind_session("session-one")
    base = int(datetime(2026, 7, 24, 14, 30, tzinfo=UTC).timestamp() * 1e9)
    for index, state in enumerate(_states(base, 5)):
        sink.ingest({"index": index}, state)
    snapshot = sink.snapshot()
    assert snapshot.state == "OBSERVING"
    assert snapshot.feature_observations == 6
