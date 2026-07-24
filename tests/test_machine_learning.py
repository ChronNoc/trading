"""Tests for machine-learning training, prediction, and validation."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.machine_learning.predict import predict_model_version, predict_success_probability
from app.machine_learning.train import (
    FEATURE_COLUMNS,
    ModelTrainingConfig,
    build_feature_matrix,
    load_labeled_dataset,
    train_models,
)
from app.machine_learning.validation import (
    calibration_check,
    drift_check,
    split_labeled_rows_by_trading_day,
    walk_forward_evaluate,
)
from app.simulator.metrics import TradingDayBoundary
from app.strategy.setups import SetupConditionResult, SetupEvaluationResult


def test_train_models_saves_versioned_artifacts_and_sidecars(tmp_path: Path) -> None:
    """Training writes LogisticRegression and XGBoost model files with JSON metadata."""
    dataset_path = _write_dataset(tmp_path / "labeled_setups.csv", _synthetic_rows(day_count=4))

    result = train_models(
        dataset_path,
        tmp_path / "models",
        version="1.2.3",
        config=ModelTrainingConfig(xgboost_estimators=5),
        feature_contract_sha256="test-contract-sha",
    )

    assert result.logistic_regression.model_path.name == "logistic_regression_model_v1.2.3.joblib"
    assert result.xgboost.model_path.name == "xgboost_model_v1.2.3.joblib"
    assert result.logistic_regression.model_path.exists()
    assert result.xgboost.model_path.exists()
    logistic_metadata = json.loads(result.logistic_regression.sidecar_path.read_text(encoding="utf-8"))
    xgboost_metadata = json.loads(result.xgboost.sidecar_path.read_text(encoding="utf-8"))
    assert logistic_metadata["row_count"] == 16
    assert xgboost_metadata["row_count"] == 16
    assert logistic_metadata["feature_columns"] == list(FEATURE_COLUMNS)
    assert logistic_metadata["feature_contract_version"] == "shared-causal-market-features-v2"
    assert logistic_metadata["feature_contract_sha256"] == "test-contract-sha"
    assert logistic_metadata["training_period"]["start"].startswith("2026-07-01")
    assert logistic_metadata["training_period"]["end"].startswith("2026-07-04")


def test_prediction_requires_accepted_task_six_result(tmp_path: Path) -> None:
    """Prediction refuses to run unless the deterministic setup evaluation was accepted."""
    dataset_path = _write_dataset(tmp_path / "labeled_setups.csv", _synthetic_rows(day_count=4))
    train_models(
        dataset_path,
        tmp_path / "models",
        version="1.0.0",
        config=ModelTrainingConfig(xgboost_estimators=5),
        feature_contract_sha256="test-contract-sha",
    )
    features = dict(_synthetic_rows(day_count=1)[0])
    features.pop("label")
    features.pop("timestamp_ns")

    probability = predict_success_probability(
        model_path=tmp_path / "models" / "logistic_regression_model_v1.0.0.joblib",
        features=features,
        setup_result=_accepted_setup_result(),
    )

    assert Decimal("0") <= probability <= Decimal("1")
    with pytest.raises(ValueError, match="accepted deterministic setup"):
        predict_model_version(
            model_dir=tmp_path / "models",
            model_type="logistic_regression",
            version="1.0.0",
            features=features,
            setup_result=_rejected_setup_result(),
        )


def test_feature_matrix_encodes_direction_and_time_of_day(tmp_path: Path) -> None:
    """Feature building preserves the contract and encodes categorical/time fields."""
    dataset = load_labeled_dataset(_write_dataset(tmp_path / "labeled_setups.csv", _synthetic_rows(day_count=1)))

    matrix = build_feature_matrix(dataset.rows)

    assert matrix.shape == (4, len(FEATURE_COLUMNS))
    assert matrix[0][FEATURE_COLUMNS.index("direction")] == 1.0
    assert matrix[1][FEATURE_COLUMNS.index("direction")] == -1.0
    assert Decimal(str(matrix[0][FEATURE_COLUMNS.index("time_of_day")])) == Decimal("0.3958333333333333")


def test_validation_split_is_day_based_and_rejects_midday_boundary() -> None:
    """Labeled setup splits use whole trading days and refuse mid-day split timestamps."""
    rows = tuple(_synthetic_rows(day_count=4))
    train_end = _timestamp_ns(2026, 7, 3, 0, 0)
    validation_end = _timestamp_ns(2026, 7, 4, 0, 0)

    splits = split_labeled_rows_by_trading_day(
        rows,
        train_end_timestamp_ns=train_end,
        validation_end_timestamp_ns=validation_end,
        day_boundary=TradingDayBoundary(),
    )

    assert len(splits.train) == 8
    assert len(splits.validation) == 4
    assert len(splits.test) == 4
    with pytest.raises(ValueError, match="trading-day boundary"):
        split_labeled_rows_by_trading_day(
            rows,
            train_end_timestamp_ns=_timestamp_ns(2026, 7, 3, 12, 0),
            validation_end_timestamp_ns=validation_end,
        )


def test_walk_forward_calibration_and_drift_checks() -> None:
    """Validation utilities return deterministic walk-forward, calibration, and drift metrics."""
    rows = tuple(_synthetic_rows(day_count=5))

    folds = walk_forward_evaluate(rows, min_train_days=2)
    calibration = calibration_check(
        probabilities=(Decimal("0.10"), Decimal("0.20"), Decimal("0.80"), Decimal("0.90")),
        labels=(0, 0, 1, 1),
        bin_count=2,
    )
    drift = drift_check(
        training_probabilities=(Decimal("0.20"), Decimal("0.30"), Decimal("0.40"), Decimal("0.50")),
        live_probabilities=(Decimal("0.75"), Decimal("0.80"), Decimal("0.85"), Decimal("0.90")),
        bin_count=4,
        psi_threshold=Decimal("0.05"),
    )

    assert len(folds) == 3
    assert all(Decimal("0") <= fold.accuracy <= Decimal("1") for fold in folds)
    assert calibration.brier_score < Decimal("0.05")
    assert calibration.expected_calibration_error >= Decimal("0")
    assert drift.live_mean > drift.training_mean
    assert drift.drift_detected is True


def test_training_rejects_non_semver_versions(tmp_path: Path) -> None:
    """Model versions must use explicit semantic version strings."""
    dataset_path = _write_dataset(tmp_path / "labeled_setups.csv", _synthetic_rows(day_count=2))

    with pytest.raises(ValueError, match="semantic version"):
        train_models(
            dataset_path,
            tmp_path / "models",
            version="latest",
            feature_contract_sha256="test-contract-sha",
        )


def _accepted_setup_result() -> SetupEvaluationResult:
    return SetupEvaluationResult(
        setup_name="LONG SETUP",
        conditions=(SetupConditionResult("accepted", True, "accepted"),),
    )


def _rejected_setup_result() -> SetupEvaluationResult:
    return SetupEvaluationResult(
        setup_name="LONG SETUP",
        conditions=(SetupConditionResult("accepted", False, "rejected"),),
    )


def _write_dataset(path: Path, rows: list[dict[str, object]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=["timestamp_ns", *FEATURE_COLUMNS, "label"])
        writer.writeheader()
        writer.writerows(rows)
    return path


def _synthetic_rows(*, day_count: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for day_offset in range(day_count):
        for setup_index in range(4):
            label = 1 if setup_index in {0, 2} else 0
            direction = "long" if setup_index % 2 == 0 else "short"
            strength = Decimal("1") if label else Decimal("-1")
            rows.append(
                {
                    "timestamp_ns": _timestamp_ns(2026, 7, 1 + day_offset, 9 + setup_index, 30),
                    "book_imbalance": Decimal("0.40") * strength,
                    "recent_aggressive_buy_volume": Decimal("120") if label else Decimal("35"),
                    "recent_aggressive_sell_volume": Decimal("35") if label else Decimal("120"),
                    "liquidity_added": Decimal("70") if label else Decimal("15"),
                    "liquidity_cancelled": Decimal("10") if label else Decimal("55"),
                    "reload_count": Decimal("2") if label else Decimal("0"),
                    "distance_to_defended_level": Decimal("0.25") if label else Decimal("1.50"),
                    "price_velocity": Decimal("0.75") * strength,
                    "trade_velocity": Decimal("20") if label else Decimal("7"),
                    "spread": Decimal("0.25"),
                    "short_term_volatility": Decimal("0.08") if label else Decimal("0.30"),
                    "time_of_day": f"{9 + setup_index:02d}:30",
                    "distance_from_overnight_high_low": Decimal("1.00") if label else Decimal("3.00"),
                    "distance_from_prior_day_levels": Decimal("1.25") if label else Decimal("3.50"),
                    "direction": direction,
                    "stop_distance": Decimal("8") if label else Decimal("14"),
                    "target_distance": Decimal("16") if label else Decimal("8"),
                    "label": label,
                },
            )
    return rows


def _timestamp_ns(year: int, month: int, day: int, hour: int, minute: int) -> int:
    timestamp = datetime(year, month, day, hour, minute, tzinfo=UTC)
    return int(timestamp.timestamp()) * 1_000_000_000
