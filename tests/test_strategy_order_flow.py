"""Tests for the handwritten day-trading order-flow checklist."""

from __future__ import annotations

from decimal import Decimal

from app.market.state import MarketState
from app.strategy.order_flow import (
    BookSide,
    ControlSide,
    OrderFlowPlanContext,
    OrderFlowThresholds,
    StrategyLevels,
    TradeDirection,
    calculate_cvd_delta,
    detect_liquidity_blocks,
    evaluate_day_trading_plan,
    find_direction_of_liquidity,
    identify_controlling_side,
)
from app.strategy.setups import SetupConditionResult, SetupEvaluationResult


def test_order_flow_plan_accepts_clean_long_absorption_continuation() -> None:
    """A clean long sequence passes DOL, absorption, reload, loading, and continuation."""
    snapshots = _long_absorption_snapshots()
    result = evaluate_day_trading_plan(snapshots, _long_context())

    assert result.accepted is True
    assert [condition.key for condition in result.conditions] == [
        "opening_observation_complete",
        "clear_dol",
        "clear_take_profit",
        "valid_stop_location",
        "controlling_side_known",
        "cvd_supports_direction",
        "market_bubbles_support_direction",
        "durable_defending_block",
        "defending_block_holds",
        "reload_confirmed",
        "defending_block_stable",
        "absorption_confirmed",
        "aggressive_side_failed",
        "loading_confirmed",
        "continuation_confirmed",
        "market_not_too_fast",
        "entry_after_reaction",
        "news_lockout",
    ]
    assert "[pass] Absorption confirmed" in "\n".join(result.render_lines())


def test_order_flow_plan_accepts_clean_short_continuation() -> None:
    """The same rules work for a short setup with an ask block above price."""
    result = evaluate_day_trading_plan(_short_absorption_snapshots(), _short_context())

    assert result.accepted is True
    assert _condition(result, "cvd_supports_direction").passed is True
    assert "sellers" in _condition(result, "cvd_supports_direction").message
    assert _condition(result, "aggressive_side_failed").passed is True


def test_detect_liquidity_blocks_tracks_reload_and_vanishing() -> None:
    """Detected blocks report reload count and whether the block vanished by the end."""
    snapshots = _long_absorption_snapshots(vanish_block=True)

    bid_blocks = [
        block
        for block in detect_liquidity_blocks(snapshots)
        if block.side == BookSide.BID and block.price == Decimal("100.00")
    ]

    assert len(bid_blocks) == 1
    assert bid_blocks[0].reload_count == 1
    assert bid_blocks[0].vanished_by_end is True


def test_first_ten_minutes_are_observe_only() -> None:
    """The first minutes of the session reject entries while bias is still forming."""
    early_context = _long_context(session_open_timestamp_ns=600_000_000_000)

    result = evaluate_day_trading_plan(_long_absorption_snapshots(), early_context)

    assert _condition(result, "opening_observation_complete").passed is False
    assert "observe bias first" in _condition(result, "opening_observation_complete").message


def test_missing_dol_and_target_rejects_entry() -> None:
    """No DOL and no TP target produce explicit no-entry reasons."""
    context = OrderFlowPlanContext(
        direction=TradeDirection.LONG,
        levels=StrategyLevels(psychological_interval=None),
        defended_level_price=Decimal("100.00"),
        stop_price=Decimal("99.25"),
        session_open_timestamp_ns=0,
    )

    result = evaluate_day_trading_plan(_long_absorption_snapshots(include_target_block=False), context)

    assert _condition(result, "clear_dol").passed is False
    assert _condition(result, "clear_take_profit").passed is False


def test_cvd_contradiction_and_no_directional_bubbles_reject_continuation() -> None:
    """Continuation fails when CVD and market orders do not support the trade direction."""
    result = evaluate_day_trading_plan(_long_absorption_snapshots(include_buy_follow_through=False), _long_context())

    assert _condition(result, "cvd_supports_direction").passed is False
    assert _condition(result, "market_bubbles_support_direction").passed is False
    assert _condition(result, "continuation_confirmed").passed is False


def test_missing_absorption_rejects_entry() -> None:
    """A block without aggressive opposite-side pressure is not absorption."""
    result = evaluate_day_trading_plan(_long_absorption_snapshots(include_absorption=False), _long_context())

    assert _condition(result, "absorption_confirmed").passed is False
    assert "No clear aggressive flow" in _condition(result, "absorption_confirmed").message


def test_vanished_or_moving_block_rejects_entry() -> None:
    """The checklist rejects blocks that vanish quickly or chase price."""
    vanished = evaluate_day_trading_plan(_long_absorption_snapshots(vanish_block=True), _long_context())
    moving = evaluate_day_trading_plan(_moving_block_snapshots(), _long_context())

    assert _condition(vanished, "defending_block_holds").passed is False
    assert "vanished" in _condition(vanished, "defending_block_holds").message
    assert _condition(moving, "defending_block_stable").passed is False
    assert "moving" in _condition(moving, "defending_block_stable").message


def test_market_too_fast_rejects_price_chasing() -> None:
    """Fast price movement rejects the setup before it becomes a chase."""
    thresholds = OrderFlowThresholds(max_price_velocity_ticks_per_second=Decimal("1"))

    result = evaluate_day_trading_plan(_long_absorption_snapshots(fast_reaction=True), _long_context(), thresholds)

    assert _condition(result, "market_not_too_fast").passed is False
    assert "too fast" in _condition(result, "market_not_too_fast").message


def test_helper_functions_report_dol_control_and_cvd() -> None:
    """Public helper functions expose DOL, control, and CVD for GUI/review use."""
    snapshots = _long_absorption_snapshots()

    dol = find_direction_of_liquidity(snapshots, _long_context())

    assert dol is not None
    assert dol.source == "manual level"
    assert identify_controlling_side(snapshots) == ControlSide.BUYERS
    assert calculate_cvd_delta(snapshots) == Decimal("-270")


def _condition(result: SetupEvaluationResult, key: str) -> SetupConditionResult:
    return next(condition for condition in result.conditions if condition.key == key)


def _long_context(
    *,
    session_open_timestamp_ns: int = 0,
) -> OrderFlowPlanContext:
    return OrderFlowPlanContext(
        direction=TradeDirection.LONG,
        levels=StrategyLevels(
            prior_day_high=Decimal("102.00"),
            overnight_low=Decimal("100.00"),
            manual_levels=(Decimal("101.50"),),
        ),
        defended_level_price=Decimal("100.00"),
        target_price=Decimal("102.00"),
        stop_price=Decimal("99.25"),
        session_open_timestamp_ns=session_open_timestamp_ns,
    )


def _short_context() -> OrderFlowPlanContext:
    return OrderFlowPlanContext(
        direction=TradeDirection.SHORT,
        levels=StrategyLevels(
            prior_day_low=Decimal("99.00"),
            overnight_high=Decimal("101.00"),
            manual_levels=(Decimal("99.50"),),
        ),
        defended_level_price=Decimal("101.00"),
        target_price=Decimal("99.00"),
        stop_price=Decimal("101.75"),
        session_open_timestamp_ns=0,
    )


def _long_absorption_snapshots(
    *,
    include_absorption: bool = True,
    include_buy_follow_through: bool = True,
    include_target_block: bool = True,
    vanish_block: bool = False,
    fast_reaction: bool = False,
) -> tuple[MarketState, ...]:
    state = MarketState()
    snapshots: list[MarketState] = []
    base = 601_000_000_000
    interval = 100_000_000 if fast_reaction else 1_000_000_000

    def apply(event: dict[str, object]) -> None:
        nonlocal state
        state = state.update(event)

    for event in (
        _depth(base, "bid", "100.00", "0", "120"),
        _depth(base, "bid", "99.75", "0", "70"),
        _depth(base, "ask", "100.25", "0", "100"),
    ):
        apply(event)
    if include_target_block:
        apply(_depth(base, "ask", "102.00", "0", "120"))
    snapshots.append(state)

    timestamp = base + interval
    if include_absorption:
        apply(_trade(timestamp, "100.00", "420", "sell", 1))
    apply(_depth(timestamp, "bid", "100.00", "120", "20"))
    snapshots.append(state)

    timestamp = base + 2 * interval
    apply(_depth(timestamp, "bid", "100.00", "20", "135"))
    snapshots.append(state)

    timestamp = base + 3 * interval
    apply(_depth(timestamp, "ask", "100.25", "100", "10"))
    snapshots.append(state)

    timestamp = base + 4 * interval
    apply(_depth(timestamp, "ask", "100.25", "10", "0"))
    apply(_depth(timestamp, "ask", "100.75", "0", "40"))
    apply(_depth(timestamp, "bid", "100.50", "0", "70"))
    apply(_depth(timestamp, "bid", "100.00", "135", "130"))
    snapshots.append(state)

    timestamp = base + 5 * interval
    if include_buy_follow_through:
        apply(_trade(timestamp, "100.75", "80", "buy", 2))
        apply(_depth(timestamp, "bid", "100.00", "130", "140"))
    else:
        apply(_trade(timestamp, "100.00", "80", "sell", 2))
    snapshots.append(state)

    timestamp = base + 6 * interval
    if include_buy_follow_through:
        apply(_trade(timestamp, "100.75", "70", "buy", 3))
    if vanish_block:
        apply(_depth(timestamp, "bid", "100.00", "140", "0"))
    snapshots.append(state)

    return tuple(snapshots)


def _short_absorption_snapshots() -> tuple[MarketState, ...]:
    state = MarketState()
    snapshots: list[MarketState] = []
    base = 601_000_000_000

    def apply(event: dict[str, object]) -> None:
        nonlocal state
        state = state.update(event)

    for event in (
        _depth(base, "ask", "101.00", "0", "120"),
        _depth(base, "ask", "101.25", "0", "70"),
        _depth(base, "bid", "100.75", "0", "100"),
        _depth(base, "bid", "99.00", "0", "120"),
    ):
        apply(event)
    snapshots.append(state)

    timestamp = base + 1_000_000_000
    apply(_trade(timestamp, "101.00", "420", "buy", 1))
    apply(_depth(timestamp, "ask", "101.00", "120", "20"))
    snapshots.append(state)

    timestamp = base + 2_000_000_000
    apply(_depth(timestamp, "ask", "101.00", "20", "135"))
    snapshots.append(state)

    timestamp = base + 3_000_000_000
    apply(_depth(timestamp, "bid", "100.75", "100", "10"))
    snapshots.append(state)

    timestamp = base + 4_000_000_000
    apply(_depth(timestamp, "bid", "100.75", "10", "0"))
    apply(_depth(timestamp, "bid", "100.25", "0", "40"))
    apply(_depth(timestamp, "ask", "100.50", "0", "70"))
    apply(_depth(timestamp, "ask", "101.00", "135", "130"))
    snapshots.append(state)

    timestamp = base + 5_000_000_000
    apply(_trade(timestamp, "100.25", "80", "sell", 2))
    apply(_depth(timestamp, "ask", "101.00", "130", "140"))
    snapshots.append(state)

    timestamp = base + 6_000_000_000
    apply(_trade(timestamp, "100.25", "70", "sell", 3))
    snapshots.append(state)

    return tuple(snapshots)


def _moving_block_snapshots() -> tuple[MarketState, ...]:
    state = MarketState()
    snapshots: list[MarketState] = []
    base = 601_000_000_000

    def apply(event: dict[str, object]) -> None:
        nonlocal state
        state = state.update(event)

    for event in (
        _depth(base, "bid", "100.00", "0", "120"),
        _depth(base, "ask", "100.25", "0", "100"),
        _depth(base, "ask", "102.00", "0", "120"),
    ):
        apply(event)
    snapshots.append(state)

    timestamp = base + 1_000_000_000
    apply(_trade(timestamp, "100.00", "420", "sell", 1))
    apply(_depth(timestamp, "bid", "100.00", "120", "20"))
    snapshots.append(state)

    timestamp = base + 2_000_000_000
    apply(_depth(timestamp, "bid", "99.25", "0", "135"))
    snapshots.append(state)

    timestamp = base + 3_000_000_000
    apply(_depth(timestamp, "bid", "99.25", "135", "20"))
    apply(_depth(timestamp, "bid", "100.00", "20", "130"))
    apply(_depth(timestamp, "ask", "100.25", "100", "10"))
    snapshots.append(state)

    timestamp = base + 4_000_000_000
    apply(_depth(timestamp, "ask", "100.25", "10", "0"))
    apply(_depth(timestamp, "ask", "100.75", "0", "40"))
    apply(_depth(timestamp, "bid", "100.50", "0", "70"))
    snapshots.append(state)

    timestamp = base + 5_000_000_000
    apply(_trade(timestamp, "100.75", "150", "buy", 2))
    snapshots.append(state)

    return tuple(snapshots)


def _depth(
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


def _trade(
    timestamp_ns: int,
    price: str,
    size: str,
    aggressor_side: str,
    sequence_id: int,
) -> dict[str, object]:
    return {
        "type": "trade",
        "timestamp_ns": timestamp_ns,
        "price": price,
        "size": size,
        "aggressor_side": aggressor_side,
        "instrument": "MNQ",
        "sequence_id": sequence_id,
    }
