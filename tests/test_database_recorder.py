"""Tests for raw market-event Parquet recording."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq

from app.database.recorder import (
    MarketEventRecorder,
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


def _timestamp_ns(year: int, month: int, day: int, hour: int, minute: int) -> int:
    event_time = datetime(year, month, day, hour, minute, tzinfo=UTC)
    return int(event_time.timestamp()) * 1_000_000_000


def _rows(path: Path) -> list[dict[str, object]]:
    return pq.read_table(path).to_pylist()
