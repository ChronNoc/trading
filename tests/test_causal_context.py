"""Tests for no-lookahead prior-day, overnight, block, stop, and DOL context."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.market.state import MarketState
from app.research.causal_context import CausalLevelTracker, derive_strategy_context
from app.strategy.order_flow import OrderFlowThresholds, TradeDirection


def test_tracker_exposes_only_completed_prior_rth_and_observed_overnight() -> None:
    tracker = CausalLevelTracker()
    tracker.observe(_ns(datetime(2026, 7, 9, 14, 0, tzinfo=UTC)), Decimal("100"))
    tracker.observe(_ns(datetime(2026, 7, 9, 19, 0, tzinfo=UTC)), Decimal("105"))
    tracker.observe(_ns(datetime(2026, 7, 9, 23, 0, tzinfo=UTC)), Decimal("103"))
    tracker.observe(_ns(datetime(2026, 7, 10, 3, 0, tzinfo=UTC)), Decimal("98"))

    before_future = tracker.levels_at(_ns(datetime(2026, 7, 10, 13, 35, tzinfo=UTC)))
    assert before_future.prior_day_high == Decimal("105")
    assert before_future.prior_day_low == Decimal("100")
    assert before_future.overnight_high == Decimal("103")
    assert before_future.overnight_low == Decimal("98")

    tracker.observe(_ns(datetime(2026, 7, 10, 15, 0, tzinfo=UTC)), Decimal("110"))
    assert before_future.prior_day_high == Decimal("105")


def test_new_york_open_respects_daylight_saving_time() -> None:
    tracker = CausalLevelTracker()
    summer = tracker.session_open_timestamp_ns(_ns(datetime(2026, 7, 10, 16, 0, tzinfo=UTC)))
    winter = tracker.session_open_timestamp_ns(_ns(datetime(2026, 1, 9, 17, 0, tzinfo=UTC)))
    assert datetime.fromtimestamp(summer / 1_000_000_000, tz=UTC).hour == 13
    assert datetime.fromtimestamp(winter / 1_000_000_000, tz=UTC).hour == 14


def test_context_does_not_invent_defended_stop_or_target_without_evidence() -> None:
    state = MarketState().update(_depth(1, "bid", "100", "0", "10"))
    tracker = CausalLevelTracker()
    tracker.observe(state.timestamp_ns, state.best_bid)
    derived = derive_strategy_context(
        (state,), TradeDirection.LONG, tracker, OrderFlowThresholds(),
        stop_buffer_points=Decimal("10"),
    )
    assert derived.context.defended_level_price is None
    assert derived.context.stop_price is None
    assert derived.context.target_price is None


def test_context_uses_durable_block_and_visible_dol() -> None:
    base = _ns(datetime(2026, 7, 10, 14, 0, tzinfo=UTC))
    state = MarketState()
    snapshots: list[MarketState] = []
    for event in (
        _depth(base, "bid", "100", "0", "120"),
        _depth(base + 1, "ask", "100.25", "0", "20"),
        _depth(base + 2, "ask", "102", "0", "120"),
        _depth(base + 3, "bid", "100", "120", "125"),
        _depth(base + 4, "bid", "100", "125", "130"),
    ):
        state = state.update(event)
        snapshots.append(state)
    tracker = CausalLevelTracker()
    for snapshot in snapshots:
        tracker.observe(snapshot.timestamp_ns, snapshot.mid_price or snapshot.best_bid)
    derived = derive_strategy_context(
        tuple(snapshots), TradeDirection.LONG, tracker, OrderFlowThresholds(),
        stop_buffer_points=Decimal("10"),
    )
    assert derived.context.defended_level_price == Decimal("100")
    assert derived.context.stop_price == Decimal("90")
    assert derived.context.target_price == Decimal("102")
    assert derived.direction_of_liquidity is not None
    assert derived.direction_of_liquidity.source == "ask liquidity block"


def _depth(timestamp: int, side: str, price: str, previous: str, new: str) -> dict[str, object]:
    return {
        "type": "depth_update", "timestamp": timestamp, "symbol": "MNQ", "side": side,
        "price": price, "previous_size": previous, "new_size": new,
    }


def _ns(value: datetime) -> int:
    return int(value.timestamp()) * 1_000_000_000
