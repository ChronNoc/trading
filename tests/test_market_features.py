"""Tests for rolling market feature calculations."""

from decimal import Decimal

import pytest

from app.market.features import (
    calculate_bid_reload_count,
    calculate_book_imbalance,
    calculate_distance_from_session_high,
    calculate_distance_from_session_low,
    calculate_liquidity_added,
    calculate_liquidity_cancelled,
    calculate_short_term_volatility,
    calculate_trade_velocity,
    compute_market_features,
)
from app.market.state import MarketState


def test_compute_market_features_from_synthetic_event_sequence() -> None:
    """A rolling event window produces imbalance, liquidity, velocity, and reload features."""
    snapshots = _liquidity_and_reload_snapshots()

    features = compute_market_features(
        snapshots,
        bid_reload_level=1,
        bid_reload_threshold=Decimal("10"),
    )

    assert features.book_imbalance == Decimal("-7") / Decimal("41")
    assert features.liquidity_added == Decimal("12")
    assert features.liquidity_cancelled == Decimal("6")
    assert features.trade_velocity == Decimal("4")
    assert features.short_term_volatility == Decimal("0")
    assert features.distance_from_session_high == Decimal("0.000")
    assert features.distance_from_session_low == Decimal("0.000")
    assert features.bid_reload_count == 1


def test_book_imbalance_returns_zero_when_depth_is_empty() -> None:
    """An empty book has neutral imbalance."""
    assert calculate_book_imbalance(MarketState()) == Decimal("0")


def test_liquidity_added_and_cancelled_are_computed_by_side_and_price() -> None:
    """Liquidity deltas compare visible sizes at each side and price."""
    snapshots = _liquidity_and_reload_snapshots()

    assert calculate_liquidity_added(snapshots) == Decimal("12")
    assert calculate_liquidity_cancelled(snapshots) == Decimal("6")


def test_trade_velocity_returns_zero_for_non_positive_elapsed_time() -> None:
    """Trade velocity is zero when elapsed time cannot be measured."""
    state = _initial_book_state()
    trade_state = state.update(
        {
            "timestamp_ns": state.timestamp_ns,
            "price": "100.25",
            "size": "4",
            "aggressor_side": "buy",
            "instrument": "MNQ",
            "sequence_id": 1,
        },
    )

    assert calculate_trade_velocity((state, trade_state)) == Decimal("0")


def test_short_term_volatility_and_session_distances_use_mid_prices() -> None:
    """Mid-price movement drives volatility and high/low distance features."""
    first = _initial_book_state()
    second = first.update(
        {
            "type": "depth_update",
            "timestamp": 1_000_000_000,
            "symbol": "MNQ",
            "side": "bid",
            "price": "100.00",
            "previous_size": "10",
            "new_size": "0",
        },
    )
    snapshots = (first, second)

    assert first.mid_price == Decimal("100.125")
    assert second.mid_price == Decimal("100.000")
    assert calculate_short_term_volatility(snapshots) == Decimal("0.0625")
    assert calculate_distance_from_session_high(snapshots) == Decimal("0.125")
    assert calculate_distance_from_session_low(snapshots) == Decimal("0.000")


def test_bid_reload_count_counts_multiple_reload_cycles() -> None:
    """Bid reload count increments each time a level returns after dropping below threshold."""
    snapshots = _reload_cycle_snapshots()

    assert calculate_bid_reload_count(
        snapshots,
        level=1,
        threshold=Decimal("10"),
    ) == 2


def test_compute_market_features_rejects_empty_window() -> None:
    """At least one snapshot is required for feature calculation."""
    with pytest.raises(ValueError, match="at least one"):
        compute_market_features(())


def test_bid_reload_count_rejects_invalid_threshold() -> None:
    """Reload thresholds must be positive."""
    with pytest.raises(ValueError, match="threshold"):
        calculate_bid_reload_count(
            (_initial_book_state(),),
            level=1,
            threshold=Decimal("0"),
        )


def _liquidity_and_reload_snapshots() -> tuple[MarketState, ...]:
    state = _initial_book_state()
    snapshots = [state]

    state = state.update(_depth_event(timestamp=500_000_000, side="bid", price="100.00", old="10", new="4"))
    snapshots.append(state)

    state = state.update(_depth_event(timestamp=1_000_000_000, side="bid", price="100.00", old="4", new="12"))
    snapshots.append(state)

    state = state.update(_depth_event(timestamp=1_000_000_000, side="ask", price="100.25", old="10", new="14"))
    snapshots.append(state)

    state = state.update(
        {
            "timestamp_ns": 1_000_000_000,
            "price": "100.25",
            "size": "4",
            "aggressor_side": "buy",
            "instrument": "MNQ",
            "sequence_id": 1,
        },
    )
    snapshots.append(state)

    return tuple(snapshots)


def _reload_cycle_snapshots() -> tuple[MarketState, ...]:
    state = _initial_book_state()
    snapshots = [state]
    for timestamp, new_size in (
        (100, "5"),
        (200, "10"),
        (300, "4"),
        (400, "11"),
    ):
        state = state.update(
            _depth_event(
                timestamp=timestamp,
                side="bid",
                price="100.00",
                old="10",
                new=new_size,
            ),
        )
        snapshots.append(state)

    return tuple(snapshots)


def _initial_book_state() -> MarketState:
    state = MarketState()
    for event in (
        _depth_event(timestamp=0, side="bid", price="100.00", old="0", new="10"),
        _depth_event(timestamp=0, side="bid", price="99.75", old="0", new="5"),
        _depth_event(timestamp=0, side="ask", price="100.25", old="0", new="10"),
        _depth_event(timestamp=0, side="ask", price="100.50", old="0", new="10"),
    ):
        state = state.update(event)
    return state


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
