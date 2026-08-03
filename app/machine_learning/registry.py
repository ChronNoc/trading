"""Immutable challenger registry and exact, human-controlled approval truth.

Registry records are offline evidence only. This module never loads a model into
the runtime and has no dependency on strategy, paper, risk, or execution code.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import yaml

from app.database.recorder import atomic_write_text
from app.machine_learning.feature_contract import FEATURE_CONTRACT_VERSION

RegistryState = Literal["NOT_REGISTERED", "CHALLENGER", "INVALID"]
ApprovalState = Literal["NOT_APPROVED", "APPROVED_RUNTIME_DISABLED", "INVALID"]


@dataclass(frozen=True, slots=True)
class ModelRegistryRecord:
    artifact_id: str
    artifact_sha256: str
    dataset_id: str
    dataset_sha256: str
    model_type: str
    model_version: str
    feature_contract_sha256: str
    included_sessions: int
    excluded_sessions: int
    validation_state: str
    validation_detail: str
    oos_predictions: int
    brier_score: float
    beats_baseline: bool
    artifact_path: str
    status: str = "challenger"
    runtime_loaded: bool = False
    shadow_predictions: int = 0
    decision_impact: str = "none"


@dataclass(frozen=True, slots=True)
class ModelApproval:
    schema_version: int = 1
    approved_artifact_id: str | None = None
    approved_sha256: str | None = None
    runtime_loading_enabled: bool = False
    shadow_scoring_enabled: bool = False


def _validate_registry_record(record: ModelRegistryRecord) -> None:
    if record.status != "challenger":
        raise ValueError("registry record status must be challenger")
    if record.runtime_loaded or record.shadow_predictions or record.decision_impact != "none":
        raise ValueError("offline registry record cannot claim runtime use or decision impact")
    if record.validation_state != "PASSED" or not record.beats_baseline:
        raise ValueError("registry challenger must carry passed validation evidence")


def _load_json_object(path: Path, *, description: str) -> dict[str, object]:
    if not path.is_file():
        raise ValueError(f"missing {description}: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{description} must be a JSON object: {path}")
    return payload


def _validate_record_evidence(models_root: Path, record: ModelRegistryRecord) -> None:
    """Bind registry claims to immutable model, bundle, validation, and dataset bytes."""
    root = models_root.resolve()
    artifact_path = (models_root / record.artifact_path).resolve()
    if not artifact_path.is_relative_to(root):
        raise ValueError("registry artifact path escapes models root")
    if not artifact_path.is_file() or sha256_file(artifact_path) != record.artifact_sha256:
        raise ValueError(f"registry artifact SHA mismatch: {record.artifact_id}")

    challenger_dir = artifact_path.parent
    if challenger_dir.name != record.artifact_id:
        raise ValueError("registry artifact path does not match artifact ID")
    bundle = _load_json_object(
        challenger_dir / "bundle_manifest.json",
        description="challenger bundle manifest",
    )
    validation = _load_json_object(
        challenger_dir / "validation.json",
        description="challenger validation evidence",
    )
    if bundle.get("artifact_id") != record.artifact_id:
        raise ValueError("bundle artifact ID does not match registry")
    bundle_core = dict(bundle)
    bundle_core.pop("artifact_id", None)
    recomputed_artifact_id = "challenger-" + hashlib.sha256(
        json.dumps(
            bundle_core,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:24]
    if recomputed_artifact_id != record.artifact_id:
        raise ValueError("bundle content does not match content-addressed artifact ID")
    expected_bundle_fields = {
        "dataset_id": record.dataset_id,
        "dataset_sha256": record.dataset_sha256,
        "model_sha256": record.artifact_sha256,
        "model_type": record.model_type,
        "model_version": record.model_version,
        "feature_contract_version": FEATURE_CONTRACT_VERSION,
        "feature_contract_sha256": record.feature_contract_sha256,
        "runtime_loaded": False,
        "shadow_predictions": 0,
        "decision_impact": "none",
    }
    for key, expected in expected_bundle_fields.items():
        if bundle.get(key) != expected:
            raise ValueError(f"bundle {key} does not match registry")
    try:
        from app.machine_learning.train import load_model_artifact

        model_payload = load_model_artifact(artifact_path)
    except Exception as error:  # noqa: BLE001 - integrity boundary
        raise ValueError(f"registry model artifact metadata is invalid: {error}") from error
    if model_payload.get("model_type") != record.model_type:
        raise ValueError("model artifact type does not match registry")
    if model_payload.get("version") != record.model_version:
        raise ValueError("model artifact version does not match registry")
    if model_payload.get("feature_contract_sha256") != record.feature_contract_sha256:
        raise ValueError("model artifact feature contract does not match registry")
    if bundle.get("validation") != validation:
        raise ValueError("bundle validation does not match validation evidence")
    if validation.get("validation_state") != record.validation_state:
        raise ValueError("validation state does not match registry")
    if validation.get("note") != record.validation_detail:
        raise ValueError("validation detail does not match registry")
    if int(str(validation.get("oos_predictions", -1))) != record.oos_predictions:
        raise ValueError("validation prediction count does not match registry")
    if float(str(validation.get("brier_score", -1))) != record.brier_score:
        raise ValueError("validation Brier score does not match registry")
    if bool(validation.get("beats_baseline")) != record.beats_baseline:
        raise ValueError("validation baseline result does not match registry")

    if (
        not record.artifact_id.startswith("challenger-")
        or Path(record.artifact_id).name != record.artifact_id
        or "/" in record.artifact_id
        or "\\" in record.artifact_id
    ):
        raise ValueError("registry artifact ID is not a safe path component")
    if (
        not record.dataset_id.startswith("dataset-")
        or Path(record.dataset_id).name != record.dataset_id
        or "/" in record.dataset_id
        or "\\" in record.dataset_id
    ):
        raise ValueError("registry dataset ID is not a safe path component")
    dataset_dir = (models_root / "datasets" / record.dataset_id).resolve()
    if not dataset_dir.is_relative_to(root):
        raise ValueError("registry dataset path escapes models root")
    dataset_path = dataset_dir / "dataset.jsonl"
    dataset_manifest = _load_json_object(
        dataset_dir / "dataset_manifest.json",
        description="dataset manifest",
    )
    if dataset_manifest.get("dataset_id") != record.dataset_id:
        raise ValueError("dataset manifest ID does not match registry")
    dataset_core = dict(dataset_manifest)
    dataset_core.pop("dataset_id", None)
    recomputed_dataset_id = "dataset-" + hashlib.sha256(
        json.dumps(
            dataset_core,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:24]
    if recomputed_dataset_id != record.dataset_id:
        raise ValueError("dataset manifest does not match content-addressed dataset ID")
    if dataset_manifest.get("rows_sha256") != record.dataset_sha256:
        raise ValueError("dataset SHA does not match registry")
    if dataset_manifest.get("feature_contract_sha256") != record.feature_contract_sha256:
        raise ValueError("dataset feature contract does not match registry")
    if not dataset_path.is_file() or sha256_file(dataset_path) != record.dataset_sha256:
        raise ValueError("dataset bytes do not match registry SHA")
    included = dataset_manifest.get("included_sessions")
    excluded = dataset_manifest.get("excluded_sessions")
    if not isinstance(included, list) or len(included) != record.included_sessions:
        raise ValueError("included-session count does not match registry")
    if not isinstance(excluded, list) or len(excluded) != record.excluded_sessions:
        raise ValueError("excluded-session count does not match registry")


@dataclass(frozen=True, slots=True)
class ModelValidationState:
    registry_state: RegistryState
    approval_state: ApprovalState
    detail: str
    record: ModelRegistryRecord | None = None


def register_challenger(models_root: Path, record: ModelRegistryRecord) -> Path:
    """Publish one immutable registry record after validating all evidence bytes."""
    _validate_registry_record(record)
    _validate_record_evidence(models_root, record)
    path = models_root / "registry" / f"{record.artifact_id}.json"
    payload = json.dumps(asdict(record), indent=2, sort_keys=True) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != payload:
            raise FileExistsError(f"immutable registry record differs: {path}")
        return path
    atomic_write_text(path, payload)
    return path


def read_registry(models_root: Path) -> tuple[ModelRegistryRecord, ...]:
    """Read registry records and revalidate every referenced evidence artifact."""
    registry = models_root / "registry"
    if not registry.is_dir():
        return ()
    records: list[ModelRegistryRecord] = []
    for path in sorted(registry.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"registry record must be an object: {path}")
        record = ModelRegistryRecord(**payload)
        if path.stem != record.artifact_id:
            raise ValueError("registry filename does not match artifact ID")
        _validate_registry_record(record)
        _validate_record_evidence(models_root, record)
        records.append(record)
    return tuple(records)


def read_model_approval(path: Path) -> ModelApproval:
    """Read typed approval; missing approval is safely equivalent to null."""
    if not path.is_file():
        return ModelApproval()
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("model approval must be a mapping")
    return ModelApproval(**payload)


def validate_explicit_approval(
    records: tuple[ModelRegistryRecord, ...],
    approval: ModelApproval,
) -> ModelValidationState:
    """Validate exact artifact-id plus SHA approval; never infer latest."""
    if approval.approved_artifact_id is None and approval.approved_sha256 is None:
        if not records:
            return ModelValidationState(
                "NOT_REGISTERED", "NOT_APPROVED",
                "no exact artifact approval is configured", None,
            )
        if len(records) > 1:
            return ModelValidationState(
                "CHALLENGER", "NOT_APPROVED",
                f"{len(records)} unapproved challengers registered; no artifact inferred", None,
            )
        return ModelValidationState(
            "CHALLENGER", "NOT_APPROVED",
            "one unapproved challenger registered; runtime loading remains disabled",
            records[0],
        )
    if not approval.approved_artifact_id or not approval.approved_sha256:
        return ModelValidationState("INVALID", "INVALID", "approval requires both artifact id and SHA")
    matches = [record for record in records if record.artifact_id == approval.approved_artifact_id]
    if len(matches) != 1:
        return ModelValidationState("INVALID", "INVALID", "approved artifact id is not registered")
    record = matches[0]
    if record.artifact_sha256 != approval.approved_sha256:
        return ModelValidationState("INVALID", "INVALID", "approved artifact SHA does not match registry")
    if record.validation_state != "PASSED":
        return ModelValidationState("INVALID", "INVALID", "failed validation cannot be approved", record)
    if approval.runtime_loading_enabled or approval.shadow_scoring_enabled:
        return ModelValidationState(
            "INVALID", "INVALID",
            "runtime loading and shadow scoring are not implemented in this cycle",
            record,
        )
    return ModelValidationState(
        "CHALLENGER", "APPROVED_RUNTIME_DISABLED",
        "exact artifact approved; runtime loading remains disabled",
        record,
    )

def is_artifact_stale(
    record: ModelRegistryRecord,
    as_of_date: date | datetime,
    max_age_days: int = 30,
    *,
    models_root: Path,
) -> bool:
    """Return whether the artifact's latest immutable evaluation day is old.

    The full content-addressed evidence graph is revalidated first. The latest
    walk-forward ``test_day`` is then used instead of mutable filesystem
    timestamps. This helper reports policy evidence only; callers decide how a
    stale artifact affects runtime behavior.
    """
    if max_age_days < 0:
        raise ValueError("max_age_days must be non-negative")

    _validate_record_evidence(models_root, record)
    root = models_root.resolve()
    artifact_path = (models_root / record.artifact_path).resolve()
    if not artifact_path.is_relative_to(root):
        raise ValueError("registry artifact path escapes models root")
    validation_path = artifact_path.parent / "validation.json"
    validation = _load_json_object(
        validation_path,
        description="challenger validation evidence",
    )
    fold_boundaries = validation.get("fold_boundaries")
    if not isinstance(fold_boundaries, list) or not fold_boundaries:
        raise ValueError("validation evidence has no walk-forward fold boundaries")

    test_days: list[date] = []
    for index, fold in enumerate(fold_boundaries):
        if not isinstance(fold, dict):
            raise ValueError(f"validation fold {index} must be an object")
        raw_test_day = fold.get("test_day")
        if not isinstance(raw_test_day, str):
            raise ValueError(f"validation fold {index} is missing test_day")
        try:
            test_days.append(date.fromisoformat(raw_test_day))
        except ValueError as error:
            raise ValueError(
                f"validation fold {index} test_day must be ISO-8601"
            ) from error

    comparison_date = as_of_date.date() if isinstance(as_of_date, datetime) else as_of_date
    age_days = (comparison_date - max(test_days)).days
    return age_days > max_age_days



def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
