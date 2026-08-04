"""Tests for local WebSocket market-event receiving."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from decimal import Decimal
from pathlib import Path

import pytest
import pyarrow.parquet as pq

from app.database.recorder import MarketEventRecorder
from app.market.receiver import (
    consume_market_stream,
    decode_market_message,
    decode_stream_message,
    decode_stream_messages,
)
from app.market.state import MarketState
from bookmap_addon.events import (
    EventSchemaError,
    event_to_json,
    format_depth_update,
    format_trade,
    parse_stream_messages,
    parse_stream_messages_resilient,
)


class MockWebSocketClient:
    """Async iterable test double for a WebSocket client."""

    def __init__(self, messages: Sequence[str | bytes]) -> None:
        """Create a mock client that yields the supplied messages."""
        self._messages = tuple(messages)
        self._index = 0

    def __aiter__(self) -> AsyncIterator[str | bytes]:
        """Return this object as an async iterator."""
        return self

    async def __anext__(self) -> str | bytes:
        """Return the next mock WebSocket message."""
        if self._index >= len(self._messages):
            raise StopAsyncIteration
        message = self._messages[self._index]
        self._index += 1
        return message


def test_receiver_updates_market_state_from_mock_websocket() -> None:
    """The receiver decodes WebSocket messages and returns the final market state."""
    messages = [
        event_to_json(
            format_depth_update(
                timestamp=100,
                symbol="MNQ",
                side="bid",
                price="100.00",
                previous_size="0",
                new_size="10",
            ),
        ),
        event_to_json(
            format_depth_update(
                timestamp=200,
                symbol="MNQ",
                side="ask",
                price="100.25",
                previous_size="0",
                new_size="8",
            ),
        ),
        event_to_json(
            format_trade(
                timestamp_ns=300,
                price="100.25",
                size="3",
                aggressor_side="buy",
                instrument="MNQ",
                sequence_id=1,
            ),
        ),
    ]
    snapshots: list[MarketState] = []

    result = asyncio.run(
        consume_market_stream(
            MockWebSocketClient(messages),
            on_state=snapshots.append,
        ),
    )

    assert result.events_processed == 3
    assert result.final_state.best_bid == Decimal("100.00")
    assert result.final_state.best_ask == Decimal("100.25")
    assert result.final_state.mid_price == Decimal("100.125")
    assert result.final_state.executed_buy_volume == Decimal("3")
    assert [snapshot.timestamp_ns for snapshot in snapshots] == [100, 200, 300]


def test_receiver_records_events_to_parquet(tmp_path: Path) -> None:
    """The receiver can persist normalized raw events through the recorder."""
    timestamp_ns = 1_783_689_600 * 1_000_000_000
    messages = [
        event_to_json(
            format_depth_update(
                timestamp=timestamp_ns,
                symbol="MNQ",
                side="bid",
                price="100.00",
                previous_size="0",
                new_size="10",
            ),
        ),
        event_to_json(
            format_trade(
                timestamp_ns=timestamp_ns + 1,
                price="100.25",
                size="1",
                aggressor_side="sell",
                instrument="MNQ",
                sequence_id=1,
            ),
        ),
    ]

    result = asyncio.run(
        consume_market_stream(
            MockWebSocketClient(messages),
            recorder=MarketEventRecorder(root_dir=tmp_path),
        ),
    )

    assert result.events_processed == 2
    assert pq.read_table(tmp_path / "2026-07-10" / "depth.parquet").num_rows == 1
    assert pq.read_table(tmp_path / "2026-07-10" / "trades.parquet").num_rows == 1


def test_receiver_can_stop_after_max_messages() -> None:
    """A max-message limit lets tests and tools consume a bounded prefix of a stream."""
    messages = [
        event_to_json(
            format_trade(
                timestamp_ns=timestamp,
                price="100.25",
                size="1",
                aggressor_side="buy",
                instrument="MNQ",
                sequence_id=timestamp,
            ),
        )
        for timestamp in (1, 2, 3)
    ]

    result = asyncio.run(consume_market_stream(MockWebSocketClient(messages), max_messages=2))

    assert result.events_processed == 2
    assert result.final_state.executed_buy_volume == Decimal("2")


def test_receiver_expands_ordered_micro_batch_and_preserves_single_frames() -> None:
    """Protocol 1.2 batches and legacy frames use the same causal event path."""
    depth = format_depth_update(
        timestamp=100,
        symbol="MNQ",
        side="bid",
        price="100.00",
        previous_size="0",
        new_size="10",
        stream_sequence=1,
    )
    trade = format_trade(
        timestamp_ns=101,
        price="100.00",
        size="2",
        aggressor_side="sell",
        instrument="MNQ",
        sequence_id=1,
        stream_sequence=2,
    )
    batch = json.dumps({
        "type": "event_batch",
        "protocol_version": "1.2",
        "event_count": 2,
        "events": [depth, trade],
    })

    assert decode_stream_messages(event_to_json(depth)) == (depth,)
    assert decode_stream_messages(batch) == (depth, trade)
    result = asyncio.run(consume_market_stream(MockWebSocketClient([batch])))
    assert result.events_processed == 2
    assert result.final_state.best_bid == Decimal("100.00")
    assert result.final_state.executed_sell_volume == Decimal("2")


def _batch_with_one_bad_item() -> str:
    good_depth = format_depth_update(
        timestamp=100, symbol="MNQ", side="bid", price="100.00",
        previous_size="0", new_size="10")
    # A zero-size trade: matches the trade key set but fails size > 0. Built as a
    # raw dict because format_trade would reject it at construction.
    bad_trade = {"type": "trade", "timestamp_ns": 101, "price": "100.25", "size": "0",
                 "aggressor_side": "buy", "instrument": "MNQ", "sequence_id": 1}
    return json.dumps({"type": "event_batch", "protocol_version": "1.2",
                       "event_count": 2, "events": [good_depth, bad_trade]})


def test_resilient_parser_keeps_good_events_and_reports_the_bad_item() -> None:
    """One bad item in a batch must not discard its well-formed siblings."""
    events, errors = parse_stream_messages_resilient(_batch_with_one_bad_item())
    assert len(events) == 1 and events[0]["type"] == "depth_update"
    assert errors == ("event batch item 1: size must be greater than 0",)
    # Strict parsing stays all-or-nothing: the whole batch is rejected.
    with pytest.raises(EventSchemaError, match="item 1"):
        parse_stream_messages(_batch_with_one_bad_item())


def test_consume_keeps_good_events_from_a_partially_malformed_batch() -> None:
    """The capture path records the good depth update and counts the bad trade."""
    reasons: list[str] = []
    result = asyncio.run(consume_market_stream(
        MockWebSocketClient([_batch_with_one_bad_item()]), on_schema_error=reasons.append))
    assert result.events_processed == 1  # the good depth update was NOT lost
    assert result.final_state.best_bid == Decimal("100.00")
    assert reasons == ["event batch item 1: size must be greater than 0"]
    # Without an error sink (strict), the same batch is fatal, as before.
    with pytest.raises(EventSchemaError):
        asyncio.run(consume_market_stream(MockWebSocketClient([_batch_with_one_bad_item()])))


@pytest.mark.parametrize(
    "change, expected",
    [
        ({"event_count": 3}, "does not match"),
        ({"protocol_version": "2.0"}, "supported major version 1"),
        ({"events": [] , "event_count": 0}, "between 1 and"),
    ],
)
def test_receiver_rejects_malformed_or_incompatible_micro_batch(
    change: dict[str, object],
    expected: str,
) -> None:
    """Bad batch envelopes fail before any contained event changes state."""
    event = format_trade(
        timestamp_ns=1,
        price="100.25",
        size="1",
        aggressor_side="buy",
        instrument="MNQ",
        sequence_id=1,
    )
    envelope: dict[str, object] = {
        "type": "event_batch",
        "protocol_version": "1.2",
        "event_count": 1,
        "events": [event],
    }
    envelope.update(change)
    with pytest.raises(EventSchemaError, match=expected):
        decode_stream_messages(json.dumps(envelope))


def test_single_event_decoder_refuses_to_silently_truncate_batch() -> None:
    """Legacy single-event callers cannot accidentally ignore batched events."""
    event = format_trade(
        timestamp_ns=1,
        price="100.25",
        size="1",
        aggressor_side="buy",
        instrument="MNQ",
        sequence_id=1,
    )
    batch = json.dumps({
        "type": "event_batch",
        "protocol_version": "1.2",
        "event_count": 2,
        "events": [event, {**event, "timestamp_ns": 2, "sequence_id": 2}],
    })
    with pytest.raises(EventSchemaError, match="use parse_stream_messages"):
        decode_stream_message(batch)


def test_receiver_raises_for_invalid_websocket_message() -> None:
    """Invalid WebSocket payloads fail clearly before state or recorder mutation."""
    with pytest.raises(EventSchemaError, match="valid JSON"):
        asyncio.run(consume_market_stream(MockWebSocketClient(["not-json"])))


def test_decode_market_message_returns_validated_event() -> None:
    """Single-message decoding is available for callers that manage their own socket loop."""
    event = format_trade(
        timestamp_ns=10,
        price="100.25",
        size="1",
        aggressor_side="buy",
        instrument="MNQ",
        sequence_id=1,
    )

    assert decode_market_message(event_to_json(event)) == event


def test_receiver_records_java_control_events_without_market_state_mutation(tmp_path: Path) -> None:
    """Java bridge control events are recorded separately and do not touch MarketState."""
    timestamp_ns = 1_783_689_600 * 1_000_000_000
    messages = [
        json_payload(
            {
                "type": "heartbeat",
                "timestamp_ns": timestamp_ns,
                "session_id": "session_test",
                "alias": "MNQ",
                "symbol": "MNQ",
                "source_mode": "historical",
                "addon_version": "0.1.0",
                "dropped_message_count": 0,
            },
        ),
        event_to_json(
            format_depth_update(
                timestamp=timestamp_ns + 1,
                symbol="MNQ",
                side="bid",
                price="100.00",
                previous_size="0",
                new_size="10",
            ),
        ),
        json_payload(
            {
                "type": "data_gap",
                "timestamp_ns": timestamp_ns + 2,
                "reason": "bounded queue overflow",
                "dropped_message_count": 1,
            },
        ),
    ]

    from app.database.recorder import MarketSessionRecorder

    recorder = MarketSessionRecorder(root_dir=tmp_path)
    result = asyncio.run(
        consume_market_stream(
            MockWebSocketClient(messages),
            recorder=recorder,
        ),
    )

    assert result.events_processed == 1
    assert result.control_events_processed == 2
    assert result.final_state.best_bid == Decimal("100.00")
    assert recorder.connection_events_path.exists()
    assert recorder.dropped_message_count == 1


def test_control_event_callback_can_enrich_event_before_recorder_persists_it(
    tmp_path: Path,
) -> None:
    """A handshake-validation callback mutating a control event must reach the recorder.

    ``tools/start_receiver.py`` validates the Java bridge handshake inside its
    ``on_control_event`` callback and stamps ``handshake_accepted``/
    ``connection_boundary`` onto the event dict in place *after* deciding the
    bridge is compatible. If the recorder observed the event before that
    callback ran, every real session would be recorded with
    ``handshake_accepted=False`` regardless of what the callback approved -
    the exact bug this test guards against.
    """
    timestamp_ns = 1_783_689_600 * 1_000_000_000
    messages = [
        json_payload(
            {
                "type": "connected",
                "timestamp_ns": timestamp_ns,
                "session_id": "session_test",
                "protocol_version": "1.2",
                "provider": "bookmap",
                "stream_id": "stream-test",
                "connection_id": "connection-test",
                "capabilities": "aggregated_depth,trades,aggressor_side,source_timestamps",
            },
        ),
    ]

    from app.database.recorder import MarketSessionRecorder

    recorder = MarketSessionRecorder(root_dir=tmp_path)

    def _enrich(event: dict[str, object]) -> None:
        # Simulate the real handshake-acceptance callback: it decides the
        # bridge is compatible only after inspecting the event, then mutates
        # the same dict in place before recording happens.
        event["handshake_accepted"] = True
        event["connection_boundary"] = "initial"

    asyncio.run(
        consume_market_stream(
            MockWebSocketClient(messages),
            recorder=recorder,
            on_control_event=_enrich,
        ),
    )

    assert recorder.handshake_accepted is True


def test_receiver_invokes_market_and_control_callbacks() -> None:
    """The receiver can feed the automatic runtime while preserving normal recording behavior."""
    messages = [
        json_payload({"type": "connected", "timestamp_ns": 10}),
        event_to_json(
            format_depth_update(
                timestamp=11,
                symbol="MNQU6",
                side="bid",
                price="100.00",
                previous_size="0",
                new_size="10",
            ),
        ),
    ]
    market_events: list[dict[str, object]] = []
    control_events: list[dict[str, object]] = []

    result = asyncio.run(
        consume_market_stream(
            MockWebSocketClient(messages),
            on_market_event=lambda event: market_events.append(dict(event)),
            on_control_event=lambda event: control_events.append(dict(event)),
        ),
    )

    assert result.events_processed == 1
    assert result.control_events_processed == 1
    assert control_events[0]["type"] == "connected"
    assert market_events[0]["symbol"] == "MNQU6"


def test_decode_stream_message_accepts_java_heartbeat() -> None:
    """Single-message stream decoding is available for mixed Java bridge payloads."""
    event = decode_stream_message(
        json_payload(
            {
                "type": "heartbeat",
                "timestamp_ns": 10,
                "source_mode": "live",
            },
        ),
    )

    assert event["type"] == "heartbeat"
    assert event["timestamp_ns"] == 10


def test_receiver_and_recorder_do_not_reference_execution_or_broker_code() -> None:
    """The data bridge remains structurally separate from order execution modules."""
    root = Path(__file__).resolve().parents[1]
    checked_files = (
        root / "app" / "market" / "receiver.py",
        root / "app" / "database" / "recorder.py",
        root / "tools" / "start_receiver.py",
    )
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in checked_files)

    assert "app.execution" not in combined
    assert "tradovate" not in combined
    assert "broker" not in combined


def json_payload(event: dict[str, object]) -> str:
    """Serialize a raw test event payload."""
    import json

    return json.dumps(event)
