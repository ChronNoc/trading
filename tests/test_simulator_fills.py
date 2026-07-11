"""Tests for simulated fill decisions."""

from decimal import Decimal

import pytest

from app.market.state import MarketState
from app.simulator.fills import FillAssumptions, SimulatedOrder, decide_fill


def test_decide_fill_partially_fills_buy_order_after_queue_position() -> None:
    """A buy order partially fills from queue-adjusted ask liquidity."""
    state = _book_state()
    order = SimulatedOrder(
        order_id="order-1",
        symbol="MNQ",
        side="buy",
        quantity=4,
        remaining_quantity=4,
        limit_price=Decimal("100.25"),
        submitted_timestamp_ns=100,
    )

    decision = decide_fill(
        order,
        state,
        FillAssumptions(queue_position_fraction=Decimal("0.50")),
    )

    assert decision.filled_quantity == 2
    assert decision.fill_price == Decimal("100.25")
    assert decision.remaining_quantity == 2
    assert decision.partial is True
    assert decision.status == "partially_filled"


def test_decide_fill_fully_fills_sell_order_across_bid_levels() -> None:
    """A sell order can fill across marketable bid levels at an average price."""
    state = _book_state()
    order = SimulatedOrder(
        order_id="order-1",
        symbol="MNQ",
        side="sell",
        quantity=6,
        remaining_quantity=6,
        limit_price=Decimal("99.75"),
        submitted_timestamp_ns=100,
    )

    decision = decide_fill(
        order,
        state,
        FillAssumptions(queue_position_fraction=Decimal("0")),
    )

    assert decision.filled_quantity == 6
    assert decision.fill_price == Decimal("99.91666666666666666666666667")
    assert decision.remaining_quantity == 0
    assert decision.partial is False
    assert decision.status == "filled"


def test_decide_fill_leaves_non_marketable_order_open() -> None:
    """A non-marketable limit order receives no fill."""
    state = _book_state()
    order = SimulatedOrder(
        order_id="order-1",
        symbol="MNQ",
        side="buy",
        quantity=2,
        remaining_quantity=2,
        limit_price=Decimal("100.00"),
        submitted_timestamp_ns=100,
    )

    decision = decide_fill(order, state, FillAssumptions())

    assert decision.filled_quantity == 0
    assert decision.fill_price is None
    assert decision.remaining_quantity == 2
    assert decision.status == "open"


def test_decide_fill_rejects_invalid_queue_fraction() -> None:
    """Queue position fraction must leave some possible available liquidity."""
    state = _book_state()
    order = SimulatedOrder(
        order_id="order-1",
        symbol="MNQ",
        side="buy",
        quantity=1,
        remaining_quantity=1,
        limit_price=Decimal("100.25"),
        submitted_timestamp_ns=100,
    )

    with pytest.raises(ValueError, match="queue_position_fraction"):
        decide_fill(order, state, FillAssumptions(queue_position_fraction=Decimal("1")))


def _book_state() -> MarketState:
    state = MarketState()
    for event in (
        _depth_event(timestamp=0, side="bid", price="100.00", old="0", new="4"),
        _depth_event(timestamp=0, side="bid", price="99.75", old="0", new="4"),
        _depth_event(timestamp=0, side="ask", price="100.25", old="0", new="5"),
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
