"""Pooled walk-forward learning across ALL sessions - the honest edge test.

Per-session models train on ~20-90 in-sample rows and cannot find a signal even
if one exists. This pools every session's triple-barrier rows, sorts by time,
and runs WALK-FORWARD: for each trading day, train only on strictly-earlier days
and predict that day (data the model has never seen). It then asks the only
question that matters: among the trades the model would actually TAKE, is the
net expectancy after costs better than taking every trade?

No look-ahead: training always precedes the tested day. A positive result here
is necessary-but-not-sufficient for the evidence gates; a non-positive result is
honest proof that this feature set has found no edge yet.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    """Honest out-of-sample summary of pooled walk-forward learning."""

    total_rows: int
    trading_days: int
    evaluated_days: int
    oos_predictions: int
    base_rate: float            # win rate taking EVERY out-of-sample trade
    model_accuracy: float       # model accuracy on unseen days
    taken_trades: int           # trades the model chose to take (predicted win)
    taken_win_rate: float       # actual win rate among taken trades
    expectancy_ticks: float     # per taken trade, after costs
    baseline_expectancy_ticks: float  # per trade taking everything, after costs
    beats_baseline: bool
    note: str

    def to_json(self) -> dict[str, object]:
        return asdict(self)


def load_pooled_rows(models_root: Path) -> list[dict[str, object]]:
    """Read every per-session dataset.jsonl under ``models_root``."""
    rows: list[dict[str, object]] = []
    for path in sorted(models_root.rglob("dataset.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _trading_day(timestamp_ns: int) -> str:
    dt = datetime.fromtimestamp(timestamp_ns // 1_000_000_000, tz=timezone.utc).astimezone(NEW_YORK)
    return dt.date().isoformat()


def walk_forward_evaluate(
    rows: list[dict[str, object]],
    *,
    target_ticks: float = 12.0,
    stop_ticks: float = 8.0,
    cost_ticks: float = 2.0,
    min_train_days: int = 3,
) -> WalkForwardResult:
    """Train on the past, predict each later day, and score after costs."""
    from app.machine_learning.train import build_feature_matrix, build_label_array

    ordered = sorted(rows, key=lambda r: int(r["timestamp_ns"]))
    by_day: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in ordered:
        by_day[_trading_day(int(row["timestamp_ns"]))].append(row)
    days = sorted(by_day)

    oos_pred: list[int] = []
    oos_actual: list[int] = []
    evaluated_days = 0
    for index, day in enumerate(days):
        if index < min_train_days:
            continue
        train_rows = [r for earlier in days[:index] for r in by_day[earlier]]
        test_rows = by_day[day]
        train_labels = {int(r["label"]) for r in train_rows}
        if len(train_rows) < 2 or train_labels != {0, 1}:
            continue
        from sklearn.linear_model import LogisticRegression

        x_train = build_feature_matrix(train_rows)
        y_train = build_label_array(train_rows)
        x_test = build_feature_matrix(test_rows)
        y_test = build_label_array(test_rows)
        model = LogisticRegression(max_iter=1_000, solver="liblinear", random_state=7)
        model.fit(x_train, y_train)
        oos_pred.extend(int(p) for p in model.predict(x_test))
        oos_actual.extend(int(a) for a in y_test)
        evaluated_days += 1

    if not oos_pred:
        return WalkForwardResult(
            total_rows=len(rows), trading_days=len(days), evaluated_days=0,
            oos_predictions=0, base_rate=0.0, model_accuracy=0.0, taken_trades=0,
            taken_win_rate=0.0, expectancy_ticks=0.0, baseline_expectancy_ticks=0.0,
            beats_baseline=False,
            note=f"insufficient walk-forward data (need >= {min_train_days + 1} trading days "
                 "with both label classes)")

    n = len(oos_actual)
    base_rate = sum(oos_actual) / n
    accuracy = sum(int(p == a) for p, a in zip(oos_pred, oos_actual)) / n
    taken = [a for p, a in zip(oos_pred, oos_actual) if p == 1]
    taken_win_rate = (sum(taken) / len(taken)) if taken else 0.0

    def expectancy(win_rate: float) -> float:
        return win_rate * target_ticks - (1 - win_rate) * stop_ticks - cost_ticks

    exp_taken = expectancy(taken_win_rate) if taken else 0.0
    exp_baseline = expectancy(base_rate)
    beats = bool(taken and exp_taken > exp_baseline and exp_taken > 0)
    return WalkForwardResult(
        total_rows=len(rows), trading_days=len(days), evaluated_days=evaluated_days,
        oos_predictions=n, base_rate=round(base_rate, 4), model_accuracy=round(accuracy, 4),
        taken_trades=len(taken), taken_win_rate=round(taken_win_rate, 4),
        expectancy_ticks=round(exp_taken, 3), baseline_expectancy_ticks=round(exp_baseline, 3),
        beats_baseline=beats,
        note=("model's taken-trade expectancy beats take-everything after costs"
              if beats else
              "no out-of-sample edge: the model does not beat taking every trade after costs"))
