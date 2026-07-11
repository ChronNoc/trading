"""Tests for Bookmap WebSocket market-event formatting."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from bookmap_addon.addon import MnqBookmapAddon
from bookmap_addon.events import (
    EventSchemaError,
    event_to_json,
    format_depth_update,
    format_trade,
    parse_event_message,
)


class CapturePublisher:
    """In-memory publisher used to test the Bookmap adapter without WebSockets."""

    def __init__(self) -> None:
        """Create an empty capture publisher."""
        self.started = False
        self.stopped = False
        self.events: list[dict[str, object]] = []

    def start(self) -> None:
        """Record that the publisher started."""
        self.started = True

    def stop(self) -> None:
        """Record that the publisher stopped."""
        self.stopped = True

    def publish(self, event: dict[str, object]) -> bool:
        """Capture one formatted event."""
        self.events.append(event)
        return True


def test_depth_update_format_matches_task_five_schema() -> None:
    """Depth updates are normalized into the exact Task 5 schema."""
    event = format_depth_update(
        timestamp=123,
        symbol="MNQ",
        side=True,
        price=Decimal("100.25"),
        previous_size=Decimal("7"),
        new_size=Decimal("12"),
    )

    assert event == {
        "type": "depth_update",
        "timestamp": 123,
        "symbol": "MNQ",
        "side": "bid",
        "price": "100.25",
        "previous_size": "7",
        "new_size": "12",
    }


def test_trade_format_matches_task_five_schema() -> None:
    """Trades are normalized into the exact Task 5 schema."""
    event = format_trade(
        timestamp_ns=456,
        price="100.50",
        size=3,
        aggressor_side="seller",
        instrument="MNQ",
        sequence_id=9,
    )

    assert event == {
        "timestamp_ns": 456,
        "price": "100.50",
        "size": "3",
        "aggressor_side": "sell",
        "instrument": "MNQ",
        "sequence_id": 9,
    }


def test_event_json_round_trip_for_depth_update() -> None:
    """Depth update JSON payloads parse back to the same normalized event."""
    event = format_depth_update(
        timestamp=123,
        symbol="MNQ",
        side="ask",
        price="100.75",
        previous_size="15",
        new_size="0",
    )

    payload = event_to_json(event)

    assert json.loads(payload) == event
    assert parse_event_message(payload) == event


def test_event_json_round_trip_for_trade() -> None:
    """Trade JSON payloads parse back to the same normalized event."""
    event = format_trade(
        timestamp_ns=456,
        price="100.25",
        size="4",
        aggressor_side="buy",
        instrument="MNQ",
        sequence_id=10,
    )

    payload = event_to_json(event)

    assert json.loads(payload) == event
    assert parse_event_message(payload) == event


def test_parser_rejects_extra_fields() -> None:
    """WebSocket payload parsing rejects messages outside the exact schemas."""
    payload = json.dumps(
        {
            "type": "depth_update",
            "timestamp": 1,
            "symbol": "MNQ",
            "side": "bid",
            "price": "100.00",
            "previous_size": "1",
            "new_size": "2",
            "extra": "not allowed",
        },
    )

    with pytest.raises(EventSchemaError, match="schema exactly"):
        parse_event_message(payload)


def test_parser_rejects_float_price_payloads() -> None:
    """Parsed payloads require price values to arrive as decimal strings."""
    payload = json.dumps(
        {
            "timestamp_ns": 1,
            "price": 100.25,
            "size": "1",
            "aggressor_side": "buy",
            "instrument": "MNQ",
            "sequence_id": 1,
        },
    )

    with pytest.raises(EventSchemaError, match="price must be encoded"):
        parse_event_message(payload)


def test_addon_forwards_depth_update_and_tracks_previous_size() -> None:
    """The Bookmap adapter fills previous_size when callbacks only include current size."""
    publisher = CapturePublisher()
    addon = MnqBookmapAddon(symbol="MNQ", publisher=publisher)

    addon.start()
    first_result = addon.on_market_depth(
        timestamp=100,
        is_bid=True,
        price="100.00",
        size="10",
    )
    second_result = addon.on_market_depth(
        timestamp=200,
        is_bid=True,
        price="100.00",
        size="6",
    )
    addon.stop()

    assert first_result is True
    assert second_result is True
    assert publisher.started is True
    assert publisher.stopped is True
    assert publisher.events == [
        {
            "type": "depth_update",
            "timestamp": 100,
            "symbol": "MNQ",
            "side": "bid",
            "price": "100.00",
            "previous_size": "0",
            "new_size": "10",
        },
        {
            "type": "depth_update",
            "timestamp": 200,
            "symbol": "MNQ",
            "side": "bid",
            "price": "100.00",
            "previous_size": "10",
            "new_size": "6",
        },
    ]


def test_addon_forwards_trade_with_generated_sequence_id() -> None:
    """The Bookmap adapter can assign monotonic sequence ids for trade callbacks."""
    publisher = CapturePublisher()
    addon = MnqBookmapAddon(symbol="MNQ", publisher=publisher)

    result = addon.on_trade(
        timestamp_ns=300,
        price="100.25",
        size="2",
        aggressor_side="buy",
    )

    assert result is True
    assert publisher.events == [
        {
            "timestamp_ns": 300,
            "price": "100.25",
            "size": "2",
            "aggressor_side": "buy",
            "instrument": "MNQ",
            "sequence_id": 1,
        },
    ]
