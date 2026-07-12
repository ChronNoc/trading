"""Explainable order-flow checklist rules for discretionary MNQ day trading."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Sequence

from app.market.features import calculate_short_term_volatility
from app.market.state import DepthLevel, MarketState
from app.strategy.setups import SetupConditionResult, SetupEvaluationResult

ORDER_FLOW_PLAN_NAME = "ORDER FLOW PLAN"
NANOSECONDS_PER_SECOND = Decimal("1000000000")
NANOSECONDS_PER_MINUTE = 60_000_000_000


class TradeDirection(StrEnum):
    """Supported trade directions for the order-flow checklist."""

    LONG = "long"
    SHORT = "short"


class BookSide(StrEnum):
    """Visible order-book side."""

    BID = "bid"
    ASK = "ask"


class ControlSide(StrEnum):
    """Current dominant side inferred from price and CVD."""

    BUYERS = "buyers"
    SELLERS = "sellers"
    BALANCED = "balanced"


@dataclass(frozen=True, slots=True)
class StrategyLevels:
    """Known higher-timeframe levels used to find direction of liquidity."""

    prior_day_high: Decimal | None = None
    prior_day_low: Decimal | None = None
    overnight_high: Decimal | None = None
    overnight_low: Decimal | None = None
    manual_levels: tuple[Decimal, ...] = ()
    psychological_interval: Decimal | None = Decimal("100")


@dataclass(frozen=True, slots=True)
class OrderFlowPlanContext:
    """Inputs that cannot be inferred safely from the current market-state window."""

    direction: TradeDirection
    levels: StrategyLevels
    defended_level_price: Decimal | None = None
    target_price: Decimal | None = None
    stop_price: Decimal | None = None
    session_open_timestamp_ns: int | None = None
    news_lockout_active: bool = False


@dataclass(frozen=True, slots=True)
class OrderFlowThresholds:
    """Tunable thresholds for the handwritten order-flow checklist."""

    tick_size: Decimal = Decimal("0.25")
    large_block_minimum: Decimal = Decimal("90")
    durable_block_min_snapshots: int = 3
    min_reload_count: int = 1
    absorption_volume_minimum: Decimal = Decimal("400")
    follow_through_volume_minimum: Decimal = Decimal("50")
    loading_liquidity_minimum: Decimal = Decimal("50")
    cvd_support_minimum: Decimal = Decimal("25")
    absorption_max_progress_ticks: Decimal = Decimal("3")
    min_reaction_ticks: Decimal = Decimal("1")
    min_continuation_ticks: Decimal = Decimal("2")
    minimum_target_ticks: Decimal = Decimal("4")
    minimum_stop_ticks: Decimal = Decimal("2")
    max_spread_ticks: Decimal = Decimal("4")
    max_volatility_ticks: Decimal = Decimal("12")
    max_price_velocity_ticks_per_second: Decimal = Decimal("8")
    first_observation_minutes: int = 10
    min_reaction_snapshots: int = 3
    max_dol_distance_ticks: Decimal = Decimal("120")
    block_move_tolerance_ticks: Decimal = Decimal("2")


@dataclass(frozen=True, slots=True)
class LiquidityBlock:
    """A visible large block detected at one side and price."""

    side: BookSide
    price: Decimal
    peak_size: Decimal
    latest_size: Decimal
    observations: int
    first_seen_timestamp_ns: int
    last_seen_timestamp_ns: int
    reload_count: int
    vanished_by_end: bool

    def is_durable(self, thresholds: OrderFlowThresholds) -> bool:
        """Return whether the block persisted long enough to be trusted."""
        return self.observations >= thresholds.durable_block_min_snapshots


@dataclass(frozen=True, slots=True)
class DirectionOfLiquidity:
    """Nearest visible or known liquidity target in the intended direction."""

    direction: TradeDirection
    target_price: Decimal
    source: str
    distance_ticks: Decimal


def detect_liquidity_blocks(
    snapshots: Sequence[MarketState],
    thresholds: OrderFlowThresholds = OrderFlowThresholds(),
) -> tuple[LiquidityBlock, ...]:
    """Detect large visible bid/ask blocks and their durability/reload behavior."""
    _validate_thresholds(thresholds)
    if not snapshots:
        return ()

    candidates: set[tuple[BookSide, Decimal]] = set()
    for snapshot in snapshots:
        for side in (BookSide.BID, BookSide.ASK):
            for level in _depth_for_side(snapshot, side):
                if level.size >= thresholds.large_block_minimum:
                    candidates.add((side, level.price))

    blocks = tuple(
        _build_liquidity_block(snapshots, side, price, thresholds)
        for side, price in sorted(candidates, key=lambda item: (item[0].value, item[1]))
    )
    return tuple(block for block in blocks if block.peak_size >= thresholds.large_block_minimum)


def calculate_cvd_delta(snapshots: Sequence[MarketState]) -> Decimal:
    """Return cumulative-volume-delta change across the supplied snapshots."""
    if len(snapshots) < 2:
        return Decimal("0")
    first = snapshots[0]
    last = snapshots[-1]
    first_cvd = first.executed_buy_volume - first.executed_sell_volume
    last_cvd = last.executed_buy_volume - last.executed_sell_volume
    return last_cvd - first_cvd


def identify_controlling_side(
    snapshots: Sequence[MarketState],
    thresholds: OrderFlowThresholds = OrderFlowThresholds(),
) -> ControlSide:
    """Infer whether buyers, sellers, or neither side currently controls the tape."""
    _validate_thresholds(thresholds)
    tail = _tail_snapshots(snapshots)
    if len(tail) < 2:
        return ControlSide.BALANCED

    first_price = _reference_price(tail[0])
    last_price = _reference_price(tail[-1])
    if first_price is None or last_price is None:
        return ControlSide.BALANCED

    price_delta = last_price - first_price
    cvd_delta = calculate_cvd_delta(tail)
    if cvd_delta >= thresholds.cvd_support_minimum and price_delta >= Decimal("0"):
        return ControlSide.BUYERS
    if cvd_delta <= -thresholds.cvd_support_minimum and price_delta <= Decimal("0"):
        return ControlSide.SELLERS
    return ControlSide.BALANCED


def find_direction_of_liquidity(
    snapshots: Sequence[MarketState],
    context: OrderFlowPlanContext,
    thresholds: OrderFlowThresholds = OrderFlowThresholds(),
) -> DirectionOfLiquidity | None:
    """Find the nearest plausible draw-on-liquidity target in the trade direction."""
    _validate_thresholds(thresholds)
    if not snapshots:
        return None

    current_price = _reference_price(snapshots[-1])
    if current_price is None:
        return None

    candidates: list[tuple[Decimal, str]] = []
    if context.target_price is not None:
        candidates.append((context.target_price, "explicit target"))
    candidates.extend(_level_candidates(current_price, context.direction, context.levels))
    candidates.extend(_block_target_candidates(snapshots[-1], context.direction, thresholds))

    if context.levels.psychological_interval is not None and context.levels.psychological_interval > Decimal("0"):
        psych = _psychological_level(
            current_price,
            context.direction,
            context.levels.psychological_interval,
        )
        candidates.append((psych, "psychological number"))

    directed = tuple(
        (price, source, _directed_distance_ticks(current_price, price, context.direction, thresholds.tick_size))
        for price, source in candidates
    )
    valid = tuple(
        (price, source, distance_ticks)
        for price, source, distance_ticks in directed
        if distance_ticks is not None
        and distance_ticks > Decimal("0")
        and distance_ticks <= thresholds.max_dol_distance_ticks
    )
    if not valid:
        return None

    target_price, source, distance_ticks = min(valid, key=lambda item: item[2])
    return DirectionOfLiquidity(
        direction=context.direction,
        target_price=target_price,
        source=source,
        distance_ticks=distance_ticks,
    )


def evaluate_day_trading_plan(
    snapshots: Sequence[MarketState],
    context: OrderFlowPlanContext,
    thresholds: OrderFlowThresholds = OrderFlowThresholds(),
) -> SetupEvaluationResult:
    """Evaluate the full day-trading order-flow checklist as explicit conditions."""
    _validate_thresholds(thresholds)
    if not snapshots:
        raise ValueError("snapshots must contain at least one MarketState")

    snapshot_tuple = tuple(snapshots)
    defending_block = _select_defending_block(snapshot_tuple, context, thresholds)
    dol = find_direction_of_liquidity(snapshot_tuple, context, thresholds)
    controlling_side = identify_controlling_side(snapshot_tuple, thresholds)
    reaction_ticks = _reaction_ticks(snapshot_tuple[-1], defending_block, context.direction, thresholds)
    loading_liquidity = _loading_liquidity(snapshot_tuple, context.direction)
    directional_volume = _aggressive_volume_delta(snapshot_tuple, context.direction)

    conditions = (
        _check_opening_observation(snapshot_tuple, context, thresholds),
        _check_clear_dol(dol),
        _check_clear_target(snapshot_tuple[-1], context, dol, thresholds),
        _check_stop_location(context, defending_block, thresholds),
        _check_controlling_side(controlling_side),
        _check_cvd_support(snapshot_tuple, context.direction, thresholds),
        _check_directional_bubbles(directional_volume, context.direction, thresholds),
        _check_durable_block(defending_block, context.direction, thresholds),
        _check_block_holds(defending_block, context.direction, thresholds),
        _check_reload(defending_block, context.direction, thresholds),
        _check_block_stability(snapshot_tuple, context, thresholds),
        _check_absorption(snapshot_tuple, context.direction, defending_block, thresholds),
        _check_aggressive_side_failed(snapshot_tuple, context.direction, defending_block, thresholds),
        _check_loading(loading_liquidity, context.direction, thresholds),
        _check_continuation(
            controlling_side=controlling_side,
            directional_volume=directional_volume,
            reaction_ticks=reaction_ticks,
            loading_liquidity=loading_liquidity,
            direction=context.direction,
            thresholds=thresholds,
        ),
        _check_market_speed(snapshot_tuple, thresholds),
        _check_entry_after_reaction(snapshot_tuple, reaction_ticks, thresholds),
        _check_news_lockout(context),
    )
    return SetupEvaluationResult(setup_name=ORDER_FLOW_PLAN_NAME, conditions=conditions)


def _build_liquidity_block(
    snapshots: Sequence[MarketState],
    side: BookSide,
    price: Decimal,
    thresholds: OrderFlowThresholds,
) -> LiquidityBlock:
    peak_size = Decimal("0")
    observations = 0
    first_seen_timestamp_ns: int | None = None
    last_seen_timestamp_ns: int | None = None
    reload_count = 0
    was_at_or_above = _size_at_price(snapshots[0], side, price) >= thresholds.large_block_minimum
    waiting_for_reload = False

    for snapshot in snapshots:
        size = _size_at_price(snapshot, side, price)
        peak_size = max(peak_size, size)
        is_at_or_above = size >= thresholds.large_block_minimum
        if is_at_or_above:
            observations += 1
            first_seen_timestamp_ns = (
                snapshot.timestamp_ns if first_seen_timestamp_ns is None else first_seen_timestamp_ns
            )
            last_seen_timestamp_ns = snapshot.timestamp_ns
        if was_at_or_above and not is_at_or_above:
            waiting_for_reload = True
            was_at_or_above = False
        elif waiting_for_reload and is_at_or_above:
            reload_count += 1
            waiting_for_reload = False
            was_at_or_above = True
        elif is_at_or_above:
            was_at_or_above = True

    latest_size = _size_at_price(snapshots[-1], side, price)
    return LiquidityBlock(
        side=side,
        price=price,
        peak_size=peak_size,
        latest_size=latest_size,
        observations=observations,
        first_seen_timestamp_ns=first_seen_timestamp_ns or snapshots[0].timestamp_ns,
        last_seen_timestamp_ns=last_seen_timestamp_ns or snapshots[0].timestamp_ns,
        reload_count=reload_count,
        vanished_by_end=latest_size < thresholds.large_block_minimum,
    )


def _select_defending_block(
    snapshots: Sequence[MarketState],
    context: OrderFlowPlanContext,
    thresholds: OrderFlowThresholds,
) -> LiquidityBlock | None:
    blocks = detect_liquidity_blocks(snapshots, thresholds)
    side = _defending_side(context.direction)
    candidates = tuple(block for block in blocks if block.side == side)
    if not candidates:
        return None

    if context.defended_level_price is not None:
        max_distance = thresholds.block_move_tolerance_ticks * thresholds.tick_size
        near_defended = tuple(
            block for block in candidates if abs(block.price - context.defended_level_price) <= max_distance
        )
        if near_defended:
            return max(near_defended, key=lambda block: (block.observations, block.peak_size))

    current_price = _reference_price(snapshots[-1])
    if current_price is not None:
        directional = tuple(
            block
            for block in candidates
            if (
                context.direction == TradeDirection.LONG
                and block.price <= current_price
                or context.direction == TradeDirection.SHORT
                and block.price >= current_price
            )
        )
        if directional:
            return max(directional, key=lambda block: (block.latest_size, block.observations, block.peak_size))

    return max(candidates, key=lambda block: (block.latest_size, block.observations, block.peak_size))


def _check_opening_observation(
    snapshots: Sequence[MarketState],
    context: OrderFlowPlanContext,
    thresholds: OrderFlowThresholds,
) -> SetupConditionResult:
    if context.session_open_timestamp_ns is None:
        return _pass("opening_observation_complete", "Opening observation guard not configured")

    required_ns = context.session_open_timestamp_ns + thresholds.first_observation_minutes * NANOSECONDS_PER_MINUTE
    if snapshots[-1].timestamp_ns < required_ns:
        return _fail(
            "opening_observation_complete",
            f"Still inside first {thresholds.first_observation_minutes} minutes; observe bias first",
        )
    return _pass("opening_observation_complete", "Opening observation period is complete")


def _check_clear_dol(dol: DirectionOfLiquidity | None) -> SetupConditionResult:
    if dol is None:
        return _fail("clear_dol", "No clear direction of liquidity")
    return _pass(
        "clear_dol",
        f"DOL points {dol.direction.value} toward {dol.source} at {dol.target_price}",
    )


def _check_clear_target(
    latest: MarketState,
    context: OrderFlowPlanContext,
    dol: DirectionOfLiquidity | None,
    thresholds: OrderFlowThresholds,
) -> SetupConditionResult:
    current_price = _reference_price(latest)
    target_price = context.target_price or (dol.target_price if dol is not None else None)
    if current_price is None or target_price is None:
        return _fail("clear_take_profit", "No clear TP target")

    distance_ticks = _directed_distance_ticks(current_price, target_price, context.direction, thresholds.tick_size)
    if distance_ticks is None or distance_ticks < thresholds.minimum_target_ticks:
        return _fail("clear_take_profit", "TP is too close or not in the trade direction")
    return _pass("clear_take_profit", f"TP has {distance_ticks} ticks of room")


def _check_stop_location(
    context: OrderFlowPlanContext,
    defending_block: LiquidityBlock | None,
    thresholds: OrderFlowThresholds,
) -> SetupConditionResult:
    if context.stop_price is None or defending_block is None:
        return _fail("valid_stop_location", "No good stop location")

    if context.direction == TradeDirection.LONG:
        distance_ticks = (defending_block.price - context.stop_price) / thresholds.tick_size
    else:
        distance_ticks = (context.stop_price - defending_block.price) / thresholds.tick_size

    if distance_ticks < thresholds.minimum_stop_ticks:
        return _fail("valid_stop_location", "Stop is too close to the defended block")
    return _pass("valid_stop_location", f"Stop sits {distance_ticks} ticks beyond the defended block")


def _check_controlling_side(controlling_side: ControlSide) -> SetupConditionResult:
    if controlling_side == ControlSide.BALANCED:
        return _fail("controlling_side_known", "No clear controlling side")
    return _pass("controlling_side_known", f"{controlling_side.value.title()} control the latest flow")


def _check_cvd_support(
    snapshots: Sequence[MarketState],
    direction: TradeDirection,
    thresholds: OrderFlowThresholds,
) -> SetupConditionResult:
    cvd_delta = calculate_cvd_delta(_tail_snapshots(snapshots))
    if direction == TradeDirection.LONG and cvd_delta >= thresholds.cvd_support_minimum:
        return _pass("cvd_supports_direction", f"CVD supports buyers by {cvd_delta}")
    if direction == TradeDirection.SHORT and cvd_delta <= -thresholds.cvd_support_minimum:
        return _pass("cvd_supports_direction", f"CVD supports sellers by {cvd_delta}")
    return _fail("cvd_supports_direction", "CVD contradicts or does not support the direction")


def _check_directional_bubbles(
    directional_volume: Decimal,
    direction: TradeDirection,
    thresholds: OrderFlowThresholds,
) -> SetupConditionResult:
    if directional_volume < thresholds.follow_through_volume_minimum:
        return _fail(
            "market_bubbles_support_direction",
            f"Not enough market bubbles in the {direction.value} direction",
        )
    return _pass(
        "market_bubbles_support_direction",
        f"{directional_volume} aggressive contracts printed in the {direction.value} direction",
    )


def _check_durable_block(
    block: LiquidityBlock | None,
    direction: TradeDirection,
    thresholds: OrderFlowThresholds,
) -> SetupConditionResult:
    if block is None:
        return _fail("durable_defending_block", f"No strong {'bid' if direction == TradeDirection.LONG else 'ask'} block")
    if not block.is_durable(thresholds):
        return _fail("durable_defending_block", "The defending block appeared but did not persist")
    return _pass("durable_defending_block", f"Defending block held at {block.price} across {block.observations} snapshots")


def _check_block_holds(
    block: LiquidityBlock | None,
    direction: TradeDirection,
    thresholds: OrderFlowThresholds,
) -> SetupConditionResult:
    if block is None:
        return _fail("defending_block_holds", "No defending block is present")
    if block.vanished_by_end:
        return _fail("defending_block_holds", "The block appeared and vanished too fast")
    if block.latest_size < thresholds.large_block_minimum:
        return _fail("defending_block_holds", "The defending block is no longer large enough")
    side_text = "buying" if direction == TradeDirection.LONG else "selling"
    return _pass("defending_block_holds", f"{side_text.title()} block is still defending {block.price}")


def _check_reload(
    block: LiquidityBlock | None,
    direction: TradeDirection,
    thresholds: OrderFlowThresholds,
) -> SetupConditionResult:
    if block is None:
        return _fail("reload_confirmed", "No block available to evaluate reload")
    if block.reload_count < thresholds.min_reload_count:
        return _fail("reload_confirmed", "The defending block did not reload")
    side_text = "bid" if direction == TradeDirection.LONG else "ask"
    return _pass("reload_confirmed", f"Defending {side_text} reloaded {block.reload_count} times")


def _check_block_stability(
    snapshots: Sequence[MarketState],
    context: OrderFlowPlanContext,
    thresholds: OrderFlowThresholds,
) -> SetupConditionResult:
    side = _defending_side(context.direction)
    dominant_prices = _dominant_block_prices(snapshots, side, thresholds)
    if len(dominant_prices) < thresholds.durable_block_min_snapshots:
        return _fail("defending_block_stable", "No stable defending block sequence")

    movement_ticks = (max(dominant_prices) - min(dominant_prices)) / thresholds.tick_size
    if movement_ticks > thresholds.block_move_tolerance_ticks:
        return _fail("defending_block_stable", "The block is moving instead of holding")
    return _pass("defending_block_stable", "Defending block is stable, not chasing price")


def _check_absorption(
    snapshots: Sequence[MarketState],
    direction: TradeDirection,
    block: LiquidityBlock | None,
    thresholds: OrderFlowThresholds,
) -> SetupConditionResult:
    if block is None:
        return _fail("absorption_confirmed", "No block available to absorb flow")

    opposite_volume = _opposite_aggressive_volume_delta(snapshots, direction)
    progress_ticks = _progress_through_block_ticks(snapshots, block, direction, thresholds)
    if opposite_volume < thresholds.absorption_volume_minimum:
        return _fail("absorption_confirmed", "No clear aggressive flow hitting the block")
    if progress_ticks > thresholds.absorption_max_progress_ticks:
        return _fail("absorption_confirmed", "Aggressive flow broke too far through the block")
    return _pass("absorption_confirmed", f"Absorption confirmed: {opposite_volume} contracts hit the block")


def _check_aggressive_side_failed(
    snapshots: Sequence[MarketState],
    direction: TradeDirection,
    block: LiquidityBlock | None,
    thresholds: OrderFlowThresholds,
) -> SetupConditionResult:
    if block is None:
        return _fail("aggressive_side_failed", "No defended block to judge aggressive failure")

    progress_ticks = _progress_through_block_ticks(snapshots, block, direction, thresholds)
    if progress_ticks > thresholds.absorption_max_progress_ticks:
        return _fail("aggressive_side_failed", "The aggressive side broke the defended block")
    failed_side = "sellers" if direction == TradeDirection.LONG else "buyers"
    return _pass("aggressive_side_failed", f"Aggressive {failed_side} failed to break the block")


def _check_loading(
    loading_liquidity: Decimal,
    direction: TradeDirection,
    thresholds: OrderFlowThresholds,
) -> SetupConditionResult:
    if loading_liquidity < thresholds.loading_liquidity_minimum:
        return _fail("loading_confirmed", "No fresh blocks loading in the trade direction")
    return _pass("loading_confirmed", f"{loading_liquidity} contracts loaded behind the {direction.value} idea")


def _check_continuation(
    *,
    controlling_side: ControlSide,
    directional_volume: Decimal,
    reaction_ticks: Decimal,
    loading_liquidity: Decimal,
    direction: TradeDirection,
    thresholds: OrderFlowThresholds,
) -> SetupConditionResult:
    expected_control = ControlSide.BUYERS if direction == TradeDirection.LONG else ControlSide.SELLERS
    if controlling_side != expected_control:
        return _fail("continuation_confirmed", "Continuation is missing because control is unclear")
    if directional_volume < thresholds.follow_through_volume_minimum:
        return _fail("continuation_confirmed", "Continuation is missing directional market orders")
    if loading_liquidity < thresholds.loading_liquidity_minimum:
        return _fail("continuation_confirmed", "Continuation is missing reloaded defending liquidity")
    if reaction_ticks < thresholds.min_continuation_ticks:
        return _fail("continuation_confirmed", "Continuation has not moved far enough from absorption")
    return _pass("continuation_confirmed", "Continuation confirmed after absorption")


def _check_market_speed(
    snapshots: Sequence[MarketState],
    thresholds: OrderFlowThresholds,
) -> SetupConditionResult:
    latest_spread_ticks = (
        (snapshots[-1].spread or Decimal("0")) / thresholds.tick_size
        if snapshots[-1].spread is not None
        else Decimal("0")
    )
    volatility_ticks = calculate_short_term_volatility(snapshots) / thresholds.tick_size
    velocity_ticks = _price_velocity_ticks_per_second(snapshots, thresholds)
    if latest_spread_ticks > thresholds.max_spread_ticks:
        return _fail("market_not_too_fast", "Spread is too wide")
    if volatility_ticks > thresholds.max_volatility_ticks:
        return _fail("market_not_too_fast", "Market is too volatile; do not chase")
    if velocity_ticks > thresholds.max_price_velocity_ticks_per_second:
        return _fail("market_not_too_fast", "Market is moving too fast; do not chase")
    return _pass("market_not_too_fast", "Market speed is controlled enough to evaluate")


def _check_entry_after_reaction(
    snapshots: Sequence[MarketState],
    reaction_ticks: Decimal,
    thresholds: OrderFlowThresholds,
) -> SetupConditionResult:
    if len(snapshots) < thresholds.min_reaction_snapshots:
        return _fail("entry_after_reaction", "Entry is too early; wait for reaction")
    if reaction_ticks < thresholds.min_reaction_ticks:
        return _fail("entry_after_reaction", "Entry is on the first touch without enough reaction")
    return _pass("entry_after_reaction", f"Reaction is visible by {reaction_ticks} ticks")


def _check_news_lockout(context: OrderFlowPlanContext) -> SetupConditionResult:
    if context.news_lockout_active:
        return _fail("news_lockout", "News lockout is active")
    return _pass("news_lockout", "Not in a news lockout window")


def _level_candidates(
    current_price: Decimal,
    direction: TradeDirection,
    levels: StrategyLevels,
) -> tuple[tuple[Decimal, str], ...]:
    named_levels = (
        (levels.prior_day_high, "prior day high"),
        (levels.prior_day_low, "prior day low"),
        (levels.overnight_high, "overnight high"),
        (levels.overnight_low, "overnight low"),
        *tuple((level, "manual level") for level in levels.manual_levels),
    )
    if direction == TradeDirection.LONG:
        return tuple((price, source) for price, source in named_levels if price is not None and price > current_price)
    return tuple((price, source) for price, source in named_levels if price is not None and price < current_price)


def _block_target_candidates(
    latest: MarketState,
    direction: TradeDirection,
    thresholds: OrderFlowThresholds,
) -> tuple[tuple[Decimal, str], ...]:
    target_side = BookSide.ASK if direction == TradeDirection.LONG else BookSide.BID
    return tuple(
        (level.price, f"{target_side.value} liquidity block")
        for level in _depth_for_side(latest, target_side)
        if level.size >= thresholds.large_block_minimum
    )


def _psychological_level(
    current_price: Decimal,
    direction: TradeDirection,
    interval: Decimal,
) -> Decimal:
    rounded = (current_price / interval).to_integral_value(rounding=ROUND_FLOOR) * interval
    if direction == TradeDirection.LONG:
        return rounded + interval if rounded <= current_price else rounded
    return rounded if rounded < current_price else rounded - interval


def _directed_distance_ticks(
    current_price: Decimal,
    target_price: Decimal,
    direction: TradeDirection,
    tick_size: Decimal,
) -> Decimal | None:
    raw_distance = target_price - current_price if direction == TradeDirection.LONG else current_price - target_price
    if raw_distance <= Decimal("0"):
        return None
    return raw_distance / tick_size


def _reaction_ticks(
    latest: MarketState,
    block: LiquidityBlock | None,
    direction: TradeDirection,
    thresholds: OrderFlowThresholds,
) -> Decimal:
    current_price = _reference_price(latest)
    if current_price is None or block is None:
        return Decimal("0")
    if direction == TradeDirection.LONG:
        return max((current_price - block.price) / thresholds.tick_size, Decimal("0"))
    return max((block.price - current_price) / thresholds.tick_size, Decimal("0"))


def _progress_through_block_ticks(
    snapshots: Sequence[MarketState],
    block: LiquidityBlock,
    direction: TradeDirection,
    thresholds: OrderFlowThresholds,
) -> Decimal:
    prices = tuple(price for price in (_reference_price(snapshot) for snapshot in snapshots) if price is not None)
    if not prices:
        return Decimal("0")
    if direction == TradeDirection.LONG:
        lowest = min(prices)
        return max((block.price - lowest) / thresholds.tick_size, Decimal("0"))
    highest = max(prices)
    return max((highest - block.price) / thresholds.tick_size, Decimal("0"))


def _aggressive_volume_delta(snapshots: Sequence[MarketState], direction: TradeDirection) -> Decimal:
    tail = _tail_snapshots(snapshots)
    if len(tail) < 2:
        return Decimal("0")
    if direction == TradeDirection.LONG:
        return max(tail[-1].executed_buy_volume - tail[0].executed_buy_volume, Decimal("0"))
    return max(tail[-1].executed_sell_volume - tail[0].executed_sell_volume, Decimal("0"))


def _opposite_aggressive_volume_delta(snapshots: Sequence[MarketState], direction: TradeDirection) -> Decimal:
    if len(snapshots) < 2:
        return Decimal("0")
    if direction == TradeDirection.LONG:
        return max(snapshots[-1].executed_sell_volume - snapshots[0].executed_sell_volume, Decimal("0"))
    return max(snapshots[-1].executed_buy_volume - snapshots[0].executed_buy_volume, Decimal("0"))


def _loading_liquidity(snapshots: Sequence[MarketState], direction: TradeDirection) -> Decimal:
    tail = _tail_snapshots(snapshots)
    if len(tail) < 2:
        return Decimal("0")
    side = BookSide.BID if direction == TradeDirection.LONG else BookSide.ASK
    previous = {level.price: level.size for level in _depth_for_side(tail[0], side)}
    current = {level.price: level.size for level in _depth_for_side(tail[-1], side)}
    added = Decimal("0")
    for price in previous.keys() | current.keys():
        delta = current.get(price, Decimal("0")) - previous.get(price, Decimal("0"))
        if delta > Decimal("0"):
            added += delta
    return added


def _dominant_block_prices(
    snapshots: Sequence[MarketState],
    side: BookSide,
    thresholds: OrderFlowThresholds,
) -> tuple[Decimal, ...]:
    prices: list[Decimal] = []
    for snapshot in snapshots:
        large_levels = tuple(
            level for level in _depth_for_side(snapshot, side) if level.size >= thresholds.large_block_minimum
        )
        if not large_levels:
            continue
        if side == BookSide.BID:
            prices.append(max(large_levels, key=lambda level: level.price).price)
        else:
            prices.append(min(large_levels, key=lambda level: level.price).price)
    return tuple(prices)


def _price_velocity_ticks_per_second(
    snapshots: Sequence[MarketState],
    thresholds: OrderFlowThresholds,
) -> Decimal:
    if len(snapshots) < 2:
        return Decimal("0")
    first_price = _reference_price(snapshots[0])
    last_price = _reference_price(snapshots[-1])
    if first_price is None or last_price is None:
        return Decimal("0")
    elapsed_ns = Decimal(snapshots[-1].timestamp_ns - snapshots[0].timestamp_ns)
    if elapsed_ns <= Decimal("0"):
        return Decimal("0")
    elapsed_seconds = elapsed_ns / NANOSECONDS_PER_SECOND
    return abs(last_price - first_price) / thresholds.tick_size / elapsed_seconds


def _tail_snapshots(snapshots: Sequence[MarketState]) -> tuple[MarketState, ...]:
    if len(snapshots) <= 2:
        return tuple(snapshots)
    return tuple(snapshots[len(snapshots) // 2 :])


def _defending_side(direction: TradeDirection) -> BookSide:
    return BookSide.BID if direction == TradeDirection.LONG else BookSide.ASK


def _depth_for_side(snapshot: MarketState, side: BookSide) -> tuple[DepthLevel, ...]:
    return snapshot.bid_depth if side == BookSide.BID else snapshot.ask_depth


def _size_at_price(snapshot: MarketState, side: BookSide, price: Decimal) -> Decimal:
    for level in _depth_for_side(snapshot, side):
        if level.price == price:
            return level.size
    return Decimal("0")


def _reference_price(market_state: MarketState) -> Decimal | None:
    if market_state.mid_price is not None:
        return market_state.mid_price
    if market_state.best_bid is not None:
        return market_state.best_bid
    if market_state.best_ask is not None:
        return market_state.best_ask
    return None


def _validate_thresholds(thresholds: OrderFlowThresholds) -> None:
    if thresholds.tick_size <= Decimal("0"):
        raise ValueError("tick_size must be greater than zero")
    if thresholds.large_block_minimum <= Decimal("0"):
        raise ValueError("large_block_minimum must be greater than zero")
    if thresholds.durable_block_min_snapshots < 1:
        raise ValueError("durable_block_min_snapshots must be at least one")


def _pass(key: str, message: str) -> SetupConditionResult:
    return SetupConditionResult(key=key, passed=True, message=message)


def _fail(key: str, message: str) -> SetupConditionResult:
    return SetupConditionResult(key=key, passed=False, message=message)
