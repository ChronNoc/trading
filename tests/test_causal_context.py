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


def test_first_hour_levels_appear_only_after_the_hour_ends_and_never_move() -> None:
    """2026-07-10 is daylight time: 09:30-10:30 New York is 13:30-14:30 UTC."""
    tracker = CausalLevelTracker()
    tracker.observe(_ns(datetime(2026, 7, 10, 13, 29, 59, tzinfo=UTC)), Decimal("90"))  # before the open
    tracker.observe(_ns(datetime(2026, 7, 10, 13, 30, 0, tzinfo=UTC)), Decimal("100"))
    tracker.observe(_ns(datetime(2026, 7, 10, 14, 0, 0, tzinfo=UTC)), Decimal("105"))
    tracker.observe(_ns(datetime(2026, 7, 10, 14, 29, 59, tzinfo=UTC)), Decimal("98"))

    forming = tracker.levels_at(_ns(datetime(2026, 7, 10, 14, 15, tzinfo=UTC)))
    assert (forming.ib_high, forming.ib_low) == (None, None)  # still forming, so not a level yet

    tracker.observe(_ns(datetime(2026, 7, 10, 14, 30, 0, tzinfo=UTC)), Decimal("200"))  # 10:30 sharp: outside
    tracker.observe(_ns(datetime(2026, 7, 10, 15, 0, 0, tzinfo=UTC)), Decimal("50"))
    done = tracker.levels_at(_ns(datetime(2026, 7, 10, 15, 30, tzinfo=UTC)))
    assert (done.ib_high, done.ib_low) == (Decimal("105"), Decimal("98"))  # only 09:30-10:30 prices

    tracker.observe(_ns(datetime(2026, 7, 10, 16, 0, tzinfo=UTC)), Decimal("999"))
    assert tracker.levels_at(_ns(datetime(2026, 7, 10, 15, 30, tzinfo=UTC))) == done  # never moves

    # After the 18:00 roll the instant belongs to the NEXT trading day, which has no first hour yet.
    assert tracker.levels_at(_ns(datetime(2026, 7, 10, 23, 0, tzinfo=UTC))).ib_high is None
    # Monday before 10:30: yesterday's first hour is not carried over.
    assert tracker.levels_at(_ns(datetime(2026, 7, 13, 13, 0, tzinfo=UTC))).ib_high is None


def test_first_hour_window_follows_new_york_winter_time() -> None:
    """2026-01-09 is standard time: 09:30-10:30 New York is 14:30-15:30 UTC."""
    tracker = CausalLevelTracker()
    tracker.observe(_ns(datetime(2026, 1, 9, 14, 29, 59, tzinfo=UTC)), Decimal("10"))  # 09:29:59 - not first hour
    tracker.observe(_ns(datetime(2026, 1, 9, 14, 30, 0, tzinfo=UTC)), Decimal("20"))
    tracker.observe(_ns(datetime(2026, 1, 9, 15, 29, 59, tzinfo=UTC)), Decimal("30"))
    tracker.observe(_ns(datetime(2026, 1, 9, 15, 30, 0, tzinfo=UTC)), Decimal("99"))  # 10:30 - not first hour

    levels = tracker.levels_at(_ns(datetime(2026, 1, 9, 16, 0, tzinfo=UTC)))

    assert (levels.ib_high, levels.ib_low) == (Decimal("30"), Decimal("20"))


def test_decision_evidence_records_the_first_hour_levels() -> None:
    tracker = CausalLevelTracker()
    tracker.observe(_ns(datetime(2026, 7, 10, 13, 45, tzinfo=UTC)), Decimal("105"))
    tracker.observe(_ns(datetime(2026, 7, 10, 14, 10, tzinfo=UTC)), Decimal("98"))
    base = _ns(datetime(2026, 7, 10, 15, 0, tzinfo=UTC))
    state = MarketState()
    snapshots = []
    for event in (_depth(base, "bid", "100", "0", "10"), _depth(base + 1, "ask", "100.25", "0", "10")):
        state = state.update(event)
        snapshots.append(state)

    derived = derive_strategy_context(
        tuple(snapshots), TradeDirection.LONG, tracker, OrderFlowThresholds(),
        stop_buffer_points=Decimal("10"),
    )

    assert (derived.context.levels.ib_high, derived.context.levels.ib_low) == (Decimal("105"), Decimal("98"))
    evidence = derived.evidence()
    assert (evidence["ib_high"], evidence["ib_low"]) == ("105", "98")


def _depth(timestamp: int, side: str, price: str, previous: str, new: str) -> dict[str, object]:
    return {
        "type": "depth_update", "timestamp": timestamp, "symbol": "MNQ", "side": side,
        "price": price, "previous_size": previous, "new_size": new,
    }


def _ns(value: datetime) -> int:
    return int(value.timestamp()) * 1_000_000_000
