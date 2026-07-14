"""Tests for raw market-event Parquet recording."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from app.database.recorder import (
    MarketEventRecorder,
    MarketSessionRecorder,
    market_event_kind,
    normalize_market_event,
    partition_date_for_event,
)
from bookmap_addon.events import format_depth_update, format_trade


def test_recorder_writes_trades_and_depth_to_date_partitions(tmp_path: Path) -> None:
    """Raw trades and depth updates are written to their date-partitioned Parquet files."""
    timestamp_ns = _timestamp_ns(2026, 7, 10, 14, 30)
    depth = format_depth_update(
        timestamp=timestamp_ns,
        symbol="MNQ",
        side="bid",
        price="100.00",
        previous_size="0",
        new_size="10",
    )
    trade = format_trade(
        timestamp_ns=timestamp_ns,
        price="100.25",
        size="3",
        aggressor_side="buy",
        instrument="MNQ",
        sequence_id=1,
    )
    recorder = MarketEventRecorder(root_dir=tmp_path)

    summary = recorder.record_many((depth, trade))

    assert summary.depth_updates == 1
    assert summary.trades == 1
    depth_path = tmp_path / "2026-07-10" / "depth.parquet"
    trades_path = tmp_path / "2026-07-10" / "trades.parquet"
    assert _rows(depth_path) == [
        {
            "timestamp": timestamp_ns,
            "symbol": "MNQ",
            "side": "bid",
            "price": "100.00",
            "previous_size": "0",
            "new_size": "10",
        },
    ]
    assert _rows(trades_path) == [
        {
            "timestamp_ns": timestamp_ns,
            "price": "100.25",
            "size": "3",
            "aggressor_side": "buy",
            "instrument": "MNQ",
            "sequence_id": 1,
        },
    ]


def test_recorder_appends_to_existing_parquet_file(tmp_path: Path) -> None:
    """Recording multiple events for the same date appends rows to the partition file."""
    recorder = MarketEventRecorder(root_dir=tmp_path)
    timestamp_ns = _timestamp_ns(2026, 7, 10, 15, 0)

    recorder.record(
        format_trade(
            timestamp_ns=timestamp_ns,
            price="100.25",
            size="1",
            aggressor_side="buy",
            instrument="MNQ",
            sequence_id=1,
        ),
    )
    recorder.record(
        format_trade(
            timestamp_ns=timestamp_ns + 1,
            price="100.50",
            size="2",
            aggressor_side="sell",
            instrument="MNQ",
            sequence_id=2,
        ),
    )

    assert _rows(tmp_path / "2026-07-10" / "trades.parquet") == [
        {
            "timestamp_ns": timestamp_ns,
            "price": "100.25",
            "size": "1",
            "aggressor_side": "buy",
            "instrument": "MNQ",
            "sequence_id": 1,
        },
        {
            "timestamp_ns": timestamp_ns + 1,
            "price": "100.50",
            "size": "2",
            "aggressor_side": "sell",
            "instrument": "MNQ",
            "sequence_id": 2,
        },
    ]


def test_recorder_partitions_by_utc_event_date(tmp_path: Path) -> None:
    """Events with different UTC dates are written into separate partition folders."""
    recorder = MarketEventRecorder(root_dir=tmp_path)

    first_path = recorder.record(
        format_trade(
            timestamp_ns=_timestamp_ns(2026, 7, 10, 23, 59),
            price="100.25",
            size="1",
            aggressor_side="buy",
            instrument="MNQ",
            sequence_id=1,
        ),
    )
    second_path = recorder.record(
        format_trade(
            timestamp_ns=_timestamp_ns(2026, 7, 11, 0, 0),
            price="100.25",
            size="1",
            aggressor_side="buy",
            instrument="MNQ",
            sequence_id=2,
        ),
    )

    assert first_path == tmp_path / "2026-07-10" / "trades.parquet"
    assert second_path == tmp_path / "2026-07-11" / "trades.parquet"


def test_event_helpers_normalize_and_classify_events() -> None:
    """Recorder helpers validate exact event schemas and classify event type."""
    event = normalize_market_event(
        {
            "timestamp_ns": 1,
            "price": "100.25",
            "size": "2",
            "aggressor_side": "buyer",
            "instrument": "MNQ",
            "sequence_id": 7,
        },
    )

    assert event["aggressor_side"] == "buy"
    assert market_event_kind(event) == "trade"
    assert partition_date_for_event(event) == "1970-01-01"


def test_session_recorder_writes_unique_session_manifest_events_and_parquet(tmp_path: Path) -> None:
    """A Java bridge run is recorded under a unique session folder with manifest metadata."""
    start = datetime(2026, 7, 10, 14, 30, tzinfo=UTC)
    recorder = MarketSessionRecorder(root_dir=tmp_path, session_start_utc=start)
    timestamp_ns = _timestamp_ns(2026, 7, 10, 14, 30)

    recorder.record_control_event(
        {
            "type": "connected",
            "timestamp_ns": timestamp_ns,
            "session_id": "java-session",
            "alias": "MNQ",
            "symbol": "MNQ",
            "source_mode": "historical",
            "addon_version": "0.1.0",
            "dropped_message_count": 0,
        },
    )
    recorder.record(
        {
            "type": "depth_update",
            "timestamp": timestamp_ns,
            "symbol": "MNQ",
            "side": "bid",
            "price": "100.00",
            "previous_size": "0",
            "new_size": "10",
        },
    )
    recorder.record(
        {
            "type": "trade",
            "timestamp_ns": timestamp_ns + 1,
            "price": "100.25",
            "size": "3",
            "aggressor_side": "buy",
            "instrument": "MNQ",
            "sequence_id": 1,
        },
    )
    recorder.record_control_event({"type": "realtime_started", "timestamp_ns": timestamp_ns + 2})
    recorder.record_control_event({"type": "session_ended", "timestamp_ns": timestamp_ns + 3})
    recorder.flush()

    session_dir = tmp_path / "2026-07-10" / "session_20260710T143000Z"
    assert recorder.session_dir == session_dir
    assert _rows(session_dir / "depth.parquet")[0]["new_size"] == "10"
    assert _rows(session_dir / "trades.parquet")[0]["sequence_id"] == 1
    assert len((session_dir / "connection_events.jsonl").read_text(encoding="utf-8").splitlines()) == 3
    manifest = json.loads((session_dir / "session_manifest.json").read_text(encoding="utf-8"))
    assert manifest["alias"] == "MNQ"
    assert manifest["source_mode"] == "live"
    assert manifest["event_counts"] == {
        "depth_updates": 1,
        "trades": 1,
        "connection_events": 3,
    }
    assert manifest["clean_shutdown"] is True
    assert manifest["valid_for_analysis"] is True


def test_session_recorder_marks_data_gap_session_invalid(tmp_path: Path) -> None:
    """Data gaps and dropped messages make the session invalid for later analysis."""
    recorder = MarketSessionRecorder(
        root_dir=tmp_path,
        session_start_utc=datetime(2026, 7, 10, 14, 30, tzinfo=UTC),
    )

    recorder.record_control_event(
        {
            "type": "data_gap",
            "timestamp_ns": _timestamp_ns(2026, 7, 10, 14, 31),
            "reason": "bounded queue overflow",
            "dropped_message_count": 2,
        },
    )
    recorder.record_control_event({"type": "session_ended", "timestamp_ns": _timestamp_ns(2026, 7, 10, 14, 32)})

    manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
    assert manifest["dropped_message_count"] == 2
    assert manifest["continuity_status"] == "bounded queue overflow"
    assert manifest["clean_shutdown"] is True
    assert manifest["valid_for_analysis"] is False


def test_session_recorder_marks_delayed_bookmap_data_not_live_decision_ready(tmp_path: Path) -> None:
    """Free Bookmap delayed sessions are recorded but not marked usable for live decisions."""
    recorder = MarketSessionRecorder(
        root_dir=tmp_path,
        session_start_utc=datetime(2026, 7, 10, 14, 30, tzinfo=UTC),
    )

    recorder.record_control_event(
        {
            "type": "delayed_mode",
            "timestamp_ns": _timestamp_ns(2026, 7, 10, 14, 30),
            "source_mode": "delayed",
            "delay_minutes": 15,
            "reason": "Bookmap free delayed data feed",
        },
    )
    recorder.record_control_event({"type": "realtime_started", "timestamp_ns": _timestamp_ns(2026, 7, 10, 14, 31)})
    recorder.record_control_event({"type": "session_ended", "timestamp_ns": _timestamp_ns(2026, 7, 10, 14, 32)})

    manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
    assert manifest["source_mode"] == "delayed"
    assert manifest["data_delay_minutes"] == 15
    assert manifest["analysis_scope"] == "delayed_market_data"
    assert manifest["valid_for_live_decisions"] is False


def test_session_recorder_creates_unique_session_directories(tmp_path: Path) -> None:
    """Two receiver connections in the same second do not write into the same folder."""
    start = datetime(2026, 7, 10, 14, 30, tzinfo=UTC)

    first = MarketSessionRecorder(root_dir=tmp_path, session_start_utc=start)
    second = MarketSessionRecorder(root_dir=tmp_path, session_start_utc=start)

    assert first.session_dir.name == "session_20260710T143000Z"
    assert second.session_dir.name == "session_20260710T143000Z_0001"


def _timestamp_ns(year: int, month: int, day: int, hour: int, minute: int) -> int:
    event_time = datetime(year, month, day, hour, minute, tzinfo=UTC)
    return int(event_time.timestamp()) * 1_000_000_000


def _rows(path: Path) -> list[dict[str, object]]:
    return pq.read_table(path).to_pylist()


def test_session_recorder_flush_streams_rows_and_finalize_makes_readable(tmp_path: Path) -> None:
    """Buffered rows stream on flush; the partition is readable after finalize."""
    start = datetime(2026, 7, 10, 14, 30, tzinfo=UTC)
    recorder = MarketSessionRecorder(root_dir=tmp_path, session_start_utc=start)
    base_ns = _timestamp_ns(2026, 7, 10, 14, 30)

    for index in range(50):
        recorder.record(
            {
                "type": "depth_update",
                "timestamp": base_ns + index,
                "symbol": "MNQ",
                "side": "bid",
                "price": "100.00",
                "previous_size": "0",
                "new_size": str(index + 1),
            },
        )

    # Below the flush threshold: nothing streamed yet.
    assert not recorder.depth_path.exists()

    # flush() streams a row group but does not write the footer; readable only
    # after finalize() closes the writer.
    recorder.flush()
    recorder.finalize(clean_shutdown=True)
    assert len(_rows(recorder.depth_path)) == 50


def test_session_recorder_batches_large_streams_without_reread(tmp_path: Path) -> None:
    """A large stream records all rows and auto-flushes past the threshold."""
    from app.database.recorder import PARQUET_FLUSH_THRESHOLD

    start = datetime(2026, 7, 10, 14, 30, tzinfo=UTC)
    recorder = MarketSessionRecorder(root_dir=tmp_path, session_start_utc=start)
    base_ns = _timestamp_ns(2026, 7, 10, 14, 30)

    total = PARQUET_FLUSH_THRESHOLD * 3 + 7
    for index in range(total):
        recorder.record(
            {
                "type": "depth_update",
                "timestamp": base_ns + index,
                "symbol": "MNQ",
                "side": "bid",
                "price": "100.00",
                "previous_size": "0",
                "new_size": str((index % 90) + 1),
            },
        )
    recorder.finalize(clean_shutdown=True)

    assert recorder.depth_updates == total
    assert len(_rows(recorder.depth_path)) == total


def test_session_recorder_never_rereads_existing_parquet(tmp_path: Path, monkeypatch) -> None:
    """Acceptance gate: recording streams row groups and never rereads the file."""
    import app.database.recorder as recorder_module

    def _forbidden_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("recorder must not reread the existing Parquet file")

    monkeypatch.setattr(recorder_module.pq, "read_table", _forbidden_read)

    start = datetime(2026, 7, 10, 14, 30, tzinfo=UTC)
    recorder = MarketSessionRecorder(root_dir=tmp_path, session_start_utc=start)
    base_ns = _timestamp_ns(2026, 7, 10, 14, 30)

    # More than several flush batches so a reread would definitely be triggered
    # by the old read-modify-write path.
    for index in range(1500):
        recorder.record(
            {
                "type": "depth_update",
                "timestamp": base_ns + index,
                "symbol": "MNQ",
                "side": "bid",
                "price": "100.00",
                "previous_size": "0",
                "new_size": str((index % 90) + 1),
            },
        )
    recorder.finalize(clean_shutdown=True)

    # read_table is patched to fail, so reading uses ParquetFile metadata only.
    assert pq.ParquetFile(recorder.depth_path).metadata.num_rows == 1500
