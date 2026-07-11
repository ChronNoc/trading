"""Tests for seeded synthetic MNQ event generation."""

from app.market.state import MarketState
from app.simulator.synthetic_events import (
    SyntheticEventSequence,
    SyntheticPatternWindow,
    generate_mnq_absorption_reclaim_events,
)
from app.strategy.setups import (
    LongAbsorptionReclaimContext,
    SetupEvaluationResult,
    evaluate_long_absorption_reclaim_setup,
)
from app.strategy.spec import StrategySpec, parse_strategy_spec_yaml


def test_generated_events_drive_strategy_acceptance_and_rejection() -> None:
    """The generated clean setup passes while the no-reload lookalike is rejected."""
    sequence = generate_mnq_absorption_reclaim_events(seed=11)
    spec = _strategy_spec()

    clean_result = _evaluate_window(sequence, sequence.clean_window, spec)
    lookalike_result = _evaluate_window(sequence, sequence.lookalike_window, spec)

    assert clean_result.accepted is True
    assert lookalike_result.accepted is False
    assert _condition_passed(lookalike_result, "aggressive_sell_volume") is True
    assert _condition_passed(lookalike_result, "bid_reload_count") is False


def test_generated_events_are_reproducible_for_same_seed() -> None:
    """The seeded generator returns identical events and windows for the same seed."""
    first = generate_mnq_absorption_reclaim_events(seed=17)
    second = generate_mnq_absorption_reclaim_events(seed=17)

    assert first == second


def test_generated_events_change_for_different_seed() -> None:
    """Different seeds change generated sizes while preserving the labeled windows."""
    first = generate_mnq_absorption_reclaim_events(seed=17)
    second = generate_mnq_absorption_reclaim_events(seed=18)

    assert first.events != second.events
    assert first.clean_window == second.clean_window
    assert first.lookalike_window == second.lookalike_window


def _evaluate_window(
    sequence: SyntheticEventSequence,
    window: SyntheticPatternWindow,
    spec: StrategySpec,
) -> SetupEvaluationResult:
    snapshots = _snapshots_for_window(sequence, window)
    return evaluate_long_absorption_reclaim_setup(
        market_state=snapshots[-1],
        feature_window=snapshots,
        strategy_spec=spec,
        context=LongAbsorptionReclaimContext(
            important_level=window.important_level,
            important_level_price=window.important_level_price,
        ),
    )


def _snapshots_for_window(
    sequence: SyntheticEventSequence,
    window: SyntheticPatternWindow,
) -> tuple[MarketState, ...]:
    state = MarketState()
    snapshots: list[MarketState] = []
    for event in sequence.events:
        state = state.update(event)
        if window.start_timestamp_ns <= state.timestamp_ns <= window.end_timestamp_ns:
            snapshots.append(state)

    assert snapshots
    return tuple(snapshots)


def _condition_passed(result: SetupEvaluationResult, key: str) -> bool:
    return next(condition.passed for condition in result.conditions if condition.key == key)


def _strategy_spec() -> StrategySpec:
    return parse_strategy_spec_yaml(
        """
instrument:
  symbol: MNQ
  allowed_contracts: 2
session:
  timezone: America/Chicago
  allowed_start: "08:30"
  allowed_end: "15:00"
context_requirements:
  trend_condition: long_absorption_reclaim
  important_levels:
    - overnight_low
entry_setup:
  liquidity_minimum: 10
  aggressive_volume_minimum: 400
  maximum_price_progress_ticks: 4
  reclaim_ticks: 1
  confirmation_window_ms: 1000
  min_reload_count: 1
  min_ask_pull_ratio: 0.50
risk:
  maximum_trades_per_day: 3
  daily_loss_fraction: "0.02"
  maximum_open_positions: 1
exit:
  stop_method: 6
  target_method: 12
  break_even_rule: 4
  time_stop_seconds: 300
""",
    )
