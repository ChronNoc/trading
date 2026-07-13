"""Parquet recorder for raw market depth and trade events."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from bookmap_addon.events import (
    RawMarketEvent,
    RawStreamEvent,
    event_to_json,
    is_control_event,
    parse_event_message,
)

NANOSECONDS_PER_SECOND = 1_000_000_000
RECEIVER_VERSION = "0.1.0"
# Buffered rows per Parquet partition before a batched flush. Batching turns
# the recorder's per-event whole-file rewrite from O(n^2) into amortized O(n).
PARQUET_FLUSH_THRESHOLD = 500

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


@dataclass(slots=True)
class MarketSessionRecorder:
    """Append one Bookmap bridge run into a unique raw-data session folder."""

    root_dir: Path = Path("data/raw")
    partition_timezone: timezone = UTC
    session_start_utc: datetime = field(default_factory=lambda: datetime.now(UTC))
    requested_session_id: str | None = None
    session_dir: Path = field(init=False)
    session_id: str = field(init=False)
    depth_updates: int = field(init=False, default=0)
    trades: int = field(init=False, default=0)
    connection_events: int = field(init=False, default=0)
    dropped_message_count: int = field(init=False, default=0)
    alias: str | None = field(init=False, default=None)
    symbol: str | None = field(init=False, default=None)
    addon_version: str | None = field(init=False, default=None)
    source_mode: str = field(init=False, default="unknown")
    data_delay_minutes: int | None = field(init=False, default=None)
    synthetic: bool = field(init=False, default=False)
    seed: int | None = field(init=False, default=None)
    scenario_version: str | None = field(init=False, default=None)
    playback_speed: str | None = field(init=False, default=None)
    continuity_status: str = field(init=False, default="continuous")
    clean_shutdown: bool = field(init=False, default=False)
    finalized: bool = field(init=False, default=False)
    _depth_buffer: list[dict[str, object]] = field(init=False, default_factory=list)
    _trade_buffer: list[dict[str, object]] = field(init=False, default_factory=list)
    utc_end: str | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        """Create the session folder and initial manifest."""
        start = self.session_start_utc.astimezone(UTC)
        object.__setattr__(self, "session_start_utc", start)
        partition_date = start.astimezone(self.partition_timezone).date().isoformat()
        base_session_id = self.requested_session_id or f"session_{start.strftime('%Y%m%dT%H%M%SZ')}"
        session_dir = _unique_session_dir(self.root_dir / partition_date, base_session_id)
        session_dir.mkdir(parents=True, exist_ok=False)
        self.session_dir = session_dir
        self.session_id = session_dir.name
        # Pending rows are buffered and flushed in batches (see the buffer
        # fields above). Rewriting the whole parquet on every event was O(n^2)
        # and dropped ~97% of a live MNQ feed once the file grew large;
        # batching makes recording keep up.
        self._write_manifest()

    @property
    def depth_path(self) -> Path:
        """Return the session depth Parquet path."""
        return self.session_dir / "depth.parquet"

    @property
    def trades_path(self) -> Path:
        """Return the session trades Parquet path."""
        return self.session_dir / "trades.parquet"

    @property
    def connection_events_path(self) -> Path:
        """Return the append-only connection event log path."""
        return self.session_dir / "connection_events.jsonl"

    @property
    def manifest_path(self) -> Path:
        """Return the session manifest path."""
        return self.session_dir / "session_manifest.json"

    def record(self, event: Mapping[str, object]) -> Path:
        """Buffer one raw market event; flushed to Parquet in batches.

        Rows land on disk when a buffer reaches ``PARQUET_FLUSH_THRESHOLD``
        or on :meth:`flush`/:meth:`finalize`. Call :meth:`flush` before
        reading a partition mid-session.
        """
        normalized_event = normalize_market_event(event)
        event_kind = market_event_kind(normalized_event)
        if event_kind == "depth":
            self._depth_buffer.append(_depth_row(normalized_event))
            self.depth_updates += 1
            self.symbol = str(normalized_event["symbol"])
            output_path = self.depth_path
            if len(self._depth_buffer) >= PARQUET_FLUSH_THRESHOLD:
                self._flush_depth()
                self._write_manifest()
        else:
            self._trade_buffer.append(_trade_row(normalized_event))
            self.trades += 1
            self.symbol = str(normalized_event["instrument"])
            output_path = self.trades_path
            if len(self._trade_buffer) >= PARQUET_FLUSH_THRESHOLD:
                self._flush_trades()
                self._write_manifest()
        return output_path

    def flush(self) -> None:
        """Write all buffered rows to their Parquet partitions."""
        self._flush_depth()
        self._flush_trades()
        self._write_manifest()

    def _flush_depth(self) -> None:
        if self._depth_buffer:
            _append_parquet(self.depth_path, DEPTH_SCHEMA, self._depth_buffer)
            self._depth_buffer = []

    def _flush_trades(self) -> None:
        if self._trade_buffer:
            _append_parquet(self.trades_path, TRADE_SCHEMA, self._trade_buffer)
            self._trade_buffer = []

    def record_control_event(self, event: Mapping[str, object]) -> Path:
        """Append one Java bridge control event and update session metadata."""
        if not is_control_event(event):
            raise ValueError("control event must have a supported control type")
        normalized_event = dict(event)
        self.connection_events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection_events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(normalized_event, sort_keys=True, separators=(",", ":")) + "\n")
        self.connection_events += 1
        self._apply_control_metadata(normalized_event)
        self._write_manifest()
        return self.connection_events_path

    def finalize(self, *, clean_shutdown: bool, reason: str | None = None) -> None:
        """Finalize the session manifest after the WebSocket stream ends."""
        if self.finalized:
            return
        self._flush_depth()
        self._flush_trades()
        self.clean_shutdown = clean_shutdown
        if not clean_shutdown:
            self.continuity_status = reason or "incomplete"
        self.utc_end = datetime.now(UTC).isoformat()
        self.finalized = True
        self._write_manifest()

    def _apply_control_metadata(self, event: RawStreamEvent) -> None:
        event_type = str(event["type"])
        self.alias = str(event.get("alias", self.alias or "")) or self.alias
        self.symbol = str(event.get("symbol", self.symbol or "")) or self.symbol
        self.addon_version = str(event.get("addon_version", self.addon_version or "")) or self.addon_version
        if "dropped_message_count" in event:
            self.dropped_message_count = max(self.dropped_message_count, int(event["dropped_message_count"]))
        if event_type in {"replay_started", "historical_mode"}:
            self.source_mode = "replay"
        elif event_type == "prototype_mode":
            self.source_mode = "prototype"
            self.synthetic = True
        elif event_type == "delayed_mode":
            self.source_mode = "delayed"
            self.data_delay_minutes = _optional_int(event.get("delay_minutes"))
        elif event_type == "realtime_started":
            if self.source_mode not in {"prototype", "delayed"}:
                self.source_mode = "live"
        elif event_type in {"connected", "heartbeat"} and event.get("source_mode"):
            if self.source_mode != "delayed":
                self.source_mode = _normalize_source_mode(str(event["source_mode"]))
        if "delay_minutes" in event:
            self.data_delay_minutes = _optional_int(event.get("delay_minutes"))
        if "synthetic" in event:
            self.synthetic = bool(event["synthetic"])
        if "seed" in event:
            self.seed = int(event["seed"])
        if "scenario_version" in event:
            self.scenario_version = str(event["scenario_version"])
        if "playback_speed" in event:
            self.playback_speed = str(event["playback_speed"])
        if event_type == "data_gap":
            self.continuity_status = str(event.get("reason", "data_gap"))
        elif event_type == "disconnected":
            self.continuity_status = str(event.get("reason", "disconnected"))
        elif event_type == "session_ended":
            self.finalize(clean_shutdown=True)

    def _manifest(self) -> dict[str, object]:
        valid_for_analysis = (
            self.finalized
            and self.clean_shutdown
            and self.continuity_status == "continuous"
            and self.dropped_message_count == 0
        )
        return {
            "alias": self.alias,
            "symbol": self.symbol,
            "source_mode": self.source_mode,
            "data_delay_minutes": self.data_delay_minutes,
            "synthetic": self.synthetic,
            "seed": self.seed,
            "scenario_version": self.scenario_version,
            "playback_speed": self.playback_speed,
            "utc_start": self.session_start_utc.isoformat(),
            "utc_end": self.utc_end,
            "addon_version": self.addon_version,
            "receiver_version": RECEIVER_VERSION,
            "event_counts": {
                "depth_updates": self.depth_updates,
                "trades": self.trades,
                "connection_events": self.connection_events,
            },
            "dropped_message_count": self.dropped_message_count,
            "continuity_status": self.continuity_status,
            "clean_shutdown": self.clean_shutdown,
            "valid_for_analysis": valid_for_analysis,
            "valid_for_real_training": False if self.synthetic else valid_for_analysis,
            "valid_for_live_decisions": self.source_mode == "live" and valid_for_analysis,
            "analysis_scope": _analysis_scope(self.source_mode, self.synthetic),
        }

    def _write_manifest(self) -> None:
        self.manifest_path.write_text(
            json.dumps(self._manifest(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


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


def _unique_session_dir(parent: Path, base_session_id: str) -> Path:
    candidate = parent / base_session_id
    index = 1
    while candidate.exists():
        candidate = parent / f"{base_session_id}_{index:04d}"
        index += 1
    return candidate


def _normalize_source_mode(source_mode: str) -> str:
    normalized = source_mode.strip().lower()
    if normalized in {"prototype", "synthetic", "prototype_mode"}:
        return "prototype"
    if normalized in {"delayed", "delayed_mode", "bookmap_delayed", "free_delayed"}:
        return "delayed"
    if normalized in {"live", "realtime", "real_time"}:
        return "live"
    if normalized in {"replay", "historical", "history", "historical_mode"}:
        return "replay"
    return "unknown"


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _analysis_scope(source_mode: str, synthetic: bool) -> str:
    if synthetic:
        return "prototype_only"
    if source_mode == "delayed":
        return "delayed_market_data"
    return "real_or_replay"


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
