"""Order-fill decisions from current book state and queue assumptions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Literal, TypeAlias

from app.market.state import DepthLevel, MarketState
from app.simulator.slippage import OrderSide

OrderStatus: TypeAlias = Literal["open", "partially_filled", "filled", "rejected"]


@dataclass(frozen=True, slots=True)
class FillAssumptions:
    """Configurable queue-position assumptions for simulated fills."""

    queue_position_fraction: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class SimulatedOrder:
    """A simulated limit order tracked by the replay engine."""

    order_id: str
    symbol: str
    side: OrderSide
    quantity: int
    remaining_quantity: int
    limit_price: Decimal
    submitted_timestamp_ns: int


@dataclass(frozen=True, slots=True)
class FillDecision:
    """Result of trying to fill an order against the current book."""

    filled_quantity: int
    fill_price: Decimal | None
    remaining_quantity: int
    partial: bool
    status: OrderStatus
    reason: str


def decide_fill(
    order: SimulatedOrder,
    market_state: MarketState,
    assumptions: FillAssumptions,
) -> FillDecision:
    """Decide whether an order fills against the current visible book."""
    _validate_order(order)
    _validate_assumptions(assumptions)

    if order.side == "buy":
        candidate_levels = tuple(
            level for level in market_state.ask_depth if level.price <= order.limit_price
        )
    else:
        candidate_levels = tuple(
            level for level in market_state.bid_depth if level.price >= order.limit_price
        )

    filled_quantity, fill_price = _fill_from_levels(
        candidate_levels,
        order.remaining_quantity,
        assumptions.queue_position_fraction,
    )
    if filled_quantity == 0:
        return FillDecision(
            filled_quantity=0,
            fill_price=None,
            remaining_quantity=order.remaining_quantity,
            partial=False,
            status="open",
            reason="Order is not marketable or no queue-adjusted liquidity is available.",
        )

    remaining_quantity = order.remaining_quantity - filled_quantity
    if remaining_quantity == 0:
        return FillDecision(
            filled_quantity=filled_quantity,
            fill_price=fill_price,
            remaining_quantity=0,
            partial=False,
            status="filled",
            reason="Order fully filled.",
        )

    return FillDecision(
        filled_quantity=filled_quantity,
        fill_price=fill_price,
        remaining_quantity=remaining_quantity,
        partial=True,
        status="partially_filled",
        reason="Order partially filled.",
    )


def _fill_from_levels(
    levels: tuple[DepthLevel, ...],
    quantity: int,
    queue_position_fraction: Decimal,
) -> tuple[int, Decimal | None]:
    remaining = quantity
    filled = 0
    notional = Decimal("0")

    for level in levels:
        available_quantity = _available_quantity(level.size, queue_position_fraction)
        if available_quantity <= 0:
            continue

        level_fill = min(remaining, available_quantity)
        filled += level_fill
        notional += level.price * Decimal(level_fill)
        remaining -= level_fill
        if remaining == 0:
            break

    if filled == 0:
        return 0, None

    return filled, notional / Decimal(filled)


def _available_quantity(resting_size: Decimal, queue_position_fraction: Decimal) -> int:
    queue_ahead = resting_size * queue_position_fraction
    available = resting_size - queue_ahead
    if available <= Decimal("0"):
        return 0

    return int(available.to_integral_value(rounding=ROUND_FLOOR))


def _validate_order(order: SimulatedOrder) -> None:
    if order.side not in {"buy", "sell"}:
        raise ValueError("order side must be buy or sell")
    if order.quantity <= 0:
        raise ValueError("order quantity must be greater than 0")
    if order.remaining_quantity <= 0:
        raise ValueError("remaining quantity must be greater than 0")
    if order.remaining_quantity > order.quantity:
        raise ValueError("remaining quantity cannot exceed original quantity")
    if order.limit_price <= Decimal("0"):
        raise ValueError("limit price must be greater than 0")
    if order.submitted_timestamp_ns < 0:
        raise ValueError("submitted timestamp must be non-negative")


def _validate_assumptions(assumptions: FillAssumptions) -> None:
    if assumptions.queue_position_fraction < Decimal("0"):
        raise ValueError("queue_position_fraction must be non-negative")
    if assumptions.queue_position_fraction >= Decimal("1"):
        raise ValueError("queue_position_fraction must be less than 1")
