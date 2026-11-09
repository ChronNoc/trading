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


def test_crossed_book_from_lossy_feed_self_heals() -> None:
    """Ghost levels left by dropped removals are purged; spread never negative."""
    state = MarketState()
    for event in (
        {"type": "depth_update", "timestamp": 1, "symbol": "MNQ", "side": "ask", "price": "29558.50", "previous_size": "0", "new_size": "5"},
        {"type": "depth_update", "timestamp": 2, "symbol": "MNQ", "side": "bid", "price": "29557.00", "previous_size": "0", "new_size": "7"},
        # Feed drops the ask removal, then a real bid arrives ABOVE the stale ask.
        {"type": "depth_update", "timestamp": 3, "symbol": "MNQ", "side": "bid", "price": "29565.75", "previous_size": "0", "new_size": "3"},
    ):
        state = state.update(event)

    assert state.best_bid == Decimal("29565.75")
    assert state.best_ask is None or state.best_ask > state.best_bid
    assert state.spread is None or state.spread > 0


def test_crossed_book_repair_works_in_both_directions() -> None:
    """A fresh ask below a stale bid purges the crossed bid levels too."""
    state = MarketState()
    for event in (
        {"type": "depth_update", "timestamp": 1, "symbol": "MNQ", "side": "bid", "price": "29570.00", "previous_size": "0", "new_size": "5"},
        {"type": "depth_update", "timestamp": 2, "symbol": "MNQ", "side": "ask", "price": "29560.00", "previous_size": "0", "new_size": "4"},
    ):
        state = state.update(event)

    assert state.best_ask == Decimal("29560.00")
    assert state.best_bid is None or state.best_bid < state.best_ask


def test_zero_size_updates_do_not_trigger_book_repair() -> None:
    """Removal events never purge the opposite side."""
    state = MarketState()
    for event in (
        {"type": "depth_update", "timestamp": 1, "symbol": "MNQ", "side": "ask", "price": "29560.00", "previous_size": "0", "new_size": "4"},
        {"type": "depth_update", "timestamp": 2, "symbol": "MNQ", "side": "bid", "price": "29565.00", "previous_size": "5", "new_size": "0"},
    ):
        state = state.update(event)

    assert state.best_ask == Decimal("29560.00")


def test_cumulative_volume_delta_tracks_aggressor_flow() -> None:
    """CVD is aggressive buys minus aggressive sells, updated per trade."""
    state = MarketState()
    trades = (
        ("sell", "500"),
        ("sell", "300"),
        ("buy", "600"),
    )
    for index, (side, size) in enumerate(trades):
        state = state.update(
            {
                "timestamp_ns": 100 + index,
                "price": "29770.00",
                "size": size,
                "aggressor_side": side,
                "instrument": "MNQ",
                "sequence_id": index + 1,
            },
        )

    assert state.cumulative_volume_delta == Decimal("-200")
    assert state.executed_buy_volume == Decimal("600")
    assert state.executed_sell_volume == Decimal("800")


# --- the fast book update is the old sorting update, exactly (2026-11-09 replay speed-up) ----------


def _reference_book_update(state: MarketState, side: str, price: Decimal, size: Decimal) -> tuple:
    """The update as it was written before the speed-up: rebuild, sort, trim, filter."""
    from app.market.state import MAX_DEPTH_LEVELS

    def update(depth, descending):
        levels = {level.price: level.size for level in depth}
        if size == 0:
            levels.pop(price, None)
        else:
            levels[price] = size
        ordered = sorted(levels, reverse=descending)[:MAX_DEPTH_LEVELS]
        return tuple(DepthLevel(price=p, size=levels[p]) for p in ordered)

    bids, asks = state.bid_depth, state.ask_depth
    if side == "bid":
        bids = update(bids, True)
        if size > 0:
            asks = tuple(level for level in asks if level.price >= price)
    else:
        asks = update(asks, False)
        if size > 0:
            bids = tuple(level for level in bids if level.price <= price)
    return bids, asks


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_the_fast_book_update_matches_the_sorting_update_on_random_events(seed: int) -> None:
    import random

    rng = random.Random(seed)
    state = MarketState()
    for step in range(4000):
        side = rng.choice(("bid", "ask"))
        # prices around a drifting centre, so sides fill past 20 levels, cross, and empty again
        centre = 20000 + (step // 400) * 2
        price = Decimal(centre) + Decimal(rng.randint(-30, 30)) * Decimal("0.25")
        size = Decimal(0) if rng.random() < 0.3 else Decimal(rng.randint(1, 120))
        expected = _reference_book_update(state, side, price, size)
        state = state.update({"type": "depth_update", "side": side, "price": str(price),
                              "previous_size": "0", "new_size": str(size), "timestamp": step + 1})
        assert (state.bid_depth, state.ask_depth) == expected, step
        assert state.best_bid == (expected[0][0].price if expected[0] else None)
        assert state.best_ask == (expected[1][0].price if expected[1] else None)


def test_a_hand_built_book_out_of_order_still_updates_exactly_as_before() -> None:
    from app.market.state import MAX_DEPTH_LEVELS

    unordered = MarketState(
        bid_depth=(DepthLevel(Decimal("99"), Decimal("1")), DepthLevel(Decimal("101"), Decimal("2")),
                   DepthLevel(Decimal("100"), Decimal("3"))),
        ask_depth=(DepthLevel(Decimal("104"), Decimal("1")), DepthLevel(Decimal("102"), Decimal("2"))),
    )
    for side, price, size in (("bid", "100.5", "4"), ("ask", "103", "5"), ("bid", "103", "1"), ("ask", "99.5", "2")):
        expected = _reference_book_update(unordered, side, Decimal(price), Decimal(size))
        updated = unordered.update({"type": "depth_update", "side": side, "price": price,
                                    "previous_size": "0", "new_size": size, "timestamp": 1})
        assert (updated.bid_depth, updated.ask_depth) == expected
    too_long = MarketState(bid_depth=tuple(DepthLevel(Decimal(200 - i), Decimal("1"))
                                           for i in range(MAX_DEPTH_LEVELS + 5)))
    expected = _reference_book_update(too_long, "bid", Decimal("150"), Decimal("9"))
    updated = too_long.update({"type": "depth_update", "side": "bid", "price": "150", "previous_size": "0",
                               "new_size": "9", "timestamp": 1})
    assert (updated.bid_depth, updated.ask_depth) == expected


def test_a_trade_keeps_the_book_and_adds_volume_on_its_side() -> None:
    book = MarketState().update({"type": "depth_update", "side": "bid", "price": "100", "previous_size": "0",
                                 "new_size": "5", "timestamp": 1})
    traded = book.update({"type": "trade", "timestamp_ns": 2, "price": "100", "size": "3",
                          "aggressor_side": "sell", "instrument": "MNQ", "sequence_id": 1})
    assert traded.bid_depth == book.bid_depth and traded.best_bid == book.best_bid
    assert traded.executed_sell_volume == Decimal("3") and traded.executed_buy_volume == 0
    assert traded.timestamp_ns == 2
