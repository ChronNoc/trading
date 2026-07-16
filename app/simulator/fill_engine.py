"""Deterministic paper fill engine: queue position, liquidity, partial fills.

Simulates one order's life against a stream of market trade events with explicit,
inspectable assumptions (all Decimal):

- **resting queue ahead**: volume that must trade at the order's price before
  any fill accrues (queue position).
- **available liquidity**: each market trade event only fills up to its own
  size after the queue ahead is consumed - so large orders fill PARTIALLY over
  MULTIPLE events at possibly multiple prices.
- **acknowledgement delay**: no fill may occur before ``ack_delay_ns`` after
  submission; a broker rejection terminates the order at acknowledgement.
- **cancel/replace delay**: a cancel or replace takes effect only after
  ``cancel_delay_ns`` - fills arriving before the cancel lands win the race and
  reduce/void it (the classic fill-vs-cancel race).
- **slippage and commission** are applied to the weighted-average fill.
- **disconnect**: freezes the order (no further fills) and marks it for
  reconciliation rather than guessing.

The engine returns append-only :class:`FillRecord` rows plus a final
:class:`SimulatedOrderResult` with the weighted-average price, so ledgers can
consume actual simulated order/fill records instead of bare setup counts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

STATUS_PENDING = "pending_ack"
STATUS_WORKING = "working"
STATUS_PARTIAL = "partially_filled"
STATUS_FILLED = "filled"
STATUS_CANCELLED = "cancelled"
STATUS_REJECTED = "rejected"
STATUS_DISCONNECTED = "disconnected_needs_reconciliation"


@dataclass(frozen=True, slots=True)
class FillModelConfig:
    """Explicit simulation assumptions (every knob visible, none hidden)."""

    queue_ahead_contracts: int = 0
    liquidity_fraction: Decimal = Decimal("1")  # fraction of each event's size available to us
    ack_delay_ns: int = 0
    cancel_delay_ns: int = 0
    slippage_per_contract: Decimal = Decimal("0")
    commission_per_contract: Decimal = Decimal("1.24")

    def __post_init__(self) -> None:
        """Validate assumptions."""
        if self.queue_ahead_contracts < 0 or self.ack_delay_ns < 0 or self.cancel_delay_ns < 0:
            raise ValueError("delays and queue ahead must be non-negative")
        if not Decimal("0") < self.liquidity_fraction <= Decimal("1"):
            raise ValueError("liquidity_fraction must be in (0, 1]")
        if self.slippage_per_contract < 0 or self.commission_per_contract < 0:
            raise ValueError("costs must be non-negative")


@dataclass(frozen=True, slots=True)
class MarketTrade:
    """One market trade event the order can interact with."""

    timestamp_ns: int
    price: Decimal
    size: int


@dataclass(frozen=True, slots=True)
class FillRecord:
    """One partial fill with full lineage."""

    timestamp_ns: int
    price: Decimal
    quantity: int


@dataclass(frozen=True, slots=True)
class SimulatedOrderResult:
    """Final outcome of one simulated order."""

    status: str
    ordered_quantity: int
    filled_quantity: int
    fills: tuple[FillRecord, ...]
    average_fill_price: Decimal
    slippage_cost: Decimal
    commission: Decimal
    cancelled_quantity: int

    @property
    def remaining_quantity(self) -> int:
        """Quantity neither filled nor cancelled (only for disconnects)."""
        return max(0, self.ordered_quantity - self.filled_quantity - self.cancelled_quantity)


@dataclass(slots=True)
class SimulatedOrder:
    """A working simulated order stepping through market trade events."""

    quantity: int
    limit_price: Decimal
    is_buy: bool
    submitted_at_ns: int
    config: FillModelConfig = field(default_factory=FillModelConfig)
    rejected: bool = False
    status: str = STATUS_PENDING
    queue_remaining: int = field(init=False, default=0)
    fills: list[FillRecord] = field(init=False, default_factory=list)
    cancel_requested_at_ns: int | None = field(init=False, default=None)
    replace_price: Decimal | None = field(init=False, default=None)
    replace_requested_at_ns: int | None = field(init=False, default=None)
    disconnected: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        """Validate and initialize queue position."""
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        self.queue_remaining = self.config.queue_ahead_contracts

    # -- caller actions -----------------------------------------------------------

    def request_cancel(self, at_ns: int) -> None:
        """Request cancellation; takes effect after the configured delay."""
        if self.cancel_requested_at_ns is None:
            self.cancel_requested_at_ns = at_ns

    def request_replace(self, at_ns: int, new_price: Decimal) -> None:
        """Request a price replace; takes effect after the configured delay."""
        self.replace_requested_at_ns = at_ns
        self.replace_price = new_price

    def disconnect(self) -> None:
        """Transport lost: freeze the order for reconciliation, never guess fills."""
        self.disconnected = True

    # -- event stepping --------------------------------------------------------------

    def on_trade(self, trade: MarketTrade) -> FillRecord | None:
        """Advance the order by one market trade; maybe produce a partial fill."""
        if self.disconnected or self.status in {STATUS_FILLED, STATUS_CANCELLED, STATUS_REJECTED}:
            return None
        # Acknowledgement first: nothing can happen before the broker ack.
        if trade.timestamp_ns < self.submitted_at_ns + self.config.ack_delay_ns:
            return None
        if self.status == STATUS_PENDING:
            if self.rejected:
                self.status = STATUS_REJECTED
                return None
            self.status = STATUS_WORKING
        # Replace lands after its delay (price change while working).
        if (self.replace_requested_at_ns is not None and self.replace_price is not None
                and trade.timestamp_ns >= self.replace_requested_at_ns + self.config.cancel_delay_ns):
            self.limit_price = self.replace_price
            self.replace_requested_at_ns = None
            self.replace_price = None
        # Cancel lands after its delay; fills that already happened stand.
        if (self.cancel_requested_at_ns is not None
                and trade.timestamp_ns >= self.cancel_requested_at_ns + self.config.cancel_delay_ns):
            self.status = STATUS_CANCELLED if not self.fills else STATUS_PARTIAL
            self.cancel_requested_at_ns = None
            if self.status == STATUS_CANCELLED:
                return None
            # Partially filled then cancelled: remaining quantity is gone.
            self.status = STATUS_CANCELLED
            return None
        # Price must trade at (or through) our limit to interact with us.
        marketable = trade.price <= self.limit_price if self.is_buy else trade.price >= self.limit_price
        if not marketable:
            return None
        available = int(Decimal(trade.size) * self.config.liquidity_fraction)
        if available <= 0:
            return None
        # Queue ahead of us consumes the event's volume first.
        if self.queue_remaining > 0:
            consumed = min(self.queue_remaining, available)
            self.queue_remaining -= consumed
            available -= consumed
            if available <= 0:
                return None
        remaining = self.quantity - sum(f.quantity for f in self.fills)
        if remaining <= 0:
            return None
        quantity = min(remaining, available)
        fill = FillRecord(timestamp_ns=trade.timestamp_ns, price=trade.price, quantity=quantity)
        self.fills.append(fill)
        filled = sum(f.quantity for f in self.fills)
        self.status = STATUS_FILLED if filled >= self.quantity else STATUS_PARTIAL
        return fill

    def result(self) -> SimulatedOrderResult:
        """Summarize the order with weighted-average fill and Decimal costs."""
        filled = sum(f.quantity for f in self.fills)
        if filled:
            value = sum((f.price * Decimal(f.quantity) for f in self.fills), Decimal("0"))
            average = (value / Decimal(filled)).quantize(Decimal("0.01"))
        else:
            average = Decimal("0")
        if self.disconnected and self.status not in {STATUS_FILLED, STATUS_CANCELLED, STATUS_REJECTED}:
            status = STATUS_DISCONNECTED
        else:
            status = self.status
        cancelled = 0
        if status == STATUS_CANCELLED:
            cancelled = self.quantity - filled
        return SimulatedOrderResult(
            status=status,
            ordered_quantity=self.quantity,
            filled_quantity=filled,
            fills=tuple(self.fills),
            average_fill_price=average,
            slippage_cost=self.config.slippage_per_contract * Decimal(filled),
            commission=self.config.commission_per_contract * Decimal(filled),
            cancelled_quantity=cancelled,
        )


def simulate_order(
    *,
    quantity: int,
    limit_price: Decimal,
    is_buy: bool,
    submitted_at_ns: int,
    trades: Sequence[MarketTrade],
    config: FillModelConfig | None = None,
    rejected: bool = False,
    cancel_at_ns: int | None = None,
    replace: tuple[int, Decimal] | None = None,
    disconnect_at_ns: int | None = None,
) -> SimulatedOrderResult:
    """Run one order through a trade stream deterministically and summarize it."""
    order = SimulatedOrder(quantity=quantity, limit_price=limit_price, is_buy=is_buy,
                           submitted_at_ns=submitted_at_ns, config=config or FillModelConfig(),
                           rejected=rejected)
    if cancel_at_ns is not None:
        order.request_cancel(cancel_at_ns)
    if replace is not None:
        order.request_replace(replace[0], replace[1])
    for trade in sorted(trades, key=lambda t: t.timestamp_ns):
        if disconnect_at_ns is not None and trade.timestamp_ns >= disconnect_at_ns:
            order.disconnect()
        order.on_trade(trade)
    return order.result()
