"""Receiver side of the Java bridge handshake contract.

The Java forwarder declares a protocol version and capability set on connect and
carries stream/connection ids on every control message. These tests pin the
receiver's consumption of that contract: version gating, capability parsing, and
reconnect detection.
"""

from __future__ import annotations

from app.market.protocol import (
    SUPPORTED_PROTOCOL_MAJOR,
    BridgeHandshake,
    ConnectionTracker,
    parse_handshake,
)

# A handshake exactly as the Java MessageFactory.connected() emits it.
_CONNECTED = {
    "type": "connected",
    "timestamp_ns": 1,
    "protocol_version": "1.2",
    "stream_id": "stream-aaaa",
    "connection_id": "conn-1111",
    "session_id": "session_20260717T140000Z",
    "alias": "MNQU6",
    "symbol": "MNQ",
    "source_mode": "delayed",
    "capabilities": "aggregated_depth,trades,aggressor_side,source_timestamps",
    "provider": "bookmap",
}


def test_a_current_handshake_is_parsed_and_accepted() -> None:
    hs = parse_handshake(_CONNECTED)
    assert hs.compatible is True
    assert hs.protocol_version == "1.2"
    assert hs.provider == "bookmap"
    assert hs.session_id == "session_20260717T140000Z"
    assert "trades" in hs.declares
    assert "aggressor_side" in hs.declares
    assert "mbo_order_by_order" not in hs.declares, "the bridge must not claim MBO"


def test_a_missing_protocol_version_is_incompatible_not_a_crash() -> None:
    """An older bridge predating the handshake must be refused, not misparsed."""
    hs = parse_handshake({"type": "connected", "session_id": "s"})
    assert hs.compatible is False
    assert "no protocol_version" in hs.reason
    assert hs.declares == frozenset()


def test_an_older_minor_version_is_refused() -> None:
    hs = parse_handshake({**_CONNECTED, "protocol_version": "1.1"})
    assert hs.compatible is False
    assert "requires at least 1.2" in hs.reason


def test_a_different_major_version_is_refused() -> None:
    hs = parse_handshake({**_CONNECTED, "protocol_version": "2.0"})
    assert hs.compatible is False
    assert f"major {SUPPORTED_PROTOCOL_MAJOR}" in hs.reason


def test_a_newer_minor_version_is_accepted_as_additive() -> None:
    hs = parse_handshake({**_CONNECTED, "protocol_version": "1.5"})
    assert hs.compatible is True


def test_an_unparseable_version_is_refused() -> None:
    hs = parse_handshake({**_CONNECTED, "protocol_version": "abc"})
    assert hs.compatible is False
    assert "unparseable" in hs.reason


def test_capabilities_are_split_and_trimmed() -> None:
    hs = parse_handshake({**_CONNECTED, "capabilities": " trades , aggregated_depth "})
    assert hs.declares == frozenset({"trades", "aggregated_depth"})


def test_the_first_handshake_is_the_initial_connection() -> None:
    tracker = ConnectionTracker()
    assert tracker.observe(parse_handshake(_CONNECTED)) == "initial"
    assert tracker.reconnects == 0 and tracker.new_streams == 0


def test_same_stream_new_connection_is_a_reconnect() -> None:
    tracker = ConnectionTracker()
    tracker.observe(parse_handshake(_CONNECTED))
    reconnected = parse_handshake({**_CONNECTED, "connection_id": "conn-2222"})
    assert tracker.observe(reconnected) == "reconnect"
    assert tracker.reconnects == 1


def test_a_new_stream_id_is_a_different_bridge_process() -> None:
    """Mid-session, a new stream means the previous one ended without a marker."""
    tracker = ConnectionTracker()
    tracker.observe(parse_handshake(_CONNECTED))
    restarted = parse_handshake({**_CONNECTED, "stream_id": "stream-bbbb", "connection_id": "conn-9"})
    assert tracker.observe(restarted) == "new_stream"
    assert tracker.new_streams == 1


def test_an_identical_repeat_handshake_is_a_duplicate() -> None:
    tracker = ConnectionTracker()
    tracker.observe(parse_handshake(_CONNECTED))
    assert tracker.observe(parse_handshake(_CONNECTED)) == "duplicate"
    assert tracker.reconnects == 0 and tracker.new_streams == 0


def test_control_events_remain_tolerant_of_the_new_fields() -> None:
    """The Python parser must preserve the enriched control envelope unchanged."""
    from bookmap_addon.events import parse_stream_message
    import json

    parsed = parse_stream_message(json.dumps(_CONNECTED))
    assert parsed["type"] == "connected"
    assert parsed["protocol_version"] == "1.2"
    assert parsed["stream_id"] == "stream-aaaa"
    assert parsed["capabilities"].startswith("aggregated_depth")
