"""Bookmap Python add-on adapter for forwarding MNQ events to the app."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from bookmap_addon.events import RawMarketEvent, format_depth_update, format_trade
from bookmap_addon.websocket_forwarder import DEFAULT_WEBSOCKET_URL, LocalWebSocketForwarder


class EventPublisher(Protocol):
    """Protocol for objects that publish formatted market events."""

    def start(self) -> None:
        """Start the publisher transport."""

    def stop(self) -> None:
        """Stop the publisher transport."""

    def publish(self, event: RawMarketEvent) -> bool:
        """Publish one raw market event and return whether it was accepted."""


class MnqBookmapAddon:
    """Minimal Bookmap adapter that forwards depth and trade callbacks locally."""

    def __init__(
        self,
        *,
        symbol: str = "MNQ",
        websocket_url: str = DEFAULT_WEBSOCKET_URL,
        publisher: EventPublisher | None = None,
    ) -> None:
        """Create the add-on adapter with a local WebSocket publisher."""
        self.symbol = symbol
        self._publisher = publisher or LocalWebSocketForwarder(websocket_url)
        self._depth_sizes: dict[tuple[str, str], Decimal] = {}
        self._next_sequence_id = 1

    def start(self) -> None:
        """Start forwarding events to the configured local WebSocket endpoint."""
        self._publisher.start()

    def stop(self) -> None:
        """Stop forwarding events and release publisher resources."""
        self._publisher.stop()

    def on_depth_update(
        self,
        *,
        timestamp: int,
        side: str | bool,
        price: object,
        new_size: object,
        previous_size: object | None = None,
        symbol: str | None = None,
    ) -> bool:
        """Forward one Bookmap depth update using the Task 5 depth-update schema."""
        event_symbol = symbol or self.symbol
        event = format_depth_update(
            timestamp=timestamp,
            symbol=event_symbol,
            side=side,
            price=price,
            previous_size=self._previous_depth_size(side, price, previous_size),
            new_size=new_size,
        )
        self._remember_depth_size(event["side"], event["price"], event["new_size"])
        return self._publisher.publish(event)

    def on_market_depth(
        self,
        *,
        timestamp: int,
        is_bid: bool,
        price: object,
        size: object,
        symbol: str | None = None,
    ) -> bool:
        """Forward a Bookmap-style depth callback that only provides current size."""
        return self.on_depth_update(
            timestamp=timestamp,
            side=is_bid,
            price=price,
            previous_size=None,
            new_size=size,
            symbol=symbol,
        )

    def on_trade(
        self,
        *,
        timestamp_ns: int,
        price: object,
        size: object,
        aggressor_side: str,
        instrument: str | None = None,
        sequence_id: int | None = None,
    ) -> bool:
        """Forward one Bookmap trade callback using the Task 5 trade schema."""
        event = format_trade(
            timestamp_ns=timestamp_ns,
            price=price,
            size=size,
            aggressor_side=aggressor_side,
            instrument=instrument or self.symbol,
            sequence_id=self._sequence_id(sequence_id),
        )
        return self._publisher.publish(event)

    def on_trade_event(
        self,
        *,
        timestamp_ns: int,
        price: object,
        size: object,
        aggressor_side: str,
        instrument: str | None = None,
        sequence_id: int | None = None,
    ) -> bool:
        """Forward an alternate Bookmap trade callback name to the same handler."""
        return self.on_trade(
            timestamp_ns=timestamp_ns,
            price=price,
            size=size,
            aggressor_side=aggressor_side,
            instrument=instrument,
            sequence_id=sequence_id,
        )

    def _previous_depth_size(
        self,
        side: str | bool,
        price: object,
        previous_size: object | None,
    ) -> object:
        if previous_size is not None:
            return previous_size
        probe = format_depth_update(
            timestamp=0,
            symbol=self.symbol,
            side=side,
            price=price,
            previous_size="0",
            new_size="0",
        )
        return self._depth_sizes.get((str(probe["side"]), str(probe["price"])), Decimal("0"))

    def _remember_depth_size(self, side: object, price: object, size: object) -> None:
        key = (str(side), str(price))
        self._depth_sizes[key] = Decimal(str(size))

    def _sequence_id(self, sequence_id: int | None) -> int:
        if sequence_id is not None:
            return sequence_id
        current = self._next_sequence_id
        self._next_sequence_id += 1
        return current


def create_addon(
    *,
    symbol: str = "MNQ",
    websocket_url: str = DEFAULT_WEBSOCKET_URL,
) -> MnqBookmapAddon:
    """Create the add-on instance for Bookmap's Python add-on loader."""
    return MnqBookmapAddon(symbol=symbol, websocket_url=websocket_url)
