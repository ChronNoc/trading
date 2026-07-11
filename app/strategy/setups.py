"""Explainable strategy setup evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from app.market.features import calculate_bid_reload_count
from app.market.state import MarketState
from app.strategy.spec import ImportantLevel, ParameterValue, StrategySpec, UNRESOLVED

LONG_SETUP_NAME = "LONG SETUP"
DEFAULT_TICK_SIZE = Decimal("0.25")


@dataclass(frozen=True, slots=True)
class LongAbsorptionReclaimContext:
    """Context required to evaluate the long absorption-reclaim setup."""

    important_level: ImportantLevel
    important_level_price: Decimal
    tick_size: Decimal = DEFAULT_TICK_SIZE
    bid_reload_level: int = 1


@dataclass(frozen=True, slots=True)
class SetupConditionResult:
    """Pass/fail result for one explainable setup sub-condition."""

    key: str
    passed: bool
    message: str

    @property
    def status(self) -> str:
        """Return a display-friendly condition status."""
        return "pass" if self.passed else "fail"


@dataclass(frozen=True, slots=True)
class SetupEvaluationResult:
    """Explainable setup evaluation result for GUI display and logging."""

    setup_name: str
    conditions: tuple[SetupConditionResult, ...]

    @property
    def accepted(self) -> bool:
        """Return true only when every sub-condition passed."""
        return all(condition.passed for condition in self.conditions)

    @property
    def status(self) -> str:
        """Return ACCEPTED or REJECTED for display."""
        return "ACCEPTED" if self.accepted else "REJECTED"

    def render_lines(self) -> tuple[str, ...]:
        """Render the setup result as GUI-friendly status lines."""
        return (
            f"{self.setup_name}: {self.status}",
            *(
                f"[{condition.status}] {condition.message}"
                for condition in self.conditions
            ),
        )


def evaluate_long_absorption_reclaim_setup(
    market_state: MarketState,
    feature_window: Sequence[MarketState],
    strategy_spec: StrategySpec,
    context: LongAbsorptionReclaimContext,
) -> SetupEvaluationResult:
    """Evaluate the long absorption-reclaim setup with every condition listed."""
    snapshots = _snapshots_with_current(feature_window, market_state)
    aggressive_sell_volume = _aggressive_sell_volume(snapshots)
    downward_progress_ticks = _downward_progress_ticks(
        snapshots=snapshots,
        important_level_price=context.important_level_price,
        tick_size=context.tick_size,
    )
    ask_cancellation_ratio = _ask_cancellation_ratio(snapshots)

    conditions = (
        _check_at_important_level(strategy_spec, context, snapshots),
        _check_aggressive_sell_volume(
            observed_volume=aggressive_sell_volume,
            threshold=strategy_spec.entry_setup.aggressive_volume_minimum,
        ),
        _check_downward_progress(
            observed_ticks=downward_progress_ticks,
            threshold=strategy_spec.entry_setup.maximum_price_progress_ticks,
        ),
        _check_bid_reload_count(
            snapshots=snapshots,
            context=context,
            liquidity_threshold=strategy_spec.entry_setup.liquidity_minimum,
            min_reload_count=strategy_spec.entry_setup.min_reload_count,
        ),
        _check_ask_cancellation_ratio(
            observed_ratio=ask_cancellation_ratio,
            threshold=strategy_spec.entry_setup.min_ask_pull_ratio,
        ),
        _check_reclaimed_level(
            market_state=market_state,
            context=context,
            reclaim_ticks=strategy_spec.entry_setup.reclaim_ticks,
        ),
        _check_news_lockout(market_state),
    )

    return SetupEvaluationResult(setup_name=LONG_SETUP_NAME, conditions=conditions)


def is_news_lockout_active(timestamp_ns: int) -> bool:
    """Return whether the timestamp is inside a scheduled news lockout window."""
    # TODO: Connect this to the versioned economic-news calendar once that module exists.
    return False


def _check_at_important_level(
    strategy_spec: StrategySpec,
    context: LongAbsorptionReclaimContext,
    snapshots: tuple[MarketState, ...],
) -> SetupConditionResult:
    if context.important_level not in strategy_spec.context_requirements.important_levels:
        return _fail(
            "at_important_level",
            f"Important level {context.important_level.value} is not enabled by the strategy spec",
        )

    touched_level = any(
        _price_is_at_level(_reference_price(snapshot), context.important_level_price, context.tick_size)
        for snapshot in snapshots
    )
    if not touched_level:
        return _fail(
            "at_important_level",
            f"Price did not trade at {context.important_level.value}",
        )

    return _pass("at_important_level", f"Price at {context.important_level.value}")


def _check_aggressive_sell_volume(
    observed_volume: Decimal,
    threshold: ParameterValue,
) -> SetupConditionResult:
    resolved_threshold = _resolve_threshold(threshold, "Aggressive sell volume threshold")
    if resolved_threshold is None:
        return _fail("aggressive_sell_volume", "Aggressive sell volume threshold is unresolved")

    if observed_volume < resolved_threshold:
        return _fail(
            "aggressive_sell_volume",
            f"Only {observed_volume} contracts sold aggressively; needed {resolved_threshold}",
        )

    return _pass("aggressive_sell_volume", f"{observed_volume} contracts sold aggressively")


def _check_downward_progress(
    observed_ticks: Decimal,
    threshold: ParameterValue,
) -> SetupConditionResult:
    resolved_threshold = _resolve_threshold(threshold, "Maximum downward progress threshold")
    if resolved_threshold is None:
        return _fail("downward_progress_ticks", "Maximum downward progress threshold is unresolved")

    if observed_ticks > resolved_threshold:
        return _fail(
            "downward_progress_ticks",
            f"Price moved {observed_ticks} ticks lower; maximum allowed is {resolved_threshold}",
        )

    return _pass("downward_progress_ticks", f"Price moved only {observed_ticks} ticks lower")


def _check_bid_reload_count(
    snapshots: tuple[MarketState, ...],
    context: LongAbsorptionReclaimContext,
    liquidity_threshold: ParameterValue,
    min_reload_count: ParameterValue,
) -> SetupConditionResult:
    resolved_liquidity_threshold = _resolve_threshold(liquidity_threshold, "Bid liquidity threshold")
    resolved_min_reload_count = _resolve_threshold(min_reload_count, "Minimum reload count")
    if resolved_liquidity_threshold is None:
        return _fail("bid_reload_count", "Bid liquidity threshold is unresolved")
    if resolved_min_reload_count is None:
        return _fail("bid_reload_count", "Minimum bid reload count is unresolved")

    reload_count = calculate_bid_reload_count(
        snapshots,
        level=context.bid_reload_level,
        threshold=resolved_liquidity_threshold,
    )
    if Decimal(reload_count) < resolved_min_reload_count:
        return _fail(
            "bid_reload_count",
            f"Bid liquidity reloaded {reload_count} times; needed {resolved_min_reload_count}",
        )

    return _pass("bid_reload_count", f"Bid liquidity reloaded {reload_count} times")


def _check_ask_cancellation_ratio(
    observed_ratio: Decimal,
    threshold: ParameterValue,
) -> SetupConditionResult:
    resolved_threshold = _resolve_threshold(threshold, "Ask cancellation ratio threshold")
    if resolved_threshold is None:
        return _fail("ask_cancellation_ratio", "Ask cancellation ratio threshold is unresolved")

    if observed_ratio < resolved_threshold:
        return _fail(
            "ask_cancellation_ratio",
            f"Ask cancellation ratio {observed_ratio} is below required {resolved_threshold}",
        )

    return _pass("ask_cancellation_ratio", f"Ask cancellation ratio {observed_ratio} met threshold")


def _check_reclaimed_level(
    market_state: MarketState,
    context: LongAbsorptionReclaimContext,
    reclaim_ticks: ParameterValue,
) -> SetupConditionResult:
    resolved_reclaim_ticks = _resolve_threshold(reclaim_ticks, "Reclaim tick threshold")
    if resolved_reclaim_ticks is None:
        return _fail("reclaimed_level", "Reclaim tick threshold is unresolved")

    current_price = _reference_price(market_state)
    if current_price is None:
        return _fail("reclaimed_level", "Reclaim confirmation not completed")

    reclaim_price = context.important_level_price + (resolved_reclaim_ticks * context.tick_size)
    if current_price < reclaim_price:
        return _fail("reclaimed_level", "Reclaim confirmation not completed")

    return _pass("reclaimed_level", "Reclaim confirmation completed")


def _check_news_lockout(market_state: MarketState) -> SetupConditionResult:
    if is_news_lockout_active(market_state.timestamp_ns):
        return _fail("news_lockout", "News lockout window is active")

    return _pass("news_lockout", "Not in a news lockout window")


def _snapshots_with_current(
    feature_window: Sequence[MarketState],
    market_state: MarketState,
) -> tuple[MarketState, ...]:
    snapshots = tuple(feature_window)
    if not snapshots:
        return (market_state,)
    if snapshots[-1] == market_state:
        return snapshots
    return (*snapshots, market_state)


def _aggressive_sell_volume(snapshots: tuple[MarketState, ...]) -> Decimal:
    if len(snapshots) < 2:
        return snapshots[-1].executed_sell_volume

    delta = snapshots[-1].executed_sell_volume - snapshots[0].executed_sell_volume
    return max(delta, Decimal("0"))


def _downward_progress_ticks(
    *,
    snapshots: tuple[MarketState, ...],
    important_level_price: Decimal,
    tick_size: Decimal,
) -> Decimal:
    if tick_size <= Decimal("0"):
        raise ValueError("tick_size must be greater than 0")

    reference_prices = tuple(
        price for price in (_reference_price(snapshot) for snapshot in snapshots) if price is not None
    )
    if not reference_prices:
        return Decimal("0")

    lowest_price = min(reference_prices)
    if lowest_price >= important_level_price:
        return Decimal("0")

    return (important_level_price - lowest_price) / tick_size


def _ask_cancellation_ratio(snapshots: tuple[MarketState, ...]) -> Decimal:
    added = Decimal("0")
    cancelled = Decimal("0")

    for previous, current in zip(snapshots, snapshots[1:]):
        previous_by_price = {level.price: level.size for level in previous.ask_depth}
        current_by_price = {level.price: level.size for level in current.ask_depth}
        for price in previous_by_price.keys() | current_by_price.keys():
            delta = current_by_price.get(price, Decimal("0")) - previous_by_price.get(
                price,
                Decimal("0"),
            )
            if delta > Decimal("0"):
                added += delta
            elif delta < Decimal("0"):
                cancelled += -delta

    total_changed = added + cancelled
    if total_changed == Decimal("0"):
        return Decimal("0")

    return cancelled / total_changed


def _reference_price(market_state: MarketState) -> Decimal | None:
    if market_state.mid_price is not None:
        return market_state.mid_price
    if market_state.best_bid is not None:
        return market_state.best_bid
    if market_state.best_ask is not None:
        return market_state.best_ask
    return None


def _price_is_at_level(
    price: Decimal | None,
    level_price: Decimal,
    tolerance: Decimal,
) -> bool:
    if price is None:
        return False
    return abs(price - level_price) <= tolerance


def _resolve_threshold(value: ParameterValue, threshold_name: str) -> Decimal | None:
    if value == UNRESOLVED:
        return None
    if value < Decimal("0"):
        raise ValueError(f"{threshold_name} must be non-negative")
    return value


def _pass(key: str, message: str) -> SetupConditionResult:
    return SetupConditionResult(key=key, passed=True, message=message)


def _fail(key: str, message: str) -> SetupConditionResult:
    return SetupConditionResult(key=key, passed=False, message=message)
