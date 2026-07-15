"""Tests for the streaming, bounded, causal session replay loader."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from app.database.recorder import MarketSessionRecorder
from app.research.replay_loader import stream_session_events, stream_session_events_with_stats


def _fixture_session(tmp_path: Path) -> Path:
    """Write a small finalized session (real recorder schema) and return its dir."""
    rec = MarketSessionRecorder(root_dir=tmp_path, session_start_utc=datetime(2026, 7, 15, 0, 22, tzinfo=UTC))
    base = 1_752_537_751_000_000_000
    # Interleave depth and trades, incl. a same-timestamp depth+trade collision.
    rec.record({"type": "depth_update", "timestamp": base + 0, "symbol": "MNQ", "side": "bid",
                "price": "29500.00", "previous_size": "0", "new_size": "10"})
    rec.record({"timestamp_ns": base + 0, "price": "29500.25", "size": "2", "aggressor_side": "buy",
                "instrument": "MNQ", "sequence_id": 1})  # same ns as the depth above
    rec.record({"type": "depth_update", "timestamp": base + 2, "symbol": "MNQ", "side": "ask",
                "price": "29500.50", "previous_size": "0", "new_size": "8"})
    rec.record({"timestamp_ns": base + 3, "price": "29500.50", "size": "1", "aggressor_side": "sell",
                "instrument": "MNQ", "sequence_id": 2})
    rec.finalize(clean_shutdown=True)
    return rec.session_dir


def test_events_stream_in_causal_timestamp_order(tmp_path: Path) -> None:
    session_dir = _fixture_session(tmp_path)
    events = list(stream_session_events(session_dir))
    timestamps = [e.timestamp_ns for e in events]
    assert timestamps == sorted(timestamps)  # non-decreasing
    assert len(events) == 4


def test_same_timestamp_collision_is_counted_not_hidden(tmp_path: Path) -> None:
    session_dir = _fixture_session(tmp_path)
    iterator, stats = stream_session_events_with_stats(session_dir)
    events = list(iterator)  # must consume fully
    assert stats.depth_events == 2
    assert stats.trade_events == 2
    # The base+0 depth and base+0 trade collide at the same nanosecond.
    assert stats.same_timestamp_collisions == 1
    assert stats.receive_order_available is True
    assert stats.ordering_ambiguous is False
    assert len(events) == 4


def test_streaming_uses_batches_not_full_load(tmp_path: Path) -> None:
    """A tiny batch size still yields all events (proves batch iteration)."""
    session_dir = _fixture_session(tmp_path)
    events = list(stream_session_events(session_dir, batch_rows=1))
    assert len(events) == 4


def test_active_closed_parts_are_readable_without_final_single_file(tmp_path: Path) -> None:
    recorder = MarketSessionRecorder(
        root_dir=tmp_path,
        session_start_utc=datetime(2026, 7, 15, 0, 22, tzinfo=UTC),
    )
    recorder.record({
        "type": "depth_update", "timestamp": 10, "symbol": "MNQ", "side": "bid",
        "price": "100", "previous_size": "0", "new_size": "10",
    })
    recorder.flush()
    assert not recorder.depth_path.exists()
    events = list(stream_session_events(recorder.session_dir, batch_rows=1))
    assert len(events) == 1 and events[0].kind == "depth"


def test_old_recording_falls_back_and_marks_collision_ambiguous(tmp_path: Path) -> None:
    session_dir = _fixture_session(tmp_path)
    for name in ("depth.parquet", "trades.parquet"):
        path = session_dir / name
        pq.write_table(pq.read_table(path).drop(["receive_sequence"]), path)
    iterator, stats = stream_session_events_with_stats(session_dir)
    list(iterator)
    assert stats.ordering_mode == "timestamp_fallback"
    assert stats.ordering_ambiguous is True


def test_receive_order_is_causal_and_timestamp_regression_is_reported(tmp_path: Path) -> None:
    recorder = MarketSessionRecorder(
        root_dir=tmp_path,
        session_start_utc=datetime(2026, 7, 15, 0, 22, tzinfo=UTC),
    )
    recorder.record({
        "type": "depth_update", "timestamp": 100, "symbol": "MNQ", "side": "bid",
        "price": "100", "previous_size": "0", "new_size": "10",
    })
    recorder.record({
        "type": "trade", "timestamp_ns": 90, "price": "100", "size": "1",
        "aggressor_side": "buy", "instrument": "MNQ", "sequence_id": 1,
    })
    recorder.finalize(clean_shutdown=True)
    iterator, stats = stream_session_events_with_stats(recorder.session_dir)
    events = list(iterator)
    assert [event.receive_sequence for event in events] == [1, 2]
    assert stats.timestamp_regressions == 1
    assert stats.continuity_ok is False


def test_trade_sequence_gap_is_counted(tmp_path: Path) -> None:
    session_dir = _fixture_session(tmp_path)
    path = session_dir / "trades.parquet"
    table = pq.read_table(path)
    rows = table.to_pylist()
    rows[1]["sequence_id"] = 5
    pq.write_table(type(table).from_pylist(rows, schema=table.schema), path)
    iterator, stats = stream_session_events_with_stats(session_dir)
    list(iterator)
    assert stats.trade_sequence_gaps == 1
    assert stats.missed_trade_events == 3
    assert stats.continuity_ok is False
