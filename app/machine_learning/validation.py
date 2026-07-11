"""Validation utilities for setup-success model research."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
from sklearn.linear_model import LogisticRegression

from app.machine_learning.train import (
    LabeledRow,
    build_feature_matrix,
    build_label_array,
    load_labeled_dataset,
)
from app.simulator.metrics import NANOSECONDS_PER_SECOND, TradingDayBoundary


@dataclass(frozen=True, slots=True)
class LabeledDatasetSplits:
    """Train, validation, and test splits grouped by whole trading days."""

    train: tuple[LabeledRow, ...]
    validation: tuple[LabeledRow, ...]
    test: tuple[LabeledRow, ...]


@dataclass(frozen=True, slots=True)
class WalkForwardFoldResult:
    """Metrics for one walk-forward validation fold."""

    train_start_day: date
    train_end_day: date
    validation_day: date
    train_rows: int
    validation_rows: int
    accuracy: Decimal
    brier_score: Decimal


@dataclass(frozen=True, slots=True)
class CalibrationBin:
    """Calibration statistics for one probability bucket."""

    lower_bound: Decimal
    upper_bound: Decimal
    count: int
    average_prediction: Decimal
    observed_frequency: Decimal


@dataclass(frozen=True, slots=True)
class CalibrationCheckResult:
    """Calibration summary for predicted probabilities versus realized labels."""

    brier_score: Decimal
    expected_calibration_error: Decimal
    bins: tuple[CalibrationBin, ...]


@dataclass(frozen=True, slots=True)
class DriftCheckResult:
    """Prediction-distribution drift summary."""

    training_mean: Decimal
    live_mean: Decimal
    mean_shift: Decimal
    population_stability_index: Decimal
    drift_detected: bool


def split_labeled_rows_by_trading_day(
    rows: Sequence[LabeledRow],
    *,
    train_end_timestamp_ns: int,
    validation_end_timestamp_ns: int,
    day_boundary: TradingDayBoundary = TradingDayBoundary(),
) -> LabeledDatasetSplits:
    """Split labeled setup rows by whole trading days and refuse mid-day boundaries."""
    _validate_day_boundary(day_boundary)
    if train_end_timestamp_ns >= validation_end_timestamp_ns:
        raise ValueError("train_end_timestamp_ns must be before validation_end_timestamp_ns")
    if not _is_boundary_timestamp(train_end_timestamp_ns, day_boundary):
        raise ValueError("train split timestamp must be exactly on a trading-day boundary")
    if not _is_boundary_timestamp(validation_end_timestamp_ns, day_boundary):
        raise ValueError("validation split timestamp must be exactly on a trading-day boundary")

    train_end_day = _trading_day_for_timestamp(train_end_timestamp_ns, day_boundary)
    validation_end_day = _trading_day_for_timestamp(validation_end_timestamp_ns, day_boundary)
    train: list[LabeledRow] = []
    validation: list[LabeledRow] = []
    test: list[LabeledRow] = []

    for row in sorted(rows, key=_timestamp_ns):
        row_day = _trading_day_for_timestamp(_timestamp_ns(row), day_boundary)
        if row_day < train_end_day:
            train.append(row)
        elif row_day < validation_end_day:
            validation.append(row)
        else:
            test.append(row)

    return LabeledDatasetSplits(train=tuple(train), validation=tuple(validation), test=tuple(test))


def split_dataset_by_trading_day(
    dataset_path: str,
    *,
    train_end_timestamp_ns: int,
    validation_end_timestamp_ns: int,
    day_boundary: TradingDayBoundary = TradingDayBoundary(),
) -> LabeledDatasetSplits:
    """Load and split a labeled dataset by whole trading days."""
    dataset = load_labeled_dataset(dataset_path)
    return split_labeled_rows_by_trading_day(
        dataset.rows,
        train_end_timestamp_ns=train_end_timestamp_ns,
        validation_end_timestamp_ns=validation_end_timestamp_ns,
        day_boundary=day_boundary,
    )


def walk_forward_evaluate(
    rows: Sequence[LabeledRow],
    *,
    min_train_days: int = 2,
    day_boundary: TradingDayBoundary = TradingDayBoundary(),
) -> tuple[WalkForwardFoldResult, ...]:
    """Run deterministic walk-forward evaluation using prior days to predict the next day."""
    if min_train_days <= 0:
        raise ValueError("min_train_days must be greater than zero")

    grouped = _rows_by_day(rows, day_boundary)
    ordered_days = tuple(sorted(grouped))
    if len(ordered_days) <= min_train_days:
        raise ValueError("not enough trading days for walk-forward evaluation")

    fold_results: list[WalkForwardFoldResult] = []
    for validation_index in range(min_train_days, len(ordered_days)):
        train_days = ordered_days[:validation_index]
        validation_day = ordered_days[validation_index]
        train_rows = tuple(row for day in train_days for row in grouped[day])
        validation_rows = tuple(grouped[validation_day])
        labels = build_label_array(train_rows)
        if set(int(label) for label in labels) != {0, 1}:
            raise ValueError("walk-forward training rows must contain both labels")

        model = LogisticRegression(max_iter=1_000, solver="liblinear", random_state=7)
        model.fit(build_feature_matrix(train_rows), labels)
        probabilities = model.predict_proba(build_feature_matrix(validation_rows))[:, 1]
        actual = build_label_array(validation_rows)
        predictions = (probabilities >= 0.5).astype(int)
        accuracy = Decimal(str(float((predictions == actual).mean())))
        brier_score = _brier_score(probabilities, actual)
        fold_results.append(
            WalkForwardFoldResult(
                train_start_day=train_days[0],
                train_end_day=train_days[-1],
                validation_day=validation_day,
                train_rows=len(train_rows),
                validation_rows=len(validation_rows),
                accuracy=accuracy,
                brier_score=brier_score,
            ),
        )

    return tuple(fold_results)


def calibration_check(
    probabilities: Sequence[Decimal | float | str],
    labels: Sequence[int],
    *,
    bin_count: int = 10,
) -> CalibrationCheckResult:
    """Check calibration with Brier score and expected calibration error."""
    if bin_count <= 0:
        raise ValueError("bin_count must be greater than zero")
    probability_array = _probability_array(probabilities)
    label_array = np.asarray(labels, dtype=np.int64)
    _validate_probability_labels(probability_array, label_array)

    bins: list[CalibrationBin] = []
    expected_calibration_error = Decimal("0")
    total_count = Decimal(len(probability_array))
    for index in range(bin_count):
        lower = Decimal(index) / Decimal(bin_count)
        upper = Decimal(index + 1) / Decimal(bin_count)
        if index == bin_count - 1:
            mask = (probability_array >= float(lower)) & (probability_array <= float(upper))
        else:
            mask = (probability_array >= float(lower)) & (probability_array < float(upper))
        count = int(mask.sum())
        if count == 0:
            average_prediction = Decimal("0")
            observed_frequency = Decimal("0")
        else:
            average_prediction = Decimal(str(float(probability_array[mask].mean())))
            observed_frequency = Decimal(str(float(label_array[mask].mean())))
            expected_calibration_error += (Decimal(count) / total_count) * abs(
                average_prediction - observed_frequency,
            )
        bins.append(
            CalibrationBin(
                lower_bound=lower,
                upper_bound=upper,
                count=count,
                average_prediction=average_prediction,
                observed_frequency=observed_frequency,
            ),
        )

    return CalibrationCheckResult(
        brier_score=_brier_score(probability_array, label_array),
        expected_calibration_error=expected_calibration_error,
        bins=tuple(bins),
    )


def drift_check(
    training_probabilities: Sequence[Decimal | float | str],
    live_probabilities: Sequence[Decimal | float | str],
    *,
    bin_count: int = 10,
    psi_threshold: Decimal = Decimal("0.20"),
) -> DriftCheckResult:
    """Compare live prediction distribution against the training prediction distribution."""
    if bin_count <= 0:
        raise ValueError("bin_count must be greater than zero")
    training = _probability_array(training_probabilities)
    live = _probability_array(live_probabilities)
    if len(training) == 0 or len(live) == 0:
        raise ValueError("training and live probabilities must not be empty")

    bins = np.linspace(0.0, 1.0, bin_count + 1)
    training_counts, _ = np.histogram(training, bins=bins)
    live_counts, _ = np.histogram(live, bins=bins)
    epsilon = Decimal("0.0001")
    psi = Decimal("0")
    training_total = Decimal(int(training_counts.sum()))
    live_total = Decimal(int(live_counts.sum()))
    for train_count, live_count in zip(training_counts, live_counts):
        train_pct = max(Decimal(int(train_count)) / training_total, epsilon)
        live_pct = max(Decimal(int(live_count)) / live_total, epsilon)
        psi += (live_pct - train_pct) * Decimal(str(np.log(float(live_pct / train_pct))))

    training_mean = Decimal(str(float(training.mean())))
    live_mean = Decimal(str(float(live.mean())))
    mean_shift = live_mean - training_mean
    return DriftCheckResult(
        training_mean=training_mean,
        live_mean=live_mean,
        mean_shift=mean_shift,
        population_stability_index=psi,
        drift_detected=psi >= psi_threshold,
    )


def _rows_by_day(
    rows: Sequence[LabeledRow],
    day_boundary: TradingDayBoundary,
) -> dict[date, tuple[LabeledRow, ...]]:
    grouped: dict[date, list[LabeledRow]] = {}
    for row in rows:
        day = _trading_day_for_timestamp(_timestamp_ns(row), day_boundary)
        grouped.setdefault(day, []).append(row)
    return {day: tuple(day_rows) for day, day_rows in grouped.items()}


def _brier_score(probabilities: np.ndarray, labels: np.ndarray) -> Decimal:
    return Decimal(str(float(np.mean((probabilities - labels) ** 2))))


def _probability_array(probabilities: Sequence[Decimal | float | str]) -> np.ndarray:
    values = np.asarray([float(Decimal(str(value))) for value in probabilities], dtype=np.float64)
    if np.any(values < 0.0) or np.any(values > 1.0):
        raise ValueError("probabilities must be between 0 and 1")
    return values


def _validate_probability_labels(probabilities: np.ndarray, labels: np.ndarray) -> None:
    if len(probabilities) != len(labels):
        raise ValueError("probabilities and labels must have the same length")
    if len(probabilities) == 0:
        raise ValueError("probabilities and labels must not be empty")
    if not set(int(label) for label in labels).issubset({0, 1}):
        raise ValueError("labels must be 0 or 1")


def _timestamp_ns(row: Mapping[str, object]) -> int:
    timestamp_ns = int(row["timestamp_ns"])
    if timestamp_ns < 0:
        raise ValueError("timestamp_ns must be non-negative")
    return timestamp_ns


def _datetime_from_ns(timestamp_ns: int) -> datetime:
    seconds, nanoseconds = divmod(timestamp_ns, NANOSECONDS_PER_SECOND)
    return datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(microseconds=nanoseconds // 1_000)


def _trading_day_for_timestamp(timestamp_ns: int, day_boundary: TradingDayBoundary) -> date:
    timestamp = _datetime_from_ns(timestamp_ns)
    boundary_today = timestamp.replace(
        hour=day_boundary.hour,
        minute=day_boundary.minute,
        second=day_boundary.second,
        microsecond=0,
    )
    if timestamp < boundary_today:
        return (timestamp - timedelta(days=1)).date()
    return timestamp.date()


def _is_boundary_timestamp(timestamp_ns: int, day_boundary: TradingDayBoundary) -> bool:
    timestamp = _datetime_from_ns(timestamp_ns)
    return (
        timestamp.hour == day_boundary.hour
        and timestamp.minute == day_boundary.minute
        and timestamp.second == day_boundary.second
        and timestamp.microsecond == 0
        and timestamp_ns % NANOSECONDS_PER_SECOND == 0
    )


def _validate_day_boundary(day_boundary: TradingDayBoundary) -> None:
    if day_boundary.hour < 0 or day_boundary.hour > 23:
        raise ValueError("day boundary hour must be between 0 and 23")
    if day_boundary.minute < 0 or day_boundary.minute > 59:
        raise ValueError("day boundary minute must be between 0 and 59")
    if day_boundary.second < 0 or day_boundary.second > 59:
        raise ValueError("day boundary second must be between 0 and 59")
