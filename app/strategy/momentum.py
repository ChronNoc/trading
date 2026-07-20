"""Optional momentum-continuation setup evaluated beside the order-flow plan.

The canonical order-flow plan trades ABSORPTION at a defended liquidity
block. This optional setup trades CONTINUATION: an established directional
move (control + follow-through) entered on a contained pullback, with the
stop behind the pullback swing and the target at the same draw-on-liquidity
machinery the canonical plan uses.

It is OFF by default (``paper_momentum_setup_enabled`` in
config/production_config.yaml) and never replaces or modifies the canonical
deterministic strategy: shared gates (opening observation, news lockout,
market speed) are REUSED from the canonical checklist with identical
thresholds, re-keyed under the ``momentum_`` prefix so condition statistics
never mix between setups. Every check reports observed-vs-required values.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Sequence

from app.market.state import MarketState
from app.strategy.order_flow import (
    NANOSECONDS_PER_SECOND,
    OrderFlowPlanContext,
    OrderFlowThresholds,
    TradeDirection,
    _check_market_speed,
    _check_news_lockout,
    _check_opening_observation,
    _reference_price,
    calculate_cvd_delta,
    find_direction_of_liquidity,
    identify_controlling_side,
)
from app.strategy.order_flow import ControlSide
from app.strategy.setups import SetupConditionResult, SetupEvaluationResult

MOMENTUM_STRATEGY_VERSION = "momentum-v1"


@dataclass(frozen=True, slots=True)
class MomentumThresholds:
    """Deterministic momentum-specific thresholds; shared gates reuse canonical ones."""

    # The move must have made real net progress inside the analysis window.
    min_progress_ticks: Decimal = Decimal("8")
    # Entry happens on a pause, never at the extreme (that is chasing) and
    # never after the move already gave most of itself back.
    min_pullback_ticks: Decimal = Decimal("1")
    max_pullback_ticks: Decimal = Decimal("6")
    # The stop leans on the lowest/highest traded price of this trailing slice.
    pullback_window_seconds: Decimal = Decimal("30")

    def validate(self) -> None:
        """Reject impossible configurations before any evaluation runs."""
        if self.min_progress_ticks <= 0:
            raise ValueError("min_progress_ticks must be positive")
        if self.min_pullback_ticks < 0:
            raise ValueError("min_pullback_ticks must not be negative")
        if self.max_pullback_ticks < self.min_pullback_ticks:
            raise ValueError("max_pullback_ticks must be >= min_pullback_ticks")
        if self.pullback_window_seconds <= 0:
            raise ValueError("pullback_window_seconds must be positive")


@dataclass(frozen=True, slots=True)
class DerivedMomentumContext:
    """Everything the engine needs to trade an accepted momentum plan."""

    context: OrderFlowPlanContext
    progress_ticks: Decimal
    pullback_ticks: Decimal
    swing_price: Decimal | None


def derive_momentum_context(
    snapshots: Sequence[MarketState],
    direction: TradeDirection,
    level_tracker: object,
    thresholds: OrderFlowThresholds,
    momentum: MomentumThresholds,
    *,
    stop_buffer_points: Decimal,
    news_lockout_active: bool = False,
) -> DerivedMomentumContext:
    """Derive progress, pullback, swing stop, and DOL target from past data only."""
    if not snapshots:
        raise ValueError("snapshots must not be empty")
    momentum.validate()
    latest = snapshots[-1]
    levels = level_tracker.levels_at(latest.timestamp_ns)  # type: ignore[attr-defined]
    session_open = level_tracker.session_open_timestamp_ns(  # type: ignore[attr-defined]
        latest.timestamp_ns,
    )

    prices = [p for p in (_reference_price(s) for s in snapshots) if p is not None]
    first_price = prices[0] if prices else None
    last_price = prices[-1] if prices else None
    extreme = (max(prices) if direction == TradeDirection.LONG else min(prices)) if prices else None

    tick = thresholds.tick_size
    if first_price is None or last_price is None or extreme is None:
        progress_ticks = Decimal("0")
        pullback_ticks = Decimal("0")
    elif direction == TradeDirection.LONG:
        progress_ticks = (extreme - first_price) / tick
        pullback_ticks = (extreme - last_price) / tick
    else:
        progress_ticks = (first_price - extreme) / tick
        pullback_ticks = (last_price - extreme) / tick

    swing_price = _pullback_swing(snapshots, direction, momentum)
    if swing_price is None:
        stop_price = None
    elif direction == TradeDirection.LONG:
        stop_price = swing_price - stop_buffer_points
    else:
        stop_price = swing_price + stop_buffer_points

    base_context = OrderFlowPlanContext(
        direction=direction,
        levels=levels,
        stop_price=stop_price,
        session_open_timestamp_ns=session_open,
        news_lockout_active=news_lockout_active,
    )
    dol = find_direction_of_liquidity(snapshots, base_context, thresholds)
    context = replace(
        base_context,
        target_price=dol.target_price if dol is not None else None,
    )
    return DerivedMomentumContext(
        context=context,
        progress_ticks=progress_ticks,
        pullback_ticks=pullback_ticks,
        swing_price=swing_price,
    )


def evaluate_momentum_plan(
    snapshots: Sequence[MarketState],
    derived: DerivedMomentumContext,
    thresholds: OrderFlowThresholds,
    momentum: MomentumThresholds,
) -> SetupEvaluationResult:
    """Evaluate the deterministic momentum checklist over past snapshots only."""
    momentum.validate()
    context = derived.context
    direction = context.direction
    conditions: list[SetupConditionResult] = [
        _rekey(_check_opening_observation(snapshots, context, thresholds)),
        _check_control(snapshots, direction, thresholds),
        _check_follow_through(snapshots, direction, thresholds),
        _check_progress(derived, direction, momentum),
        _check_pullback(derived, momentum),
        _check_stop(derived, thresholds),
        _check_target(derived, thresholds),
        _rekey(_check_market_speed(snapshots, thresholds)),
        _rekey(_check_news_lockout(context)),
    ]
    return SetupEvaluationResult(
        setup_name=f"{MOMENTUM_STRATEGY_VERSION}_{direction.value}",
        conditions=tuple(conditions),
    )


def _rekey(result: SetupConditionResult) -> SetupConditionResult:
    """Shared canonical gates get a momentum_ key so statistics never mix."""
    return SetupConditionResult(
        key=f"momentum_{result.key}",
        passed=result.passed,
        message=result.message,
    )


def _check_control(
    snapshots: Sequence[MarketState],
    direction: TradeDirection,
    thresholds: OrderFlowThresholds,
) -> SetupConditionResult:
    expected = ControlSide.BUYERS if direction == TradeDirection.LONG else ControlSide.SELLERS
    observed = identify_controlling_side(snapshots, thresholds)
    cvd = calculate_cvd_delta(snapshots)
    if observed == expected:
        return SetupConditionResult(
            key="momentum_control",
            passed=True,
            message=f"{expected.value} control the move (window CVD {cvd:+})",
        )
    return SetupConditionResult(
        key="momentum_control",
        passed=False,
        message=(
            f"observed control {observed.value} (window CVD {cvd:+}) vs "
            f"required {expected.value} control"
        ),
    )


def _check_follow_through(
    snapshots: Sequence[MarketState],
    direction: TradeDirection,
    thresholds: OrderFlowThresholds,
) -> SetupConditionResult:
    if len(snapshots) < 2:
        return SetupConditionResult(
            key="momentum_follow_through",
            passed=False,
            message="not enough window history to measure follow-through",
        )
    first, last = snapshots[0], snapshots[-1]
    if direction == TradeDirection.LONG:
        observed = last.executed_buy_volume - first.executed_buy_volume
    else:
        observed = last.executed_sell_volume - first.executed_sell_volume
    required = thresholds.follow_through_volume_minimum
    if observed >= required:
        return SetupConditionResult(
            key="momentum_follow_through",
            passed=True,
            message=f"{observed} aggressive contracts with the move (requires >= {required})",
        )
    return SetupConditionResult(
        key="momentum_follow_through",
        passed=False,
        message=f"aggressive volume {observed} with the move vs required >= {required}",
    )


def _check_progress(
    derived: DerivedMomentumContext,
    direction: TradeDirection,
    momentum: MomentumThresholds,
) -> SetupConditionResult:
    observed = derived.progress_ticks
    required = momentum.min_progress_ticks
    if observed >= required:
        return SetupConditionResult(
            key="momentum_progress",
            passed=True,
            message=f"{observed:.0f} ticks of {direction.value} progress (requires >= {required})",
        )
    return SetupConditionResult(
        key="momentum_progress",
        passed=False,
        message=f"net progress {observed:.0f} ticks vs required >= {required} - no momentum to join",
    )


def _check_pullback(
    derived: DerivedMomentumContext,
    momentum: MomentumThresholds,
) -> SetupConditionResult:
    observed = derived.pullback_ticks
    low, high = momentum.min_pullback_ticks, momentum.max_pullback_ticks
    if observed < low:
        return SetupConditionResult(
            key="momentum_pullback",
            passed=False,
            message=f"pullback {observed:.0f} ticks vs required >= {low} - entering at the extreme is chasing",
        )
    if observed > high:
        return SetupConditionResult(
            key="momentum_pullback",
            passed=False,
            message=f"pullback {observed:.0f} ticks vs allowed <= {high} - the move is giving itself back",
        )
    return SetupConditionResult(
        key="momentum_pullback",
        passed=True,
        message=f"contained pullback of {observed:.0f} ticks (within {low}..{high})",
    )


def _check_stop(
    derived: DerivedMomentumContext,
    thresholds: OrderFlowThresholds,
) -> SetupConditionResult:
    context = derived.context
    stop = context.stop_price
    if stop is None or derived.swing_price is None:
        return SetupConditionResult(
            key="momentum_valid_stop",
            passed=False,
            message="no pullback swing to place a stop behind",
        )
    return SetupConditionResult(
        key="momentum_valid_stop",
        passed=True,
        message=f"stop {stop} behind the pullback swing {derived.swing_price}",
    )


def _check_target(
    derived: DerivedMomentumContext,
    thresholds: OrderFlowThresholds,
) -> SetupConditionResult:
    target = derived.context.target_price
    if target is None:
        return SetupConditionResult(
            key="momentum_clear_target",
            passed=False,
            message="no draw-on-liquidity target in the trade direction",
        )
    return SetupConditionResult(
        key="momentum_clear_target",
        passed=True,
        message=f"draw-on-liquidity target at {target}",
    )


def _pullback_swing(
    snapshots: Sequence[MarketState],
    direction: TradeDirection,
    momentum: MomentumThresholds,
) -> Decimal | None:
    """Lowest (LONG) / highest (SHORT) reference price of the trailing slice."""
    if not snapshots:
        return None
    cutoff_ns = snapshots[-1].timestamp_ns - int(
        momentum.pullback_window_seconds * NANOSECONDS_PER_SECOND
    )
    prices = [
        price
        for snapshot in snapshots
        if snapshot.timestamp_ns >= cutoff_ns
        and (price := _reference_price(snapshot)) is not None
    ]
    if not prices:
        return None
    return min(prices) if direction == TradeDirection.LONG else max(prices)
