"""Load versioned setup-success models and produce gated predictions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from app.machine_learning.train import (
    ModelType,
    build_feature_matrix,
    load_model_artifact,
    model_path_for_version,
)
from app.strategy.setups import SetupEvaluationResult


@dataclass(frozen=True, slots=True)
class PredictionResult:
    """Success-probability prediction from a versioned model."""

    model_type: ModelType
    version: str
    success_probability: Decimal


def predict_success_probability(
    *,
    model_path: str | Path,
    features: Mapping[str, object],
    setup_result: SetupEvaluationResult,
) -> Decimal:
    """Return success probability only after the deterministic setup result is accepted."""
    prediction = predict_with_model(
        model_path=model_path,
        features=features,
        setup_result=setup_result,
    )
    return prediction.success_probability


def predict_model_version(
    *,
    model_dir: str | Path,
    model_type: ModelType,
    version: str,
    features: Mapping[str, object],
    setup_result: SetupEvaluationResult,
) -> PredictionResult:
    """Load a saved model version from a model directory and predict success probability."""
    return predict_with_model(
        model_path=model_path_for_version(model_dir, model_type, version),
        features=features,
        setup_result=setup_result,
    )


def predict_with_model(
    *,
    model_path: str | Path,
    features: Mapping[str, object],
    setup_result: SetupEvaluationResult,
) -> PredictionResult:
    """Load one model artifact and return the target-before-stop success probability."""
    if not isinstance(setup_result, SetupEvaluationResult):
        raise TypeError("setup_result must be a Task 6 SetupEvaluationResult")
    if not setup_result.accepted:
        raise ValueError("ML prediction requires an accepted deterministic setup result")

    artifact = load_model_artifact(model_path)
    feature_columns = tuple(str(column) for column in artifact["feature_columns"])
    model = artifact["model"]
    matrix = build_feature_matrix((features,), feature_columns)
    if not hasattr(model, "predict_proba"):
        raise ValueError("model artifact does not support predict_proba")
    probabilities = model.predict_proba(matrix)
    probability = Decimal(str(float(probabilities[0][1])))
    return PredictionResult(
        model_type=str(artifact["model_type"]),  # type: ignore[arg-type]
        version=str(artifact["version"]),
        success_probability=probability,
    )
