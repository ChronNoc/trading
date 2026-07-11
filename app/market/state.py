"""Immutable market-state snapshots built from depth and trade events."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Mapping, TypeAlias

MAX_DEPTH_LEVELS = 20
RawEvent: TypeAlias = Mapping[str, object]


@dataclass(frozen=True, slots=True)
class DepthLevel:
    """One price level in the visible order book."""

    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class MarketState:
    """Immutable market state derived from depth and trade events."""

    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    spread: Decimal | None = None
    mid_price: Decimal | None = None
    bid_depth: tuple[DepthLevel, ...] = ()
    ask_depth: tuple[DepthLevel, ...] = ()
    executed_buy_volume: Decimal = Decimal("0")
    executed_sell_volume: Decimal = Decimal("0")
    timestamp_ns: int = 0

    @property
    def depth(self) -> dict[str, tuple[DepthLevel, ...]]:
        """Return visible bid and ask depth keyed by book side."""
        return {
            "bid": self.bid_depth,
            "ask": self.ask_depth,
        }

    def bid_size_at_level(self, level: int) -> Decimal:
        """Return bid size at a 1-based depth level, or zero if absent."""
        _validate_depth_level(level)
        index = level - 1
        if index >= len(self.bid_depth):
            return Decimal("0")
        return self.bid_depth[index].size

    def ask_size_at_level(self, level: int) -> Decimal:
        """Return ask size at a 1-based depth level, or zero if absent."""
        _validate_depth_level(level)
        index = level - 1
        if index >= len(self.ask_depth):
            return Decimal("0")
        return self.ask_depth[index].size

    def update(self, raw_event: RawEvent) -> "MarketState":
        """Return a new market state with one raw depth or trade event applied."""
        event_type = str(raw_event.get("type", "")).lower()
        if event_type == "depth_update" or _looks_like_depth_update(raw_event):
            return self._apply_depth_update(raw_event)
        if event_type == "trade" or _looks_like_trade(raw_event):
            return self._apply_trade(raw_event)

        raise ValueError("Unsupported market event: expected depth_update or trade schema")

    def _apply_depth_update(self, raw_event: RawEvent) -> "MarketState":
        side = _normalize_depth_side(raw_event.get("side"))
        price = _coerce_decimal(raw_event.get("price"), "price")
        new_size = _coerce_decimal(raw_event.get("new_size"), "new_size")
        timestamp_ns = _coerce_timestamp(raw_event.get("timestamp"), "timestamp")

        if price <= Decimal("0"):
            raise ValueError("price must be greater than 0")
        if new_size < Decimal("0"):
            raise ValueError("new_size must be non-negative")

        if side == "bid":
            bid_depth = _update_depth_side(self.bid_depth, price, new_size, reverse=True)
            ask_depth = self.ask_depth
        else:
            bid_depth = self.bid_depth
            ask_depth = _update_depth_side(self.ask_depth, price, new_size, reverse=False)

        return _with_recalculated_book(
            replace(
                self,
                bid_depth=bid_depth,
                ask_depth=ask_depth,
                timestamp_ns=timestamp_ns,
            ),
        )

    def _apply_trade(self, raw_event: RawEvent) -> "MarketState":
        aggressor_side = _normalize_aggressor_side(raw_event.get("aggressor_side"))
        size = _coerce_decimal(raw_event.get("size"), "size")
        timestamp_ns = _coerce_timestamp(raw_event.get("timestamp_ns"), "timestamp_ns")

        if size <= Decimal("0"):
            raise ValueError("size must be greater than 0")

        if aggressor_side == "buy":
            return replace(
                self,
                executed_buy_volume=self.executed_buy_volume + size,
                timestamp_ns=timestamp_ns,
            )

        return replace(
            self,
            executed_sell_volume=self.executed_sell_volume + size,
            timestamp_ns=timestamp_ns,
        )


def _looks_like_depth_update(raw_event: RawEvent) -> bool:
    return {"side", "price", "previous_size", "new_size"}.issubset(raw_event)


def _looks_like_trade(raw_event: RawEvent) -> bool:
    return {"timestamp_ns", "price", "size", "aggressor_side", "instrument", "sequence_id"}.issubset(
        raw_event,
    )


def _update_depth_side(
    current_depth: tuple[DepthLevel, ...],
    price: Decimal,
    new_size: Decimal,
    *,
    reverse: bool,
) -> tuple[DepthLevel, ...]:
    levels_by_price = {level.price: level.size for level in current_depth}
    if new_size == Decimal("0"):
        levels_by_price.pop(price, None)
    else:
        levels_by_price[price] = new_size

    sorted_prices = sorted(levels_by_price, reverse=reverse)
    return tuple(
        DepthLevel(price=level_price, size=levels_by_price[level_price])
        for level_price in sorted_prices[:MAX_DEPTH_LEVELS]
    )


def _with_recalculated_book(state: MarketState) -> MarketState:
    best_bid = state.bid_depth[0].price if state.bid_depth else None
    best_ask = state.ask_depth[0].price if state.ask_depth else None

    if best_bid is None or best_ask is None:
        return replace(state, best_bid=best_bid, best_ask=best_ask, spread=None, mid_price=None)

    spread = best_ask - best_bid
    mid_price = (best_bid + best_ask) / Decimal("2")
    return replace(state, best_bid=best_bid, best_ask=best_ask, spread=spread, mid_price=mid_price)


def _normalize_depth_side(value: object) -> str:
    normalized = str(value).lower()
    if normalized in {"bid", "b"}:
        return "bid"
    if normalized in {"ask", "offer", "a"}:
        return "ask"
    raise ValueError("side must be bid or ask")


def _normalize_aggressor_side(value: object) -> str:
    normalized = str(value).lower()
    if normalized in {"buy", "buyer", "b"}:
        return "buy"
    if normalized in {"sell", "seller", "s"}:
        return "sell"
    raise ValueError("aggressor_side must be buy or sell")


def _coerce_decimal(value: object, field_name: str) -> Decimal:
    if value is None:
        raise ValueError(f"{field_name} is required")

    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{field_name} must be a decimal-compatible value") from error

    if not decimal_value.is_finite():
        raise ValueError(f"{field_name} must be finite")

    return decimal_value


def _coerce_timestamp(value: object, field_name: str) -> int:
    if value is None:
        raise ValueError(f"{field_name} is required")

    timestamp_ns = int(value)
    if timestamp_ns < 0:
        raise ValueError(f"{field_name} must be non-negative")

    return timestamp_ns


def _validate_depth_level(level: int) -> None:
    if level < 1 or level > MAX_DEPTH_LEVELS:
        raise ValueError(f"depth level must be between 1 and {MAX_DEPTH_LEVELS}")
