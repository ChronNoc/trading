"""Order-flow feature calculations from rolling market-state windows."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from app.market.state import MAX_DEPTH_LEVELS, DepthLevel, MarketState

NANOSECONDS_PER_SECOND = Decimal("1000000000")


@dataclass(frozen=True, slots=True)
class MarketFeatures:
    """Computed market features for a rolling state window."""

    book_imbalance: Decimal
    liquidity_added: Decimal
    liquidity_cancelled: Decimal
    trade_velocity: Decimal
    short_term_volatility: Decimal
    distance_from_session_high: Decimal | None
    distance_from_session_low: Decimal | None
    bid_reload_count: int


def compute_market_features(
    snapshots: Sequence[MarketState],
    *,
    bid_reload_level: int = 1,
    bid_reload_threshold: Decimal = Decimal("1"),
) -> MarketFeatures:
    """Compute all market features for a rolling window of market states."""
    if not snapshots:
        raise ValueError("snapshots must contain at least one MarketState")

    return MarketFeatures(
        book_imbalance=calculate_book_imbalance(snapshots[-1]),
        liquidity_added=calculate_liquidity_added(snapshots),
        liquidity_cancelled=calculate_liquidity_cancelled(snapshots),
        trade_velocity=calculate_trade_velocity(snapshots),
        short_term_volatility=calculate_short_term_volatility(snapshots),
        distance_from_session_high=calculate_distance_from_session_high(snapshots),
        distance_from_session_low=calculate_distance_from_session_low(snapshots),
        bid_reload_count=calculate_bid_reload_count(
            snapshots,
            level=bid_reload_level,
            threshold=bid_reload_threshold,
        ),
    )


def calculate_book_imbalance(state: MarketState) -> Decimal:
    """Calculate normalized book imbalance from the latest snapshot."""
    bid_size = _sum_depth_size(state.bid_depth)
    ask_size = _sum_depth_size(state.ask_depth)
    total_size = bid_size + ask_size
    if total_size == Decimal("0"):
        return Decimal("0")

    return (bid_size - ask_size) / total_size


def calculate_liquidity_added(snapshots: Sequence[MarketState]) -> Decimal:
    """Calculate total visible liquidity added across a rolling window."""
    added, _cancelled = _calculate_liquidity_changes(snapshots)
    return added


def calculate_liquidity_cancelled(snapshots: Sequence[MarketState]) -> Decimal:
    """Calculate total visible liquidity cancelled across a rolling window."""
    _added, cancelled = _calculate_liquidity_changes(snapshots)
    return cancelled


def calculate_trade_velocity(snapshots: Sequence[MarketState]) -> Decimal:
    """Calculate executed volume per second over the rolling window."""
    if len(snapshots) < 2:
        return Decimal("0")

    first = snapshots[0]
    last = snapshots[-1]
    elapsed_ns = Decimal(last.timestamp_ns - first.timestamp_ns)
    if elapsed_ns <= Decimal("0"):
        return Decimal("0")

    first_volume = first.executed_buy_volume + first.executed_sell_volume
    last_volume = last.executed_buy_volume + last.executed_sell_volume
    volume_delta = last_volume - first_volume
    if volume_delta <= Decimal("0"):
        return Decimal("0")

    elapsed_seconds = elapsed_ns / NANOSECONDS_PER_SECOND
    return volume_delta / elapsed_seconds


def calculate_short_term_volatility(snapshots: Sequence[MarketState]) -> Decimal:
    """Calculate population standard deviation of mid prices in the window."""
    mid_prices = _mid_prices(snapshots)
    if len(mid_prices) < 2:
        return Decimal("0")

    count = Decimal(len(mid_prices))
    mean = sum(mid_prices, Decimal("0")) / count
    variance = sum((mid_price - mean) ** 2 for mid_price in mid_prices) / count
    return variance.sqrt()


def calculate_distance_from_session_high(snapshots: Sequence[MarketState]) -> Decimal | None:
    """Calculate distance from rolling session high to the latest mid price."""
    mid_prices = _mid_prices(snapshots)
    if not mid_prices:
        return None

    current_mid = mid_prices[-1]
    return max(mid_prices) - current_mid


def calculate_distance_from_session_low(snapshots: Sequence[MarketState]) -> Decimal | None:
    """Calculate distance from latest mid price to the rolling session low."""
    mid_prices = _mid_prices(snapshots)
    if not mid_prices:
        return None

    current_mid = mid_prices[-1]
    return current_mid - min(mid_prices)


def calculate_bid_reload_count(
    snapshots: Sequence[MarketState],
    *,
    level: int,
    threshold: Decimal,
) -> int:
    """Count bid reloads after a level drops below and returns to the threshold."""
    if level < 1 or level > MAX_DEPTH_LEVELS:
        raise ValueError(f"level must be between 1 and {MAX_DEPTH_LEVELS}")
    if threshold <= Decimal("0"):
        raise ValueError("threshold must be greater than 0")

    if not snapshots:
        return 0

    was_at_or_above = snapshots[0].bid_size_at_level(level) >= threshold
    waiting_for_reload = False
    reload_count = 0

    for snapshot in snapshots[1:]:
        is_at_or_above = snapshot.bid_size_at_level(level) >= threshold
        if was_at_or_above and not is_at_or_above:
            waiting_for_reload = True
            was_at_or_above = False
        elif waiting_for_reload and is_at_or_above:
            reload_count += 1
            waiting_for_reload = False
            was_at_or_above = True
        elif is_at_or_above:
            was_at_or_above = True

    return reload_count


def _calculate_liquidity_changes(snapshots: Sequence[MarketState]) -> tuple[Decimal, Decimal]:
    added = Decimal("0")
    cancelled = Decimal("0")
    for previous, current in zip(snapshots, snapshots[1:]):
        for previous_depth, current_depth in (
            (previous.bid_depth, current.bid_depth),
            (previous.ask_depth, current.ask_depth),
        ):
            previous_by_price = _depth_by_price(previous_depth)
            current_by_price = _depth_by_price(current_depth)
            for price in previous_by_price.keys() | current_by_price.keys():
                delta = current_by_price.get(price, Decimal("0")) - previous_by_price.get(
                    price,
                    Decimal("0"),
                )
                if delta > Decimal("0"):
                    added += delta
                elif delta < Decimal("0"):
                    cancelled += -delta

    return added, cancelled


def _depth_by_price(depth: tuple[DepthLevel, ...]) -> dict[Decimal, Decimal]:
    return {level.price: level.size for level in depth}


def _sum_depth_size(depth: tuple[DepthLevel, ...]) -> Decimal:
    return sum((level.size for level in depth), Decimal("0"))


def _mid_prices(snapshots: Sequence[MarketState]) -> list[Decimal]:
    return [snapshot.mid_price for snapshot in snapshots if snapshot.mid_price is not None]
