"""Pooled walk-forward learning: finds a real signal, honest when there is none."""

from __future__ import annotations

import random

from app.machine_learning.pooled_training import walk_forward_evaluate
from app.machine_learning.train import FEATURE_COLUMNS

_DAY_NS = 24 * 3600 * 1_000_000_000
_BASE_NS = 1_752_000_000 * 1_000_000_000


def _row(day: int, index: int, *, imbalance: float, label: int) -> dict[str, object]:
    values: dict[str, object] = {col: "0" for col in FEATURE_COLUMNS}
    values["book_imbalance"] = f"{imbalance}"
    values["direction"] = "long"
    values["time_of_day"] = "10:00:00"
    values["stop_distance"] = "8"
    values["target_distance"] = "12"
    values["label"] = label
    values["timestamp_ns"] = _BASE_NS + day * _DAY_NS + index * 1_000_000
    values["label_resolved_timestamp_ns"] = values["timestamp_ns"]
    return values


def test_a_learnable_signal_is_found_and_traded() -> None:
    """book_imbalance perfectly predicts the label -> model takes the winners."""
    rng = random.Random(1)
    rows: list[dict[str, object]] = []
    for day in range(8):
        for i in range(40):
            win = rng.random() < 0.5
            rows.append(_row(day, i, imbalance=1.0 if win else -1.0, label=1 if win else 0))

    result = walk_forward_evaluate(rows, min_train_days=3)
    assert result.evaluated_days >= 1
    assert result.taken_trades > 0
    assert result.taken_win_rate > 0.9, "the perfect signal should be learned out-of-sample"
    assert result.beats_baseline is True


def test_evening_and_following_day_share_one_futures_trading_day() -> None:
    """CME trading days roll at 18:00 New York, not at midnight."""
    from datetime import datetime, timezone

    from app.research.causal_context import trading_day_for_timestamp

    sunday_evening = int(datetime(2026, 7, 19, 22, 30, tzinfo=timezone.utc).timestamp() * 1e9)
    monday_day = int(datetime(2026, 7, 20, 14, 30, tzinfo=timezone.utc).timestamp() * 1e9)

    assert trading_day_for_timestamp(sunday_evening) == "2026-07-20"
    assert trading_day_for_timestamp(monday_day) == "2026-07-20"


def test_walk_forward_purges_labels_resolved_in_the_test_period() -> None:
    from datetime import datetime, timezone

    rows: list[dict[str, object]] = []
    for day in range(5):
        rows.extend([
            _row(day, 0, imbalance=-1.0, label=0),
            _row(day, 1, imbalance=1.0, label=1),
        ])

    validation_morning = int(
        datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc).timestamp() * 1e9
    )
    first_validation_row = int(
        datetime(2026, 7, 17, 14, 30, tzinfo=timezone.utc).timestamp() * 1e9
    )
    # Rebase day 3/4 rows to explicit futures days. The candidate label resolves
    # during the validation trading day but before its first sampled row.
    for offset, row in enumerate(rows[6:8]):
        row["timestamp_ns"] = first_validation_row + offset * 1_000_000
        row["label_resolved_timestamp_ns"] = row["timestamp_ns"]
    rows[4]["label_resolved_timestamp_ns"] = validation_morning

    result = walk_forward_evaluate(rows, min_train_days=3)

    assert result.fold_boundaries
    assert result.fold_boundaries[0]["purged_train_rows"] == 1
    assert result.fold_boundaries[0]["train_rows"] == 5


def test_no_signal_is_reported_honestly() -> None:
    """Random labels uncorrelated with features -> no edge, does not beat baseline."""
    rng = random.Random(2)
    rows: list[dict[str, object]] = []
    for day in range(8):
        for i in range(40):
            rows.append(_row(day, i, imbalance=rng.uniform(-1, 1), label=rng.randint(0, 1)))

    result = walk_forward_evaluate(rows, min_train_days=3)
    assert result.beats_baseline is False
    assert "no out-of-sample edge" in result.note


def test_insufficient_days_is_honest() -> None:
    rows = [_row(0, i, imbalance=0.5, label=i % 2) for i in range(10)]
    result = walk_forward_evaluate(rows, min_train_days=3)
    assert result.evaluated_days == 0
    assert result.beats_baseline is False
    assert "insufficient" in result.note


def test_expectancy_uses_target_stop_and_costs() -> None:
    # A 60% win rate at 12/8 target/stop, 2-tick cost:
    # 0.6*12 - 0.4*8 - 2 = 7.2 - 3.2 - 2 = 2.0 ticks/trade.
    rng = random.Random(3)
    rows: list[dict[str, object]] = []
    for day in range(8):
        for i in range(50):
            win = rng.random() < 0.6
            rows.append(_row(day, i, imbalance=1.0 if win else -1.0, label=1 if win else 0))
    result = walk_forward_evaluate(rows, target_ticks=12, stop_ticks=8, cost_ticks=2, min_train_days=3)
    # The learned model takes near-only winners, so its expectancy is strongly positive.
    assert result.expectancy_ticks > result.baseline_expectancy_ticks
