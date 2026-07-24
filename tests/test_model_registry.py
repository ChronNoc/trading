from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.machine_learning.registry import (
    ModelApproval,
    ModelRegistryRecord,
    read_model_approval,
    read_registry,
    register_challenger,
    validate_explicit_approval,
)


import pytest

def _record(**overrides: object) -> ModelRegistryRecord:
    base = ModelRegistryRecord(
        artifact_id="challenger-abc",
        artifact_sha256="a" * 64,
        dataset_id="dataset-abc",
        dataset_sha256="b" * 64,
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
        artifact_path="challengers/challenger-abc/model.joblib",
    )
    return replace(base, **overrides)


def _evidence(models_root: Path) -> ModelRegistryRecord:
    """Publish one internally consistent content-addressed evidence graph."""
    import hashlib
    import json

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

    import joblib
    from sklearn.linear_model import LogisticRegression

    from app.machine_learning.feature_contract import FEATURE_COLUMNS, FEATURE_CONTRACT_VERSION

    model_path_staging = models_root / "model-staging.joblib"
    joblib.dump({
        "model": LogisticRegression(),
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
    bundle_core: dict[str, object] = {
        "dataset_id": dataset_id,
        "dataset_sha256": dataset_sha,
        "model_sha256": model_sha,
        "model_type": "logistic_regression",
        "model_version": "0.1.0",
        "feature_contract_version": "shared-causal-market-features-v2",
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


def test_registry_is_immutable_and_idempotent(tmp_path: Path) -> None:
    record = _evidence(tmp_path)
    first = register_challenger(tmp_path, record)
    second = register_challenger(tmp_path, record)
    assert first == second
    assert read_registry(tmp_path) == (record,)

    try:
        register_challenger(tmp_path, replace(record, validation_detail="changed"))
    except ValueError as error:
        assert "validation detail" in str(error)
    else:  # pragma: no cover
        raise AssertionError("registry evidence mismatch must be refused")


def test_registry_read_rejects_tampered_model_bytes(tmp_path: Path) -> None:
    record = _evidence(tmp_path)
    register_challenger(tmp_path, record)
    artifact = tmp_path / record.artifact_path
    artifact.write_bytes(b"tampered-model")

    try:
        read_registry(tmp_path)
    except ValueError as error:
        assert "SHA mismatch" in str(error)
    else:  # pragma: no cover
        raise AssertionError("tampered model bytes must invalidate registry reads")


def test_registry_read_rejects_tampered_bundle_evidence(tmp_path: Path) -> None:
    import json

    record = _evidence(tmp_path)
    register_challenger(tmp_path, record)
    bundle_path = tmp_path / "challengers" / record.artifact_id / "bundle_manifest.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    bundle["model_version"] = "9.9.9"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    with pytest.raises(ValueError, match="content-addressed artifact ID"):
        read_registry(tmp_path)


def test_registry_read_rejects_tampered_dataset_bytes(tmp_path: Path) -> None:
    record = _evidence(tmp_path)
    register_challenger(tmp_path, record)
    dataset = tmp_path / "datasets" / record.dataset_id / "dataset.jsonl"
    dataset.write_bytes(dataset.read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="dataset bytes"):
        read_registry(tmp_path)


def test_registry_read_rejects_tampered_validation_evidence(tmp_path: Path) -> None:
    import json

    record = _evidence(tmp_path)
    register_challenger(tmp_path, record)
    validation_path = tmp_path / "challengers" / record.artifact_id / "validation.json"
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    validation["note"] = "tampered"
    validation_path.write_text(json.dumps(validation), encoding="utf-8")

    with pytest.raises(ValueError, match="bundle validation"):
        read_registry(tmp_path)


def test_registry_read_rejects_runtime_claims_in_json(tmp_path: Path) -> None:
    import json
    from dataclasses import asdict

    registry = tmp_path / "registry"
    registry.mkdir(parents=True)
    payload = asdict(_record(runtime_loaded=True, decision_impact="strategy"))
    (registry / "challenger-abc.json").write_text(json.dumps(payload), encoding="utf-8")

    try:
        read_registry(tmp_path)
    except ValueError as error:
        assert "runtime use" in str(error)
    else:  # pragma: no cover
        raise AssertionError("tampered runtime claims must be rejected")


def test_default_approval_is_null_and_runtime_disabled(tmp_path: Path) -> None:
    approval = read_model_approval(tmp_path / "missing.yaml")
    assert approval == ModelApproval()
    state = validate_explicit_approval((_record(),), approval)
    assert state.registry_state == "CHALLENGER"
    assert state.approval_state == "NOT_APPROVED"


def test_multiple_unapproved_challengers_are_not_silently_selected() -> None:
    first = _record(artifact_id="challenger-first")
    second = _record(artifact_id="challenger-second")

    state = validate_explicit_approval((first, second), ModelApproval())

    assert state.registry_state == "CHALLENGER"
    assert state.approval_state == "NOT_APPROVED"
    assert state.record is None
    assert "no artifact inferred" in state.detail


def test_exact_id_and_sha_are_both_required() -> None:
    record = _record()
    partial = ModelApproval(approved_artifact_id=record.artifact_id)
    assert validate_explicit_approval((record,), partial).approval_state == "INVALID"

    mismatch = ModelApproval(
        approved_artifact_id=record.artifact_id,
        approved_sha256="d" * 64,
    )
    assert "SHA" in validate_explicit_approval((record,), mismatch).detail


def test_exact_approval_never_enables_unimplemented_runtime_loading() -> None:
    record = _record()
    approval = ModelApproval(
        approved_artifact_id=record.artifact_id,
        approved_sha256=record.artifact_sha256,
    )
    state = validate_explicit_approval((record,), approval)
    assert state.approval_state == "APPROVED_RUNTIME_DISABLED"
    assert state.record is record
    assert state.record.runtime_loaded is False
    assert state.record.decision_impact == "none"

    unsafe = replace(approval, runtime_loading_enabled=True)
    blocked = validate_explicit_approval((record,), unsafe)
    assert blocked.approval_state == "INVALID"
    assert "not implemented" in blocked.detail


def test_failed_validation_cannot_be_approved() -> None:
    record = _record(validation_state="REJECTED", beats_baseline=False)
    approval = ModelApproval(
        approved_artifact_id=record.artifact_id,
        approved_sha256=record.artifact_sha256,
    )
    assert "failed validation" in validate_explicit_approval((record,), approval).detail
