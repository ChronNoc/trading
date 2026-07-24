"""Train versioned setup-success classifiers from labeled setup rows."""

from __future__ import annotations

import csv
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, TypeAlias, cast

import joblib
import numpy as np
from sklearn.linear_model import LogisticRegression

from app.machine_learning.feature_contract import (
    FEATURE_COLUMNS,
    FEATURE_CONTRACT_VERSION,
)

ModelType: TypeAlias = Literal["logistic_regression", "xgboost"]
LabeledRow: TypeAlias = Mapping[str, object]
LABEL_COLUMN = "label"
TIMESTAMP_COLUMN = "timestamp_ns"
MODEL_FILENAME_TEMPLATE = "{model_type}_model_v{version}.joblib"
SIDECAR_FILENAME_TEMPLATE = "{model_type}_model_v{version}.json"
SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


@dataclass(frozen=True, slots=True)
class LabeledDataset:
    """Validated labeled setup dataset for model training."""

    rows: tuple[LabeledRow, ...]
    feature_columns: tuple[str, ...] = FEATURE_COLUMNS


@dataclass(frozen=True, slots=True)
class ModelTrainingConfig:
    """Training configuration for deterministic sklearn and XGBoost models."""

    random_state: int = 7
    logistic_max_iter: int = 1_000
    xgboost_estimators: int = 25
    xgboost_max_depth: int = 2
    xgboost_learning_rate: Decimal = Decimal("0.20")


@dataclass(frozen=True, slots=True)
class ModelArtifactMetadata:
    """Metadata written beside each versioned model artifact."""

    model_type: ModelType
    version: str
    feature_columns: tuple[str, ...]
    feature_contract_version: str
    feature_contract_sha256: str
    training_period: dict[str, str]
    row_count: int
    positive_label_count: int

    def to_json_dict(self) -> dict[str, object]:
        """Return metadata as a JSON-compatible dictionary."""
        return {
            "model_type": self.model_type,
            "version": self.version,
            "feature_columns": list(self.feature_columns),
            "feature_contract_version": self.feature_contract_version,
            "feature_contract_sha256": self.feature_contract_sha256,
            "training_period": dict(self.training_period),
            "row_count": self.row_count,
            "positive_label_count": self.positive_label_count,
        }


@dataclass(frozen=True, slots=True)
class TrainedModelArtifact:
    """Paths and metadata for one trained model artifact."""

    model_type: ModelType
    version: str
    model_path: Path
    sidecar_path: Path
    metadata: ModelArtifactMetadata


@dataclass(frozen=True, slots=True)
class TrainingRunResult:
    """Result of training all supported model types."""

    logistic_regression: TrainedModelArtifact
    xgboost: TrainedModelArtifact


def train_models(
    dataset_path: str | Path,
    output_dir: str | Path,
    *,
    version: str,
    config: ModelTrainingConfig | None = None,
    feature_contract_sha256: str,
) -> TrainingRunResult:
    """Train LogisticRegression and XGBoost classifiers and save versioned artifacts."""
    _validate_semver(version)
    training_config = config or ModelTrainingConfig()
    dataset = load_labeled_dataset(dataset_path)
    features = build_feature_matrix(dataset.rows, dataset.feature_columns)
    labels = build_label_array(dataset.rows)
    _validate_binary_labels(labels)

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    logistic_model = LogisticRegression(
        max_iter=training_config.logistic_max_iter,
        random_state=training_config.random_state,
        solver="liblinear",
    )
    logistic_model.fit(features, labels)

    xgboost_model = _build_xgboost_classifier(training_config)
    xgboost_model.fit(features, labels)

    logistic_artifact = save_model_artifact(
        model=logistic_model,
        model_type="logistic_regression",
        version=version,
        output_dir=output_path,
        dataset=dataset,
        labels=labels,
        feature_contract_sha256=feature_contract_sha256,
    )
    xgboost_artifact = save_model_artifact(
        model=xgboost_model,
        model_type="xgboost",
        version=version,
        output_dir=output_path,
        dataset=dataset,
        labels=labels,
        feature_contract_sha256=feature_contract_sha256,
    )
    return TrainingRunResult(
        logistic_regression=logistic_artifact,
        xgboost=xgboost_artifact,
    )


def load_labeled_dataset(path: str | Path) -> LabeledDataset:
    """Load and validate a labeled setup dataset from CSV, JSON, JSONL, or Parquet."""
    dataset_path = Path(path)
    rows = tuple(_load_rows(dataset_path))
    if not rows:
        raise ValueError("labeled dataset must contain at least one row")
    _validate_required_columns(rows)
    return LabeledDataset(rows=rows)


def build_feature_matrix(
    rows: Sequence[Mapping[str, object]],
    feature_columns: Sequence[str] = FEATURE_COLUMNS,
) -> np.ndarray:
    """Build a numeric feature matrix in the saved model feature order."""
    if not rows:
        raise ValueError("rows must contain at least one setup")

    matrix: list[list[float]] = []
    for row in rows:
        matrix.append([_feature_value(row, column) for column in feature_columns])
    return np.asarray(matrix, dtype=np.float64)


def build_label_array(rows: Sequence[Mapping[str, object]]) -> np.ndarray:
    """Build a binary label array where 1 means target-before-stop success."""
    labels: list[int] = []
    for row in rows:
        if LABEL_COLUMN not in row:
            raise ValueError(f"dataset row is missing {LABEL_COLUMN}")
        label = int(row[LABEL_COLUMN])
        if label not in {0, 1}:
            raise ValueError("label must be 0 or 1")
        labels.append(label)
    return np.asarray(labels, dtype=np.int64)


def save_model_artifact(
    *,
    model: object,
    model_type: ModelType,
    version: str,
    output_dir: str | Path,
    dataset: LabeledDataset,
    labels: np.ndarray,
    feature_contract_sha256: str,
) -> TrainedModelArtifact:
    """Save one model artifact and sidecar JSON metadata."""
    _validate_semver(version)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    model_path = output_path / MODEL_FILENAME_TEMPLATE.format(model_type=model_type, version=version)
    sidecar_path = output_path / SIDECAR_FILENAME_TEMPLATE.format(model_type=model_type, version=version)
    metadata = ModelArtifactMetadata(
        model_type=model_type,
        version=version,
        feature_columns=tuple(dataset.feature_columns),
        feature_contract_version=FEATURE_CONTRACT_VERSION,
        feature_contract_sha256=feature_contract_sha256,
        training_period=_training_period(dataset.rows),
        row_count=len(dataset.rows),
        positive_label_count=int(labels.sum()),
    )

    joblib.dump(
        {
            "model": model,
            "model_type": model_type,
            "version": version,
            "feature_columns": tuple(dataset.feature_columns),
            "feature_contract_version": FEATURE_CONTRACT_VERSION,
            "feature_contract_sha256": feature_contract_sha256,
        },
        model_path,
    )
    sidecar_path.write_text(
        json.dumps(metadata.to_json_dict(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return TrainedModelArtifact(
        model_type=model_type,
        version=version,
        model_path=model_path,
        sidecar_path=sidecar_path,
        metadata=metadata,
    )


def model_path_for_version(model_dir: str | Path, model_type: ModelType, version: str) -> Path:
    """Return the expected joblib path for a model type and semantic version."""
    _validate_semver(version)
    return Path(model_dir) / MODEL_FILENAME_TEMPLATE.format(model_type=model_type, version=version)


def sidecar_path_for_version(model_dir: str | Path, model_type: ModelType, version: str) -> Path:
    """Return the expected sidecar JSON path for a model type and semantic version."""
    _validate_semver(version)
    return Path(model_dir) / SIDECAR_FILENAME_TEMPLATE.format(model_type=model_type, version=version)


def load_model_artifact(path: str | Path) -> dict[str, object]:
    """Load a saved model artifact and validate its feature metadata."""
    artifact_path = Path(path)
    payload = joblib.load(artifact_path)
    if not isinstance(payload, dict):
        raise ValueError("model artifact must contain a dictionary payload")
    if "model" not in payload:
        raise ValueError("model artifact is missing model")
    feature_columns = payload.get("feature_columns")
    if tuple(feature_columns or ()) != FEATURE_COLUMNS:
        raise ValueError("model artifact feature columns do not match the current feature contract")
    if payload.get("feature_contract_version") != FEATURE_CONTRACT_VERSION:
        raise ValueError("model artifact feature contract version does not match")
    feature_hash = payload.get("feature_contract_sha256")
    if not isinstance(feature_hash, str) or not feature_hash:
        raise ValueError("model artifact is missing feature contract SHA")
    return cast(dict[str, object], payload)


def _build_xgboost_classifier(config: ModelTrainingConfig) -> object:
    from xgboost import XGBClassifier

    return XGBClassifier(
        n_estimators=config.xgboost_estimators,
        max_depth=config.xgboost_max_depth,
        learning_rate=float(config.xgboost_learning_rate),
        objective="binary:logistic",
        eval_metric="logloss",
        random_state=config.random_state,
        n_jobs=1,
        verbosity=0,
    )


def _load_rows(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        raise FileNotFoundError(f"labeled dataset not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8", newline="") as csv_file:
            return [dict(row) for row in csv.DictReader(csv_file)]
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("JSON dataset must contain a list of rows")
        return [cast(dict[str, object], row) for row in payload]
    if suffix in {".jsonl", ".ndjson"}:
        return [
            cast(dict[str, object], json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    if suffix == ".parquet":
        import pyarrow.parquet as pq

        return cast(list[dict[str, object]], pq.read_table(path).to_pylist())
    raise ValueError("dataset path must end with .csv, .json, .jsonl, .ndjson, or .parquet")


def _validate_required_columns(rows: Sequence[Mapping[str, object]]) -> None:
    required = set(FEATURE_COLUMNS) | {LABEL_COLUMN, TIMESTAMP_COLUMN}
    missing = sorted(column for column in required if column not in rows[0])
    if missing:
        raise ValueError(f"dataset is missing required columns: {', '.join(missing)}")


def _feature_value(row: Mapping[str, object], column: str) -> float:
    if column == "direction":
        return _direction_value(row.get(column))
    if column == "time_of_day":
        return _time_of_day_value(row.get(column))
    return float(_decimal_value(row.get(column), column))


def _direction_value(value: object) -> float:
    normalized = str(value).strip().lower()
    if normalized == "long":
        return 1.0
    if normalized == "short":
        return -1.0
    raise ValueError("direction must be long or short")


def _time_of_day_value(value: object) -> float:
    if isinstance(value, str) and ":" in value:
        parts = value.split(":")
        if len(parts) not in {2, 3}:
            raise ValueError("time_of_day string must be HH:MM or HH:MM:SS")
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
        if hour < 0 or hour > 23 or minute < 0 or minute > 59 or second < 0 or second > 59:
            raise ValueError("time_of_day is outside valid clock range")
        return float((Decimal(hour * 3600 + minute * 60 + second) / Decimal("86400")))
    return float(_decimal_value(value, "time_of_day"))


def _decimal_value(value: object, column: str) -> Decimal:
    if value is None:
        raise ValueError(f"{column} is required")
    if isinstance(value, bool):
        raise ValueError(f"{column} must be numeric, not bool")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{column} must be decimal-compatible") from error
    if not decimal_value.is_finite():
        raise ValueError(f"{column} must be finite")
    return decimal_value


def _training_period(rows: Sequence[Mapping[str, object]]) -> dict[str, str]:
    timestamps = tuple(_timestamp_ns(row) for row in rows)
    return {
        "start": _datetime_from_ns(min(timestamps)).isoformat(),
        "end": _datetime_from_ns(max(timestamps)).isoformat(),
    }


def _timestamp_ns(row: Mapping[str, object]) -> int:
    value = row.get(TIMESTAMP_COLUMN)
    if isinstance(value, bool) or value is None:
        raise ValueError("timestamp_ns is required")
    timestamp_ns = int(value)
    if timestamp_ns < 0:
        raise ValueError("timestamp_ns must be non-negative")
    return timestamp_ns


def _datetime_from_ns(timestamp_ns: int) -> datetime:
    seconds, nanoseconds = divmod(timestamp_ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(microseconds=nanoseconds // 1_000)


def _validate_binary_labels(labels: np.ndarray) -> None:
    if len(labels) < 2:
        raise ValueError("at least two labeled rows are required")
    if set(int(label) for label in labels) != {0, 1}:
        raise ValueError("training labels must contain both 0 and 1")


def _validate_semver(version: str) -> None:
    if not SEMVER_PATTERN.fullmatch(version):
        raise ValueError("version must use semantic version format MAJOR.MINOR.PATCH")
