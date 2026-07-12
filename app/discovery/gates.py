"""Candidate acceptance gates - items 14 through 20.

Every gate returns an explicit pass/fail with a reason string, mirroring
the strategy engine's explainable-conditions philosophy. A candidate must
clear all of them before it may appear in a recommendation report.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.discovery.backtest import ParameterSet
from app.discovery.metrics import TradeResult, composite_score, expectancy_r


@dataclass(frozen=True, slots=True)
class GateResult:
    """Pass/fail outcome of one gate with its explanation."""

    gate: str
    passed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """Declares when a feature's inputs are available - leakage guard input."""

    name: str
    available_at: str  # "decision_time" or "after_outcome"


def sample_size_gate(trades: Sequence[TradeResult], *, minimum_trades: int = 100) -> GateResult:
    """Reject candidates below the minimum trade count - noise, not edge."""
    if len(trades) < minimum_trades:
        return GateResult(
            gate="sample_size",
            passed=False,
            reason=f"only {len(trades)} trades; minimum is {minimum_trades} - a small sample is noise",
        )
    return GateResult(gate="sample_size", passed=True, reason=f"{len(trades)} trades meets minimum {minimum_trades}")


def regime_robustness_gate(
    trades: Sequence[TradeResult],
    *,
    min_positive_buckets: int = 2,
) -> GateResult:
    """Reject performance that exists only in one narrow regime or session.

    Buckets trades by regime tag and by session; requires positive
    expectancy in at least ``min_positive_buckets`` regime buckets AND
    at least ``min_positive_buckets`` session buckets.
    """
    by_regime: dict[str, list[Decimal]] = defaultdict(list)
    by_session: dict[str, list[Decimal]] = defaultdict(list)
    for trade in trades:
        by_regime[trade.regime_tag].append(trade.r_multiple)
        by_session[trade.session].append(trade.r_multiple)
    positive_regimes = [tag for tag, values in by_regime.items() if expectancy_r(values) > 0]
    positive_sessions = [tag for tag, values in by_session.items() if expectancy_r(values) > 0]
    if len(positive_regimes) < min_positive_buckets:
        return GateResult(
            gate="regime_robustness",
            passed=False,
            reason=f"positive expectancy in only {len(positive_regimes)} regime bucket(s): {sorted(positive_regimes)}",
        )
    if len(positive_sessions) < min_positive_buckets:
        return GateResult(
            gate="regime_robustness",
            passed=False,
            reason=f"positive expectancy in only {len(positive_sessions)} session bucket(s): {sorted(positive_sessions)}",
        )
    return GateResult(
        gate="regime_robustness",
        passed=True,
        reason=f"positive in regimes {sorted(positive_regimes)} and sessions {sorted(positive_sessions)}",
    )


def regime_breakdown(trades: Sequence[TradeResult]) -> dict[str, Decimal]:
    """Per-regime expectancy for reporting - never one blended number."""
    by_regime: dict[str, list[Decimal]] = defaultdict(list)
    for trade in trades:
        by_regime[trade.regime_tag].append(trade.r_multiple)
    return {tag: expectancy_r(values) for tag, values in sorted(by_regime.items())}


def sensitivity_gate(
    parameters: ParameterSet,
    evaluate: Callable[[ParameterSet], Sequence[TradeResult]],
    *,
    max_composite_drop_ratio: Decimal = Decimal("0.6"),
) -> GateResult:
    """Perturb each parameter slightly and re-test - reject fragile optima."""
    base_trades = evaluate(parameters)
    base = composite_score(base_trades).composite
    if base <= 0:
        return GateResult(gate="sensitivity", passed=False, reason="base composite is not positive")
    neighbors = _neighbor_parameters(parameters)
    worst = base
    for neighbor in neighbors:
        neighbor_composite = composite_score(evaluate(neighbor)).composite
        worst = min(worst, neighbor_composite)
    floor = base * max_composite_drop_ratio
    if worst < floor:
        return GateResult(
            gate="sensitivity",
            passed=False,
            reason=f"perturbed composite fell to {worst} vs base {base} - fragile/overfit parameters",
        )
    return GateResult(gate="sensitivity", passed=True, reason=f"neighbors held composite >= {worst} vs base {base}")


def _neighbor_parameters(parameters: ParameterSet) -> tuple[ParameterSet, ...]:
    volume_step = Decimal("50")
    ratio_step = Decimal("0.05")
    neighbors = [
        ParameterSet(
            min_reload_count=max(0, parameters.min_reload_count - 1),
            min_aggressive_volume=parameters.min_aggressive_volume,
            min_ask_pull_ratio=parameters.min_ask_pull_ratio,
            target_r=parameters.target_r,
            stop_r=parameters.stop_r,
        ),
        ParameterSet(
            min_reload_count=parameters.min_reload_count,
            min_aggressive_volume=parameters.min_aggressive_volume + volume_step,
            min_ask_pull_ratio=parameters.min_ask_pull_ratio,
            target_r=parameters.target_r,
            stop_r=parameters.stop_r,
        ),
        ParameterSet(
            min_reload_count=parameters.min_reload_count,
            min_aggressive_volume=max(Decimal("0"), parameters.min_aggressive_volume - volume_step),
            min_ask_pull_ratio=min(Decimal("0.95"), parameters.min_ask_pull_ratio + ratio_step),
            target_r=parameters.target_r,
            stop_r=parameters.stop_r,
        ),
    ]
    return tuple(neighbors)


@dataclass(frozen=True, slots=True)
class MonteCarloResult:
    """Resampled drawdown distribution and risk-of-ruin estimate."""

    resamples: int
    median_drawdown_r: Decimal
    p95_drawdown_r: Decimal
    risk_of_ruin: Decimal


def monte_carlo_gate(
    trades: Sequence[TradeResult],
    *,
    seed: int,
    resamples: int = 500,
    ruin_threshold_r: Decimal = Decimal("10"),
    max_risk_of_ruin: Decimal = Decimal("0.05"),
) -> tuple[GateResult, MonteCarloResult]:
    """Bootstrap the trade sequence to estimate drawdown and ruin risk."""
    r_values = [trade.r_multiple for trade in trades]
    if not r_values:
        empty = MonteCarloResult(0, Decimal("0"), Decimal("0"), Decimal("1"))
        return GateResult(gate="monte_carlo", passed=False, reason="no trades to resample"), empty
    rng = random.Random(seed)
    drawdowns: list[Decimal] = []
    ruined = 0
    for _ in range(resamples):
        equity = Decimal("0")
        peak = Decimal("0")
        worst = Decimal("0")
        for _ in range(len(r_values)):
            equity += rng.choice(r_values)
            peak = max(peak, equity)
            worst = max(worst, peak - equity)
        drawdowns.append(worst)
        if worst >= ruin_threshold_r:
            ruined += 1
    drawdowns.sort()
    result = MonteCarloResult(
        resamples=resamples,
        median_drawdown_r=drawdowns[len(drawdowns) // 2],
        p95_drawdown_r=drawdowns[int(len(drawdowns) * 0.95)],
        risk_of_ruin=(Decimal(ruined) / Decimal(resamples)).quantize(Decimal("0.0001")),
    )
    if result.risk_of_ruin > max_risk_of_ruin:
        return (
            GateResult(
                gate="monte_carlo",
                passed=False,
                reason=f"risk of ruin {result.risk_of_ruin} exceeds {max_risk_of_ruin} "
                f"(p95 drawdown {result.p95_drawdown_r}R)",
            ),
            result,
        )
    return (
        GateResult(
            gate="monte_carlo",
            passed=True,
            reason=f"risk of ruin {result.risk_of_ruin}, p95 drawdown {result.p95_drawdown_r}R",
        ),
        result,
    )


def leakage_gate(features: Sequence[FeatureSpec]) -> GateResult:
    """Reject any feature computed with information unavailable at decision time."""
    leaking = [feature.name for feature in features if feature.available_at != "decision_time"]
    if leaking:
        return GateResult(
            gate="label_leakage",
            passed=False,
            reason=f"features use post-decision information: {leaking}",
        )
    return GateResult(gate="label_leakage", passed=True, reason=f"all {len(features)} features decision-time safe")
