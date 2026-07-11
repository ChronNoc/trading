"""Tests for immutable market-state updates."""

from decimal import Decimal

import pytest

from app.market.state import DepthLevel, MarketState


def test_depth_update_returns_new_state_with_best_prices_and_mid() -> None:
    """Depth updates rebuild top-of-book fields without mutating the prior state."""
    initial_state = MarketState()

    bid_state = initial_state.update(
        {
            "type": "depth_update",
            "timestamp": 100,
            "symbol": "MNQ",
            "side": "bid",
            "price": "100.00",
            "previous_size": "0",
            "new_size": "10",
        },
    )
    ask_state = bid_state.update(
        {
            "type": "depth_update",
            "timestamp": 200,
            "symbol": "MNQ",
            "side": "ask",
            "price": "100.25",
            "previous_size": "0",
            "new_size": "8",
        },
    )

    assert initial_state.best_bid is None
    assert bid_state.best_bid == Decimal("100.00")
    assert bid_state.best_ask is None
    assert ask_state.best_bid == Decimal("100.00")
    assert ask_state.best_ask == Decimal("100.25")
    assert ask_state.spread == Decimal("0.25")
    assert ask_state.mid_price == Decimal("100.125")
    assert ask_state.timestamp_ns == 200
    assert ask_state.depth["bid"] == (DepthLevel(price=Decimal("100.00"), size=Decimal("10")),)


def test_depth_update_sorts_sides_and_keeps_twenty_visible_levels() -> None:
    """Bid depth sorts high-to-low and ask depth sorts low-to-high, capped at 20 levels."""
    state = MarketState()
    for offset in range(25):
        state = state.update(
            {
                "type": "depth_update",
                "timestamp": offset,
                "symbol": "MNQ",
                "side": "bid",
                "price": Decimal("100") - (Decimal(offset) * Decimal("0.25")),
                "previous_size": "0",
                "new_size": "1",
            },
        )
        state = state.update(
            {
                "type": "depth_update",
                "timestamp": offset,
                "symbol": "MNQ",
                "side": "ask",
                "price": Decimal("100.25") + (Decimal(offset) * Decimal("0.25")),
                "previous_size": "0",
                "new_size": "1",
            },
        )

    assert len(state.bid_depth) == 20
    assert len(state.ask_depth) == 20
    assert state.bid_depth[0].price == Decimal("100.00")
    assert state.bid_depth[-1].price == Decimal("95.25")
    assert state.ask_depth[0].price == Decimal("100.25")
    assert state.ask_depth[-1].price == Decimal("105.00")


def test_depth_update_removes_level_when_new_size_is_zero() -> None:
    """A zero-size depth update removes that price level."""
    state = _book_state()

    updated = state.update(
        {
            "type": "depth_update",
            "timestamp": 300,
            "symbol": "MNQ",
            "side": "bid",
            "price": "100.00",
            "previous_size": "10",
            "new_size": "0",
        },
    )

    assert updated.best_bid == Decimal("99.75")
    assert updated.bid_depth == (DepthLevel(price=Decimal("99.75"), size=Decimal("5")),)


def test_trade_update_accumulates_aggressor_volume_without_changing_book() -> None:
    """Trade events update buy/sell volume while preserving visible depth."""
    state = _book_state()

    buy_state = state.update(
        {
            "timestamp_ns": 400,
            "price": "100.25",
            "size": "3",
            "aggressor_side": "buy",
            "instrument": "MNQ",
            "sequence_id": 1,
        },
    )
    sell_state = buy_state.update(
        {
            "timestamp_ns": 500,
            "price": "100.00",
            "size": "2",
            "aggressor_side": "sell",
            "instrument": "MNQ",
            "sequence_id": 2,
        },
    )

    assert state.executed_buy_volume == Decimal("0")
    assert buy_state.executed_buy_volume == Decimal("3")
    assert buy_state.executed_sell_volume == Decimal("0")
    assert sell_state.executed_buy_volume == Decimal("3")
    assert sell_state.executed_sell_volume == Decimal("2")
    assert sell_state.bid_depth == state.bid_depth
    assert sell_state.ask_depth == state.ask_depth


def test_market_state_rejects_unknown_event_schema() -> None:
    """Unknown event shapes raise a clear error."""
    with pytest.raises(ValueError, match="Unsupported market event"):
        MarketState().update({"type": "heartbeat"})


def test_market_state_rejects_invalid_depth_side() -> None:
    """Depth events must use bid or ask sides."""
    with pytest.raises(ValueError, match="side must be bid or ask"):
        MarketState().update(
            {
                "type": "depth_update",
                "timestamp": 100,
                "symbol": "MNQ",
                "side": "middle",
                "price": "100",
                "previous_size": "0",
                "new_size": "1",
            },
        )


def _book_state() -> MarketState:
    state = MarketState()
    for event in (
        _depth_event(timestamp=100, side="bid", price="100.00", previous_size="0", new_size="10"),
        _depth_event(timestamp=100, side="bid", price="99.75", previous_size="0", new_size="5"),
        _depth_event(timestamp=100, side="ask", price="100.25", previous_size="0", new_size="8"),
    ):
        state = state.update(event)
    return state


def _depth_event(
    *,
    timestamp: int,
    side: str,
    price: str,
    previous_size: str,
    new_size: str,
) -> dict[str, object]:
    return {
        "type": "depth_update",
        "timestamp": timestamp,
        "symbol": "MNQ",
        "side": side,
        "price": price,
        "previous_size": previous_size,
        "new_size": new_size,
    }
