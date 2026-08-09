"""Read-only market event for the Bidirectional Paper Trading Lab.

Deliberately tiny and self-contained: the lab NEVER imports the live market
feed, receiver, recorder, or any execution module. It only ever consumes an
immutable stream of these events (from a recorded session replay, or any other
read-only source) - it can never publish back, change a subscription, or touch
runtime state.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal


@dataclass(frozen=True, slots=True)
class MarketEvent:
    """One immutable market observation the lab may act on.

    ``seq`` is a strictly increasing logical index the engine assigns as it
    consumes the stream; it is how the lab proves that no other event was
    processed between the two legs of a paired entry (both legs are stamped with
    the seq of the single triggering event).
    """

    ts_ns: int
    seq: int = 0
    last: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    kind: str = "trade"  # "trade" | "depth"

    @property
    def mid(self) -> Decimal | None:
        """Mid price when both sides are known, else None."""
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / Decimal(2)
        return None

    def price(self, source: str) -> Decimal | None:
        """Return the chosen price source (last/bid/ask/mid), or None if absent."""
        if source == "last":
            return self.last
        if source == "bid":
            return self.bid
        if source == "ask":
            return self.ask
        if source == "mid":
            return self.mid
        raise ValueError(f"unknown price source {source!r}")


def align_to_tick(price: Decimal, tick_size: Decimal, *, round_up: bool) -> Decimal:
    """Snap a price to the instrument grid on the given side (never invents a price)."""
    ticks = price / tick_size
    rounded = ticks.to_integral_value(rounding=ROUND_CEILING if round_up else ROUND_FLOOR)
    return rounded * tick_size


def nearest_tick(price: Decimal, tick_size: Decimal) -> Decimal:
    """Snap to the nearest grid price (for display/level normalisation only)."""
    ticks = (price / tick_size).to_integral_value(rounding=ROUND_HALF_UP)
    return ticks * tick_size
