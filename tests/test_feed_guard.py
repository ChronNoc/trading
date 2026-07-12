"""Tests for the Bookmap feed guard: robustness, gap detection, and dual health flags."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.market.feed_guard import (
    MODE_FULL,
    MODE_OFFLINE,
    MODE_STALLED,
    MODE_TRADES_ONLY,
    BookSnapshotRing,
    EventReplayBuffer,
    ExponentialBackoff,
    FeedGuard,
    FeedGuardConfig,
    SequenceGapDetector,
)
from app.market.receiver import consume_market_stream


class FakeClock:
    """Deterministic nanosecond clock for guard tests."""

    def __init__(self, start_ns: int = 1_000_000_000_000) -> None:
        self.now_ns = start_ns

    def __call__(self) -> int:
        return self.now_ns

    def advance_seconds(self, seconds: float) -> None:
        self.now_ns += int(seconds * 1_000_000_000)


def _depth(timestamp_ns: int) -> dict[str, object]:
    return {
        "type": "depth_update",
        "timestamp": timestamp_ns,
        "symbol": "MNQ",
        "side": "bid",
        "price": "100.00",
        "previous_size": "0",
        "new_size": "10",
    }


def _trade(timestamp_ns: int, sequence_id: int) -> dict[str, object]:
    return {
        "timestamp_ns": timestamp_ns,
        "price": "100.25",
        "size": "1",
        "aggressor_side": "buy",
        "instrument": "MNQ",
        "sequence_id": sequence_id,
    }


def _connected_guard(clock: FakeClock, **config_overrides: object) -> FeedGuard:
    guard = FeedGuard(FeedGuardConfig(**config_overrides), now_ns=clock)  # type: ignore[arg-type]
    guard.handle_control_event({"type": "connected"})
    return guard


def test_backoff_grows_exponentially_and_resets() -> None:
    """Delays double up to the cap, and reset() starts over."""
    backoff = ExponentialBackoff(base_seconds=0.25, factor=2.0, max_seconds=2.0)

    delays = [backoff.next_delay() for _ in range(5)]

    assert delays == [0.25, 0.5, 1.0, 2.0, 2.0]
    assert backoff.attempts == 5
    backoff.reset()
    assert backoff.next_delay() == 0.25


def test_backoff_rejects_invalid_parameters() -> None:
    """Nonsensical backoff parameters raise immediately."""
    with pytest.raises(ValueError):
        ExponentialBackoff(base_seconds=0)
    with pytest.raises(ValueError):
        ExponentialBackoff(base_seconds=1.0, max_seconds=0.5)


def test_sequence_gap_detector_counts_missed_events() -> None:
    """Skipped sequence ids are detected and totaled."""
    detector = SequenceGapDetector()

    assert detector.observe(1) == 0
    assert detector.observe(2) == 0
    assert detector.observe(5) == 2
    assert detector.gap_count == 1
    assert detector.missed_events == 2


def test_replay_buffer_bounds_and_drains_in_order() -> None:
    """The buffer keeps arrival order and drops the oldest on overflow."""
    buffer = EventReplayBuffer(max_events=2)
    buffer.buffer({"n": 1})
    buffer.buffer({"n": 2})
    buffer.buffer({"n": 3})

    drained = buffer.drain()

    assert [event["n"] for event in drained] == [2, 3]
    assert buffer.dropped_events == 1
    assert len(buffer) == 0


def test_snapshot_ring_keeps_newest_and_writes_jsonl(tmp_path: Path) -> None:
    """The ring is bounded and dumps valid JSONL for debugging."""
    ring = BookSnapshotRing(max_entries=3)
    for index in range(5):
        ring.record(_depth(1_000 + index))

    assert len(ring) == 3
    assert [entry["timestamp"] for entry in ring.entries()] == [1_002, 1_003, 1_004]

    path = ring.write_jsonl(tmp_path / "debug" / "book_ring.jsonl")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[-1])["timestamp"] == 1_004


def test_guard_rejects_out_of_order_depth_loudly() -> None:
    """Depth updates that go backwards in time are rejected and counted."""
    clock = FakeClock()
    guard = _connected_guard(clock, source_mode="replay")

    assert guard.ingest_market_event(_depth(2_000)) == (True, None)
    accepted, reason = guard.ingest_market_event(_depth(1_000))

    assert accepted is False
    assert reason is not None and "out-of-order" in reason
    assert guard.status().out_of_order_events == 1
    assert any("out-of-order" in entry for entry in guard.rejections)


def test_guard_counts_trade_sequence_gaps_without_rejecting() -> None:
    """Sequence gaps are counted loudly but the trade still flows."""
    clock = FakeClock()
    guard = _connected_guard(clock, source_mode="replay")

    guard.ingest_market_event(_trade(1_000, sequence_id=1))
    accepted, _ = guard.ingest_market_event(_trade(2_000, sequence_id=4))

    assert accepted is True
    status = guard.status()
    assert status.sequence_gaps == 1
    assert status.missed_events == 2


def test_guard_clock_drift_flags_live_but_not_replay() -> None:
    """Clock drift trips only in live mode; replay timestamps are exempt."""
    clock = FakeClock()
    stale_timestamp = clock.now_ns - 10_000_000_000

    live_guard = _connected_guard(clock, source_mode="live")
    live_guard.ingest_market_event(_trade(stale_timestamp, sequence_id=1))
    live_status = live_guard.status()
    assert live_status.clock_drift_alerts == 1
    assert live_status.data_quality_ok is False
    assert "drifting" in " ".join(live_status.reasons)

    replay_guard = _connected_guard(clock, source_mode="replay")
    replay_guard.ingest_market_event(_trade(stale_timestamp, sequence_id=1))
    assert replay_guard.status().clock_drift_alerts == 0


def test_guard_separates_connection_health_from_data_quality() -> None:
    """Connected-but-stale and fresh-but-disconnected are different failures."""
    clock = FakeClock()
    guard = _connected_guard(clock, source_mode="replay", stale_after_ns=2_000_000_000)
    guard.ingest_market_event(_depth(clock.now_ns))
    guard.ingest_market_event(_trade(clock.now_ns, sequence_id=1))

    fresh = guard.status()
    assert fresh.mode == MODE_FULL
    assert fresh.connection_healthy is True
    assert fresh.data_quality_ok is True
    assert fresh.risk_connection_ok is True

    clock.advance_seconds(5)
    stale = guard.status()
    assert stale.mode == MODE_STALLED
    assert stale.connection_healthy is True
    assert stale.data_quality_ok is False
    assert stale.risk_connection_ok is False

    guard.handle_control_event({"type": "disconnected"})
    offline = guard.status()
    assert offline.mode == MODE_OFFLINE
    assert offline.connection_healthy is False
    assert offline.risk_connection_ok is False


def test_guard_trades_only_failover_keeps_reduced_confidence_mode() -> None:
    """Depth stalling with fresh trades degrades to trades-only, not failure."""
    clock = FakeClock()
    guard = _connected_guard(clock, source_mode="replay", stale_after_ns=2_000_000_000)
    guard.ingest_market_event(_depth(clock.now_ns))

    clock.advance_seconds(5)
    guard.ingest_market_event(_trade(clock.now_ns, sequence_id=1))

    status = guard.status()
    assert status.mode == MODE_TRADES_ONLY
    assert status.data_quality_ok is True
    assert status.risk_connection_ok is True
    assert "trades-only" in " ".join(status.reasons)


def test_guard_malformed_messages_are_counted_loudly() -> None:
    """Malformed messages increment the counter and the rejection log."""
    clock = FakeClock()
    guard = _connected_guard(clock, source_mode="replay")

    guard.record_malformed("price used a JSON float")

    status = guard.status()
    assert status.malformed_events == 1
    assert any("malformed" in entry for entry in guard.rejections)


def test_replay_and_live_guards_share_one_code_path() -> None:
    """The same guard class and ingest path serve live and replay sources."""
    clock = FakeClock()
    live = _connected_guard(clock, source_mode="live")
    replay = _connected_guard(clock, source_mode="replay")

    event = _trade(clock.now_ns, sequence_id=1)
    assert live.ingest_market_event(event) == (True, None)
    assert replay.ingest_market_event(event) == (True, None)
    assert type(live) is type(replay)


class _ListStream:
    """Async stream yielding preset websocket payloads."""

    def __init__(self, payloads: list[str]) -> None:
        self._payloads = payloads

    def __aiter__(self):
        async def generate():
            for payload in self._payloads:
                yield payload

        return generate()


def test_receiver_stream_routes_through_guard(tmp_path: Path) -> None:
    """Malformed and out-of-order messages are counted; good events still flow."""
    clock = FakeClock()
    guard = _connected_guard(clock, source_mode="replay")
    payloads = [
        json.dumps(_depth(2_000)),
        "{not valid json",
        json.dumps(_depth(1_000)),
        json.dumps(_trade(3_000, sequence_id=1)),
    ]
    seen: list[dict[str, object]] = []

    result = asyncio.run(
        consume_market_stream(
            _ListStream(payloads),
            state_store=None,
            on_market_event=lambda event: seen.append(dict(event)),
            event_filter=lambda event: guard.ingest_market_event(event)[0],
            on_schema_error=guard.record_malformed,
        ),
    )

    assert result.events_processed == 2
    assert len(seen) == 2
    status = guard.status()
    assert status.malformed_events == 1
    assert status.out_of_order_events == 1
    assert len(ring_free := guard.snapshot_ring.entries()) == 1
    assert ring_free[0]["timestamp"] == 2_000


def test_receiver_stream_without_schema_handler_still_raises(tmp_path: Path) -> None:
    """Back-compat: malformed messages raise when no handler is registered."""
    from bookmap_addon.events import EventSchemaError

    with pytest.raises(EventSchemaError):
        asyncio.run(
            consume_market_stream(
                _ListStream(["{not valid json"]),
                state_store=None,
            ),
        )
