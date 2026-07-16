"""Day-based splits and walk-forward out-of-sample evaluation (no day is split).

Splits operate on WHOLE trading days: a day's outcomes are never divided between
partitions, so no intraday leakage can cross a split boundary. Walk-forward
folds use an expanding train window and a strictly LATER test window; parameters
may only ever be chosen on train/validation days - the API hands test outcomes
back separately so test data cannot select anything.

All money math is Decimal. With zero outcomes everything is honestly
insufficient - nothing here can pass on an empty sample.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from app.research.real_episodes import CompletedRealOutcome


@dataclass(frozen=True, slots=True)
class DaySplit:
    """Whole-day train/validation/test partition."""

    train_days: tuple[str, ...]
    validation_days: tuple[str, ...]
    test_days: tuple[str, ...]


def split_days(
    days: Sequence[str],
    *,
    train_fraction: Decimal = Decimal("0.6"),
    validation_fraction: Decimal = Decimal("0.2"),
) -> DaySplit:
    """Split distinct trading days chronologically into train/validation/test.

    Operates on day labels only, so a day can never be split. Later days go to
    test - the strictly out-of-sample region.
    """
    if not Decimal("0") < train_fraction < Decimal("1"):
        raise ValueError("train_fraction must be in (0, 1)")
    if not Decimal("0") <= validation_fraction < Decimal("1") - train_fraction:
        raise ValueError("validation_fraction leaves no room for a test partition")
    ordered = sorted(set(days))
    n = len(ordered)
    train_end = int(Decimal(n) * train_fraction)
    validation_end = train_end + int(Decimal(n) * validation_fraction)
    return DaySplit(
        train_days=tuple(ordered[:train_end]),
        validation_days=tuple(ordered[train_end:validation_end]),
        test_days=tuple(ordered[validation_end:]),
    )


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    """One expanding-window fold: train on earlier days, test on later days."""

    fold_index: int
    train_days: tuple[str, ...]
    test_days: tuple[str, ...]


def walk_forward_folds(days: Sequence[str], *, folds: int = 4) -> tuple[WalkForwardFold, ...]:
    """Build expanding-window walk-forward folds over WHOLE days.

    Each fold trains on all days before its test block and tests on the next
    block of days. Requires at least ``folds + 1`` distinct days.
    """
    if folds < 1:
        raise ValueError("folds must be >= 1")
    ordered = sorted(set(days))
    if len(ordered) < folds + 1:
        return ()
    block = max(1, len(ordered) // (folds + 1))
    result: list[WalkForwardFold] = []
    for index in range(folds):
        test_start = block * (index + 1)
        test_end = len(ordered) if index == folds - 1 else test_start + block
        if test_start >= len(ordered):
            break
        result.append(WalkForwardFold(
            fold_index=index,
            train_days=tuple(ordered[:test_start]),
            test_days=tuple(ordered[test_start:test_end]),
        ))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class FoldMetrics:
    """Strictly out-of-sample metrics for one fold (after all costs)."""

    fold_index: int
    trades: int
    net_r_per_trade: Decimal
    net_pnl_per_contract: Decimal


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    """Aggregate walk-forward result; sufficient only with real folds and trades."""

    folds: tuple[FoldMetrics, ...]
    total_test_trades: int
    positive_folds: int

    @property
    def sufficient(self) -> bool:
        """A report is meaningful only with >= 2 folds that each saw trades."""
        return len(self.folds) >= 2 and all(f.trades > 0 for f in self.folds)

    @property
    def all_folds_positive(self) -> bool:
        """Whether every out-of-sample fold had positive net expectancy."""
        return self.sufficient and self.positive_folds == len(self.folds)


def evaluate_walk_forward(
    outcomes: Sequence[CompletedRealOutcome],
    *,
    folds: int = 4,
) -> WalkForwardReport:
    """Evaluate net expectancy after costs on each fold's TEST days only.

    Zero outcomes yield an empty (insufficient) report - never a pass. Only
    outcomes on a fold's test days count toward that fold; train-day outcomes
    are available for parameter selection elsewhere and are never mixed in.
    """
    days = [o.trading_day for o in outcomes]
    fold_defs = walk_forward_folds(days, folds=folds)
    metrics: list[FoldMetrics] = []
    total = 0
    positive = 0
    for fold in fold_defs:
        test_set = [o for o in outcomes if o.trading_day in set(fold.test_days)]
        trades = len(test_set)
        total += trades
        if trades:
            net_r = sum((o.r_multiple for o in test_set), Decimal("0")) / Decimal(trades)
            net_pnl = sum((o.net_pnl_per_contract for o in test_set), Decimal("0")) / Decimal(trades)
        else:
            net_r = net_pnl = Decimal("0")
        if trades and net_pnl > 0:
            positive += 1
        metrics.append(FoldMetrics(fold_index=fold.fold_index, trades=trades,
                                   net_r_per_trade=net_r.quantize(Decimal("0.0001")),
                                   net_pnl_per_contract=net_pnl.quantize(Decimal("0.01"))))
    return WalkForwardReport(folds=tuple(metrics), total_test_trades=total, positive_folds=positive)
