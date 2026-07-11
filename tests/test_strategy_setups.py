"""Tests for explainable strategy setup evaluation."""

from __future__ import annotations

from decimal import Decimal

from pytest import MonkeyPatch

from app.market.state import MarketState
from app.strategy.setups import (
    LongAbsorptionReclaimContext,
    SetupConditionResult,
    SetupEvaluationResult,
    evaluate_long_absorption_reclaim_setup,
)
from app.strategy.spec import ImportantLevel, StrategySpec, parse_strategy_spec_yaml


def test_long_absorption_reclaim_setup_accepts_fully_passing_case() -> None:
    """A synthetic absorption-reclaim sequence passes every sub-condition."""
    spec = _strategy_spec()
    snapshots = _passing_snapshots()
    result = evaluate_long_absorption_reclaim_setup(
        market_state=snapshots[-1],
        feature_window=snapshots,
        strategy_spec=spec,
        context=_context(),
    )

    assert result.accepted is True
    assert [condition.key for condition in result.conditions] == [
        "at_important_level",
        "aggressive_sell_volume",
        "downward_progress_ticks",
        "bid_reload_count",
        "ask_cancellation_ratio",
        "reclaimed_level",
        "news_lockout",
    ]
    assert all(condition.passed for condition in result.conditions)
    assert result.render_lines()[0] == "LONG SETUP: ACCEPTED"
    assert "[pass] 412 contracts sold aggressively" in result.render_lines()


def test_long_absorption_reclaim_setup_fails_when_not_at_configured_important_level() -> None:
    """The important-level condition fails when the level is not enabled in the spec."""
    result = _evaluate_with_context(
        LongAbsorptionReclaimContext(
            important_level=ImportantLevel.PRIOR_DAY_HIGH,
            important_level_price=Decimal("100.00"),
        ),
    )

    assert _condition(result, "at_important_level").passed is False
    assert "not enabled" in _condition(result, "at_important_level").message
    assert result.accepted is False


def test_long_absorption_reclaim_setup_fails_for_insufficient_aggressive_sell_volume() -> None:
    """The aggressive-volume condition fails below the spec threshold."""
    snapshots = _passing_snapshots(sell_volume=Decimal("399"))

    result = _evaluate_with_snapshots(snapshots)

    assert _condition(result, "aggressive_sell_volume").passed is False
    assert "Only 399" in _condition(result, "aggressive_sell_volume").message


def test_long_absorption_reclaim_setup_fails_when_downward_progress_exceeds_threshold() -> None:
    """The downward-progress condition fails when price pushes too far through the level."""
    snapshots = _passing_snapshots(include_excessive_downward_progress=True)

    result = _evaluate_with_snapshots(snapshots)

    assert _condition(result, "downward_progress_ticks").passed is False
    assert "maximum allowed" in _condition(result, "downward_progress_ticks").message


def test_long_absorption_reclaim_setup_fails_when_bid_does_not_reload() -> None:
    """The bid-reload condition fails when the level never drops and reloads."""
    snapshots = _passing_snapshots(include_bid_reload=False)

    result = _evaluate_with_snapshots(snapshots)

    assert _condition(result, "bid_reload_count").passed is False
    assert "reloaded 0 times" in _condition(result, "bid_reload_count").message


def test_long_absorption_reclaim_setup_fails_when_ask_pull_ratio_is_too_low() -> None:
    """The ask-cancellation condition fails when visible ask liquidity is not pulled."""
    snapshots = _passing_snapshots(include_ask_cancellation=False)

    result = _evaluate_with_snapshots(snapshots)

    assert _condition(result, "ask_cancellation_ratio").passed is False
    assert "below required" in _condition(result, "ask_cancellation_ratio").message


def test_long_absorption_reclaim_setup_fails_when_level_is_not_reclaimed() -> None:
    """The reclaim condition fails when current price has not reclaimed enough ticks."""
    snapshots = _passing_snapshots(include_reclaim=False)

    result = _evaluate_with_snapshots(snapshots)

    assert _condition(result, "reclaimed_level").passed is False
    assert "not completed" in _condition(result, "reclaimed_level").message


def test_long_absorption_reclaim_setup_fails_during_news_lockout(monkeypatch: MonkeyPatch) -> None:
    """The news-lockout condition fails when the stub reports an active lockout."""
    monkeypatch.setattr("app.strategy.setups.is_news_lockout_active", lambda timestamp_ns: True)

    result = _evaluate_with_snapshots(_passing_snapshots())

    assert _condition(result, "news_lockout").passed is False
    assert "News lockout" in _condition(result, "news_lockout").message


def test_long_absorption_reclaim_setup_fails_when_threshold_is_unresolved() -> None:
    """Unresolved setup thresholds fail the specific dependent condition."""
    spec = _strategy_spec(aggressive_volume_minimum="unresolved")
    snapshots = _passing_snapshots()

    result = evaluate_long_absorption_reclaim_setup(
        market_state=snapshots[-1],
        feature_window=snapshots,
        strategy_spec=spec,
        context=_context(),
    )

    assert _condition(result, "aggressive_sell_volume").passed is False
    assert "unresolved" in _condition(result, "aggressive_sell_volume").message


def _evaluate_with_snapshots(snapshots: tuple[MarketState, ...]) -> SetupEvaluationResult:
    return evaluate_long_absorption_reclaim_setup(
        market_state=snapshots[-1],
        feature_window=snapshots,
        strategy_spec=_strategy_spec(),
        context=_context(),
    )


def _evaluate_with_context(context: LongAbsorptionReclaimContext) -> SetupEvaluationResult:
    snapshots = _passing_snapshots()
    return evaluate_long_absorption_reclaim_setup(
        market_state=snapshots[-1],
        feature_window=snapshots,
        strategy_spec=_strategy_spec(),
        context=context,
    )


def _condition(result: SetupEvaluationResult, key: str) -> SetupConditionResult:
    return next(condition for condition in result.conditions if condition.key == key)


def _passing_snapshots(
    *,
    sell_volume: Decimal = Decimal("412"),
    include_excessive_downward_progress: bool = False,
    include_bid_reload: bool = True,
    include_ask_cancellation: bool = True,
    include_reclaim: bool = True,
) -> tuple[MarketState, ...]:
    state = MarketState()
    for event in (
        _depth_event(timestamp=0, side="bid", price="100.00", old="0", new="10"),
        _depth_event(timestamp=0, side="ask", price="100.25", old="0", new="10"),
    ):
        state = state.update(event)

    snapshots = [state]

    if include_excessive_downward_progress:
        state = state.update(_depth_event(timestamp=100, side="bid", price="100.00", old="10", new="0"))
        state = state.update(_depth_event(timestamp=100, side="bid", price="98.75", old="0", new="12"))
        state = state.update(_depth_event(timestamp=100, side="ask", price="99.00", old="0", new="12"))
        snapshots.append(state)

    if include_bid_reload:
        state = state.update(_depth_event(timestamp=200, side="bid", price="100.00", old="10", new="4"))
        snapshots.append(state)
        state = state.update(_depth_event(timestamp=300, side="bid", price="100.00", old="4", new="12"))
        snapshots.append(state)

    if include_ask_cancellation:
        state = state.update(_depth_event(timestamp=400, side="ask", price="100.25", old="10", new="2"))
    else:
        state = state.update(_depth_event(timestamp=400, side="ask", price="100.25", old="10", new="14"))
    snapshots.append(state)

    state = state.update(
        {
            "timestamp_ns": 500,
            "price": "100.00",
            "size": sell_volume,
            "aggressor_side": "sell",
            "instrument": "MNQ",
            "sequence_id": 1,
        },
    )
    snapshots.append(state)

    if include_reclaim:
        state = state.update(_depth_event(timestamp=600, side="bid", price="100.25", old="0", new="12"))
        snapshots.append(state)

    return tuple(snapshots)


def _context() -> LongAbsorptionReclaimContext:
    return LongAbsorptionReclaimContext(
        important_level=ImportantLevel.OVERNIGHT_LOW,
        important_level_price=Decimal("100.00"),
    )


def _strategy_spec(aggressive_volume_minimum: str | int = 400) -> StrategySpec:
    return parse_strategy_spec_yaml(
        f"""
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
  aggressive_volume_minimum: {aggressive_volume_minimum}
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


def _depth_event(
    *,
    timestamp: int,
    side: str,
    price: str,
    old: str,
    new: str,
) -> dict[str, object]:
    return {
        "type": "depth_update",
        "timestamp": timestamp,
        "symbol": "MNQ",
        "side": side,
        "price": price,
        "previous_size": old,
        "new_size": new,
    }
