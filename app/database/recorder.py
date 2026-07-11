"""Parquet recorder for raw market depth and trade events."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from bookmap_addon.events import RawMarketEvent, event_to_json, parse_event_message

NANOSECONDS_PER_SECOND = 1_000_000_000

DEPTH_SCHEMA = pa.schema(
    [
        ("timestamp", pa.int64()),
        ("symbol", pa.string()),
        ("side", pa.string()),
        ("price", pa.string()),
        ("previous_size", pa.string()),
        ("new_size", pa.string()),
    ],
)
TRADE_SCHEMA = pa.schema(
    [
        ("timestamp_ns", pa.int64()),
        ("price", pa.string()),
        ("size", pa.string()),
        ("aggressor_side", pa.string()),
        ("instrument", pa.string()),
        ("sequence_id", pa.int64()),
    ],
)


@dataclass(frozen=True, slots=True)
class RecorderWriteSummary:
    """Counts of raw market events written by a recorder call."""

    depth_updates: int
    trades: int


@dataclass(frozen=True, slots=True)
class MarketEventRecorder:
    """Append raw depth and trade events into date-partitioned Parquet files."""

    root_dir: Path = Path("data/raw")
    partition_timezone: timezone = UTC

    def record(self, event: Mapping[str, object]) -> Path:
        """Append one raw market event and return the Parquet file path written."""
        normalized_event = normalize_market_event(event)
        event_kind = market_event_kind(normalized_event)
        partition = partition_date_for_event(normalized_event, self.partition_timezone)
        partition_dir = self.root_dir / partition
        partition_dir.mkdir(parents=True, exist_ok=True)

        if event_kind == "depth":
            output_path = partition_dir / "depth.parquet"
            _append_parquet(output_path, DEPTH_SCHEMA, [_depth_row(normalized_event)])
            return output_path

        output_path = partition_dir / "trades.parquet"
        _append_parquet(output_path, TRADE_SCHEMA, [_trade_row(normalized_event)])
        return output_path

    def record_many(self, events: Iterable[Mapping[str, object]]) -> RecorderWriteSummary:
        """Append multiple raw events and return counts by event type."""
        depth_updates = 0
        trades = 0
        for event in events:
            output_path = self.record(event)
            if output_path.name == "depth.parquet":
                depth_updates += 1
            else:
                trades += 1
        return RecorderWriteSummary(depth_updates=depth_updates, trades=trades)


def normalize_market_event(event: Mapping[str, object]) -> RawMarketEvent:
    """Return a validated event that matches one of the Task 5 schemas exactly."""
    return parse_event_message(event_to_json(dict(event)))


def market_event_kind(event: Mapping[str, object]) -> str:
    """Return ``depth`` or ``trade`` for a normalized Task 5 market event."""
    if event.get("type") == "depth_update":
        return "depth"
    if "timestamp_ns" in event and "sequence_id" in event:
        return "trade"
    raise ValueError("event must be a Task 5 depth_update or trade event")


def partition_date_for_event(event: Mapping[str, object], target_timezone: timezone = UTC) -> str:
    """Return the YYYY-MM-DD partition date for a raw market event timestamp."""
    event_kind = market_event_kind(event)
    timestamp_ns = int(event["timestamp"] if event_kind == "depth" else event["timestamp_ns"])
    if timestamp_ns < 0:
        raise ValueError("event timestamp must be non-negative")

    seconds, nanoseconds = divmod(timestamp_ns, NANOSECONDS_PER_SECOND)
    event_time = datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=nanoseconds // 1000)
    return event_time.astimezone(target_timezone).date().isoformat()


def _append_parquet(path: Path, schema: pa.Schema, rows: list[dict[str, object]]) -> None:
    new_table = pa.Table.from_pylist(rows, schema=schema)
    if path.exists():
        existing_table = pq.read_table(path, schema=schema)
        new_table = pa.concat_tables([existing_table, new_table])
    pq.write_table(new_table, path)


def _depth_row(event: Mapping[str, object]) -> dict[str, object]:
    return {
        "timestamp": int(event["timestamp"]),
        "symbol": str(event["symbol"]),
        "side": str(event["side"]),
        "price": str(event["price"]),
        "previous_size": str(event["previous_size"]),
        "new_size": str(event["new_size"]),
    }


def _trade_row(event: Mapping[str, object]) -> dict[str, object]:
    return {
        "timestamp_ns": int(event["timestamp_ns"]),
        "price": str(event["price"]),
        "size": str(event["size"]),
        "aggressor_side": str(event["aggressor_side"]),
        "instrument": str(event["instrument"]),
        "sequence_id": int(event["sequence_id"]),
    }
