"""Parquet recorder for raw market depth and trade events."""

from __future__ import annotations

import itertools
import json
import os
import threading
import time
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
# A fixed .tmp name lets two writers collide on the same file; these make every
# temp path unique and serialize the rename per destination.
_TEMP_COUNTER = itertools.count()
_WRITE_LOCKS: dict[str, threading.Lock] = {}
_LOCK_REGISTRY_GUARD = threading.Lock()
# Windows refuses a rename while ANY handle (incl. an AV scanner's) is open on
# the destination, so a rename must be retried to be reliable.
REPLACE_ATTEMPTS = 8
REPLACE_INITIAL_DELAY = 0.01
# Mid-session manifest rewrites are informational; cap their frequency so the
# fsync+rename cost cannot stall the writer thread every 500 events.
MANIFEST_MIN_INTERVAL_SECONDS = 5.0

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

# The network protocol remains the exact Bookmap event schema.  The receiver
# adds this local monotonic sequence only after validation, at the disk
# boundary, so new recordings can be replayed in the order Python received
# them even when exchange timestamps collide.
SESSION_DEPTH_SCHEMA = DEPTH_SCHEMA.append(pa.field("receive_sequence", pa.int64()))
SESSION_TRADE_SCHEMA = TRADE_SCHEMA.append(pa.field("receive_sequence", pa.int64()))


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




_REASON_BUCKET_LIMIT = 256


def _reason_bucket(reason: str, *, fallback: str) -> str:
    """Bound reason-cardinality while retaining the attributable CAUSE."""
    normalized = reason.strip() or fallback
    # A batch envelope wraps the real cause as "event batch item {index}: {cause}"
    # (or "event batch item {index} must be ..."). Here the index is the VOLATILE
    # part and the cause is stable - so strip the index prefix and bucket by the
    # cause. Without this, a malformed burst produces one bucket per batch
    # position (0..N) and the real schema reason is invisible.
    if normalized.startswith("event batch item "):
        rest = normalized[len("event batch item "):].lstrip("0123456789")
        normalized = rest.lstrip(": ").strip() or fallback
    # Other feed-guard details append volatile timestamps/sequence numbers after a
    # colon. Keeping the stable category before it prevents manifest/memory blowups.
    category = normalized.split(":", 1)[0].strip() or fallback
    return category[:160]

@dataclass(slots=True)
class MarketSessionRecorder:
    """Append one Bookmap bridge run into a unique raw-data session folder."""

    root_dir: Path = Path("data/raw")
    partition_timezone: timezone = UTC
    session_start_utc: datetime = field(default_factory=lambda: datetime.now(UTC))
    requested_session_id: str | None = None
    compact_on_finalize: bool = True
    session_dir: Path = field(init=False)
    session_id: str = field(init=False)
    depth_updates: int = field(init=False, default=0)
    trades: int = field(init=False, default=0)
    connection_events: int = field(init=False, default=0)
    dropped_message_count: int = field(init=False, default=0)
    malformed_event_count: int = field(init=False, default=0)
    malformed_event_reasons: dict[str, int] = field(init=False, default_factory=dict)
    rejected_event_count: int = field(init=False, default=0)
    rejected_event_reasons: dict[str, int] = field(init=False, default_factory=dict)
    out_of_order_event_count: int = field(init=False, default=0)
    trade_sequence_gap_count: int = field(init=False, default=0)
    missed_trade_event_count: int = field(init=False, default=0)
    duplicate_stream_event_count: int = field(init=False, default=0)
    clock_drift_alert_count: int = field(init=False, default=0)
    receiver_intake_lost_count: int = field(init=False, default=0)
    bridge_drop_baseline: int | None = field(init=False, default=None)
    transport_connections: int = field(init=False, default=0)
    transport_reconnects: int = field(init=False, default=0)
    protocol_version: str | None = field(init=False, default=None)
    provider: str | None = field(init=False, default=None)
    bridge_stream_id: str | None = field(init=False, default=None)
    bridge_session_id: str | None = field(init=False, default=None)
    connection_ids: set[str] = field(init=False, default_factory=set)
    declared_capabilities: set[str] = field(init=False, default_factory=set)
    handshake_accepted: bool = field(init=False, default=False)
    first_stream_sequence: int | None = field(init=False, default=None)
    last_stream_sequence: int | None = field(init=False, default=None)
    aggressor_side_trade_count: int = field(init=False, default=0)
    alias: str | None = field(init=False, default=None)
    symbol: str | None = field(init=False, default=None)
    addon_version: str | None = field(init=False, default=None)
    source_mode: str = field(init=False, default="unknown")
    data_delay_minutes: int | None = field(init=False, default=None)
    # Feed entitlement, separate from Bookmap playback phase. Once the feed is
    # known delayed it stays delayed for the whole session: replay_started /
    # realtime_started / connected / heartbeat are PLAYBACK phases and never
    # remove the user's 15-minute entitlement delay.
    is_delayed: bool = field(init=False, default=False)
    synthetic: bool = field(init=False, default=False)
    seed: int | None = field(init=False, default=None)
    scenario_version: str | None = field(init=False, default=None)
    playback_speed: str | None = field(init=False, default=None)
    continuity_status: str = field(init=False, default="continuous")
    clean_shutdown: bool = field(init=False, default=False)
    finalized: bool = field(init=False, default=False)
    _depth_buffer: list[dict[str, object]] = field(init=False, default_factory=list)
    _trade_buffer: list[dict[str, object]] = field(init=False, default_factory=list)
    _receive_sequence: int = field(init=False, default=0)
    _depth_part_index: int = field(init=False, default=0)
    _trade_part_index: int = field(init=False, default=0)
    _last_manifest_monotonic: float = field(init=False, default=0.0)
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
    def depth_parts_dir(self) -> Path:
        """Return the directory containing readable depth parts."""
        return self.session_dir / "depth_parts"

    @property
    def trade_parts_dir(self) -> Path:
        """Return the directory containing readable trade parts."""
        return self.session_dir / "trade_parts"

    @property
    def connection_events_path(self) -> Path:
        """Return the append-only connection event log path."""
        return self.session_dir / "connection_events.jsonl"

    @property
    def manifest_path(self) -> Path:
        """Return the session manifest path."""
        return self.session_dir / "session_manifest.json"

    def record(self, event: Mapping[str, object]) -> Path:
        """Buffer one raw market event with local receive-order provenance.

        Rows are flushed as atomically closed Parquet parts. Every completed
        part is readable while Bookmap continues recording and survives an
        unclean process exit. Compatibility callers may request final single
        files; production replays the parts directly so shutdown never rewrites
        a multi-million-event session.
        """
        if self.finalized:
            raise RuntimeError("cannot record market events after session finalization")
        return self._record_validated(normalize_market_event(event))

    def record_normalized(self, event: Mapping[str, object]) -> Path:
        """Persist an event that ``parse_stream_message`` already validated.

        The streaming path parses and schema-validates every event once at the
        socket. ``record`` then re-ran ``normalize_market_event`` - a full JSON
        serialize -> parse -> validate round-trip PER EVENT in the writer
        thread, purely redundant work that made the writer the throughput
        ceiling under load. Call this ONLY with events produced by the stream
        parser; raw external dicts must keep going through ``record``.
        """
        if self.finalized:
            raise RuntimeError("cannot record market events after session finalization")
        return self._record_validated(event)

    def _record_validated(self, normalized_event: Mapping[str, object]) -> Path:
        event_kind = market_event_kind(normalized_event)
        self._receive_sequence += 1
        stream_sequence = normalized_event.get("stream_sequence")
        if isinstance(stream_sequence, int):
            if self.first_stream_sequence is None:
                self.first_stream_sequence = stream_sequence
            self.last_stream_sequence = stream_sequence
        if event_kind == "depth":
            self._depth_buffer.append(_depth_row(normalized_event, self._receive_sequence))
            self.depth_updates += 1
            self.symbol = str(normalized_event["symbol"])
            output_path = self.depth_path
            if len(self._depth_buffer) >= PARQUET_FLUSH_THRESHOLD:
                self._flush_depth()
                self._maybe_write_manifest()
        else:
            self._trade_buffer.append(_trade_row(normalized_event, self._receive_sequence))
            self.trades += 1
            if str(normalized_event.get("aggressor_side", "")).lower() in {"buy", "sell"}:
                self.aggressor_side_trade_count += 1
            self.symbol = str(normalized_event["instrument"])
            output_path = self.trades_path
            if len(self._trade_buffer) >= PARQUET_FLUSH_THRESHOLD:
                self._flush_trades()
                self._maybe_write_manifest()
        return output_path

    def _maybe_write_manifest(self) -> None:
        """Rewrite the manifest at most every few seconds during recording.

        The manifest was rewritten (fsync + locked rename) after EVERY 500-row
        flush - ~2.7 stalls/second at the real event rate, each an fsync the
        writer thread waited on, and each a fresh invitation for the antivirus
        scanner to hold the file. Mid-session manifests are informational;
        ``finalize``/``flush`` still write the authoritative one unconditionally.
        """
        now = time.monotonic()
        if now - self._last_manifest_monotonic >= MANIFEST_MIN_INTERVAL_SECONDS:
            self._last_manifest_monotonic = now
            self._write_manifest()

    def flush(self) -> None:
        """Write buffered rows as atomically closed, immediately readable parts."""
        self._flush_depth()
        self._flush_trades()
        self._write_manifest()

    def _flush_depth(self) -> None:
        if not self._depth_buffer:
            return
        batch = pa.Table.from_pylist(self._depth_buffer, schema=SESSION_DEPTH_SCHEMA)
        _write_parquet_part(
            self.depth_parts_dir,
            self._depth_part_index,
            batch,
        )
        self._depth_part_index += 1
        self._depth_buffer = []

    def _flush_trades(self) -> None:
        if not self._trade_buffer:
            return
        batch = pa.Table.from_pylist(self._trade_buffer, schema=SESSION_TRADE_SCHEMA)
        _write_parquet_part(
            self.trade_parts_dir,
            self._trade_part_index,
            batch,
        )
        self._trade_part_index += 1
        self._trade_buffer = []

    def note_malformed_event(self, reason: str) -> None:
        """Count malformed input with bounded, attributable reason categories."""
        category = _reason_bucket(reason, fallback="unspecified schema error")
        self.malformed_event_count += 1
        if category not in self.malformed_event_reasons and len(self.malformed_event_reasons) >= _REASON_BUCKET_LIMIT:
            category = "other malformed schema errors"
        self.malformed_event_reasons[category] = self.malformed_event_reasons.get(category, 0) + 1

    def note_rejected_event(self, reason: str) -> None:
        """Count rejections with bounded, attributable reason categories."""
        category = _reason_bucket(reason, fallback="unspecified rejection")
        self.rejected_event_count += 1
        if category not in self.rejected_event_reasons and len(self.rejected_event_reasons) >= _REASON_BUCKET_LIMIT:
            category = "other feed-guard rejections"
        self.rejected_event_reasons[category] = self.rejected_event_reasons.get(category, 0) + 1
        if "out-of-order" in category.lower():
            self.out_of_order_event_count += 1

    def update_feed_quality(
        self,
        *,
        sequence_gaps: int,
        missed_events: int,
        malformed_events: int,
        out_of_order_events: int,
        clock_drift_alerts: int,
        duplicate_events: int = 0,
    ) -> None:
        """Merge per-connection feed-guard counters into the session manifest."""
        self.trade_sequence_gap_count = max(self.trade_sequence_gap_count, sequence_gaps)
        self.missed_trade_event_count = max(self.missed_trade_event_count, missed_events)
        self.malformed_event_count = max(self.malformed_event_count, malformed_events)
        self.out_of_order_event_count = max(self.out_of_order_event_count, out_of_order_events)
        self.duplicate_stream_event_count = max(
            self.duplicate_stream_event_count, duplicate_events
        )
        self.clock_drift_alert_count = max(self.clock_drift_alert_count, clock_drift_alerts)
        if self.finalized:
            self._write_manifest()

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
        if self.compact_on_finalize:
            _merge_parquet_parts(self.depth_parts_dir, self.depth_path, SESSION_DEPTH_SCHEMA)
            _merge_parquet_parts(self.trade_parts_dir, self.trades_path, SESSION_TRADE_SCHEMA)
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
        stream_sequence = event.get("stream_sequence")
        if isinstance(stream_sequence, int):
            if self.first_stream_sequence is None:
                self.first_stream_sequence = stream_sequence
            self.last_stream_sequence = stream_sequence
        if "dropped_message_count" in event:
            lifetime_drops = int(str(event["dropped_message_count"]))
            if self.bridge_drop_baseline is None:
                self.bridge_drop_baseline = lifetime_drops
            self.dropped_message_count = max(self.dropped_message_count, lifetime_drops)
        if event_type == "connected":
            self.transport_connections += 1
            if str(event.get("connection_boundary", "")) in {"reconnect", "new_stream"}:
                self.transport_reconnects += 1
            protocol_version = str(event.get("protocol_version", "")).strip()
            provider = str(event.get("provider", "")).strip()
            bridge_stream_id = str(event.get("stream_id", "")).strip()
            bridge_session_id = str(event.get("session_id", "")).strip()
            connection_id = str(event.get("connection_id", "")).strip()
            if protocol_version:
                self.protocol_version = protocol_version
            if provider:
                self.provider = provider
            if bridge_stream_id:
                self.bridge_stream_id = bridge_stream_id
            if bridge_session_id:
                self.bridge_session_id = bridge_session_id
            if connection_id:
                self.connection_ids.add(connection_id)
            self.declared_capabilities.update(
                capability.strip()
                for capability in str(event.get("capabilities", "")).split(",")
                if capability.strip()
            )
            # Accepted handshake evidence is session-monotonic. A reconnect or
            # duplicate control event from an older/incomplete bridge may omit
            # the enrichment field, but that absence must not erase an earlier
            # compatibility decision. Missing evidence still fails closed when
            # no accepted handshake has ever been observed.
            self.handshake_accepted = (
                self.handshake_accepted
                or event.get("handshake_accepted") is True
            )
        if "receiver_intake_lost" in event:
            self.receiver_intake_lost_count = max(
                self.receiver_intake_lost_count,
                int(str(event["receiver_intake_lost"])),
            )
        if event_type in {"replay_started", "historical_mode"} and not self.is_delayed:
            self.source_mode = "replay"
        elif event_type == "prototype_mode":
            self.source_mode = "prototype"
            self.synthetic = True
        elif event_type == "delayed_mode":
            self.source_mode = "delayed"
            self.is_delayed = True
            self.data_delay_minutes = _optional_int(event.get("delay_minutes"))
        elif event_type == "realtime_started":
            # Playback reached the current edge of the supplied stream; this is
            # NOT proof of a real-time entitlement. Never relabel a delayed feed
            # as live.
            if self.source_mode not in {"prototype", "delayed"} and not self.is_delayed:
                self.source_mode = "live"
        elif event_type in {"connected", "heartbeat"} and event.get("source_mode"):
            if not self.is_delayed and self.source_mode != "delayed":
                self.source_mode = _normalize_source_mode(str(event["source_mode"]))
        if "delay_minutes" in event:
            self.data_delay_minutes = _optional_int(event.get("delay_minutes"))
            if (self.data_delay_minutes or 0) > 0:
                self.is_delayed = True
        if "synthetic" in event:
            self.synthetic = bool(event["synthetic"])
        if "seed" in event:
            self.seed = int(str(event["seed"]))
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
        session_bridge_drops = (
            self.dropped_message_count
            if self.bridge_drop_baseline is None
            else max(0, self.dropped_message_count - self.bridge_drop_baseline)
        )
        quality_counters = {
            "malformed_events": self.malformed_event_count,
            "rejected_events": self.rejected_event_count,
            "out_of_order_events": self.out_of_order_event_count,
            "trade_sequence_gaps": self.trade_sequence_gap_count,
            "missed_trade_events": self.missed_trade_event_count,
            "stream_sequence_gaps": self.trade_sequence_gap_count,
            "missed_stream_events": self.missed_trade_event_count,
            "duplicate_stream_events": self.duplicate_stream_event_count,
            "clock_drift_alerts": self.clock_drift_alert_count,
            "session_dropped_messages": session_bridge_drops,
            "bridge_dropped_messages": self.dropped_message_count,
            "receiver_intake_lost": self.receiver_intake_lost_count,
        }
        invalidating_counters = {
            key: value
            for key, value in quality_counters.items()
            if key != "bridge_dropped_messages"
        }
        quality_ok = all(value == 0 for value in invalidating_counters.values())
        valid_for_analysis = (
            self.finalized
            and self.clean_shutdown
            and self.continuity_status == "continuous"
            and quality_ok
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
            "transport": {
                "connections": self.transport_connections,
                "reconnects": self.transport_reconnects,
            },
            "bridge_provenance": {
                "protocol_version": self.protocol_version,
                "minimum_protocol_version": "1.2",
                "provider": self.provider,
                "stream_id": self.bridge_stream_id,
                "bridge_session_id": self.bridge_session_id,
                "connection_ids": sorted(self.connection_ids),
                "declared_capabilities": sorted(self.declared_capabilities),
                "handshake_accepted": self.handshake_accepted,
                "first_stream_sequence": self.first_stream_sequence,
                "last_stream_sequence": self.last_stream_sequence,
                "observed": {
                    "depth_updates": self.depth_updates,
                    "trades": self.trades,
                    "trades_with_aggressor_side": self.aggressor_side_trade_count,
                },
            },
            "storage": {
                "format": "closed_parquet_parts_v1",
                "depth_parts": self._depth_part_index,
                "trade_parts": self._trade_part_index,
                "finalized_single_files": (
                    self.depth_path.is_file() or self.trades_path.is_file()
                ),
                "compaction_deferred": self.finalized and not self.compact_on_finalize,
            },
            "receive_order": {
                "field": "receive_sequence",
                "available": True,
                "last_sequence": self._receive_sequence,
                "scope": "validated market events within this receiver session",
            },
            "data_quality": {
                "ok": quality_ok,
                **quality_counters,
                "malformed_event_reasons": dict(sorted(self.malformed_event_reasons.items())),
                "rejected_event_reasons": dict(sorted(self.rejected_event_reasons.items())),
            },
            "dropped_message_count": self.dropped_message_count,
            "continuity_status": self.continuity_status,
            "clean_shutdown": self.clean_shutdown,
            "is_delayed": self.is_delayed,
            "provenance": _provenance(self.synthetic, self.is_delayed, self.source_mode),
            "valid_for_analysis": valid_for_analysis,
            "valid_for_real_training": False if self.synthetic else valid_for_analysis,
            "valid_for_order_flow_replay": (
                valid_for_analysis and self.depth_updates > 0 and self.trades > 0
            ),
            # A delayed entitlement can never authorize live decisions, no matter
            # what playback phase Bookmap reached.
            "valid_for_live_decisions": (
                self.source_mode == "live" and not self.is_delayed and valid_for_analysis
            ),
            "analysis_scope": _analysis_scope(self.source_mode, self.synthetic),
        }

    def _write_manifest(self) -> None:
        payload = json.dumps(self._manifest(), indent=2, sort_keys=True) + "\n"
        atomic_write_text(self.manifest_path, payload)


def unique_temp_path(destination: Path, suffix: str) -> Path:
    """Return a temp path unique to THIS writer, beside ``destination``.

    A fixed ``.tmp`` name is the bug this exists to prevent: two overlapping
    writers open the same temporary file, and whichever calls ``replace`` first
    hits it while the other still holds a handle. On Windows that raises
    ``PermissionError: [WinError 32] ... used by another process``; on POSIX it
    silently interleaves two payloads, which is worse. Including the pid and a
    per-process counter makes collision impossible.
    """
    return destination.with_name(
        f"{destination.name}.{os.getpid()}.{next(_TEMP_COUNTER)}{suffix}",
    )


def replace_with_retry(
    temporary: Path,
    destination: Path,
    *,
    attempts: int = REPLACE_ATTEMPTS,
    initial_delay: float = REPLACE_INITIAL_DELAY,
) -> None:
    """``os.replace`` with bounded backoff, because Windows transiently refuses.

    Windows raises ``PermissionError [WinError 5] Access is denied`` when ANY
    handle exists on the destination - including the short-lived ones Defender
    and the search indexer take to scan a file the instant it is written. No
    amount of locking prevents that: the other holder is not our process.

    Measured, not assumed: 16 threads rewriting one manifest produced 1-2 such
    failures per run before this retry existed. Retrying is the documented way
    to make a rename reliable on Windows; the last attempt is allowed to raise
    so a genuine permission problem still surfaces instead of being swallowed.
    """
    delay = initial_delay
    for attempt in range(1, attempts + 1):
        try:
            os.replace(temporary, destination)
            return
        except PermissionError:
            if attempt == attempts:
                raise  # a real, persistent problem must not be hidden
            time.sleep(delay)
            delay *= 2


def atomic_write_text(destination: Path, payload: str) -> None:
    """Write ``payload`` to ``destination`` atomically and durably.

    Serialized per-path so two threads cannot race the rename, retried against
    transient Windows handle contention, and the temp file is always cleaned up.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = unique_temp_path(destination, ".tmp")
    with _write_lock_for(destination):
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())  # survive a crash, not just a clean exit
            replace_with_retry(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)  # never strand a temp file


def _write_lock_for(destination: Path) -> threading.Lock:
    """Return the process-wide lock guarding writes to ``destination``."""
    key = str(destination.resolve() if destination.parent.exists() else destination)
    with _LOCK_REGISTRY_GUARD:
        return _WRITE_LOCKS.setdefault(key, threading.Lock())


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


def _write_parquet_part(parts_dir: Path, part_index: int, table: pa.Table) -> Path:
    """Atomically publish one closed and readable Parquet part."""
    parts_dir.mkdir(parents=True, exist_ok=True)
    destination = parts_dir / f"part-{part_index:06d}.parquet"
    temporary = unique_temp_path(destination, ".tmp")
    try:
        pq.write_table(table, temporary)
        replace_with_retry(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _merge_parquet_parts(parts_dir: Path, destination: Path, schema: pa.Schema) -> None:
    """Build a legacy single Parquet file from closed parts using bounded batches."""
    parts = tuple(sorted(parts_dir.glob("part-*.parquet"))) if parts_dir.is_dir() else ()
    if not parts:
        return
    temporary = unique_temp_path(destination, ".tmp")
    writer: pq.ParquetWriter | None = None
    try:
        writer = pq.ParquetWriter(temporary, schema)
        for part in parts:
            parquet = pq.ParquetFile(part)
            for batch in parquet.iter_batches(batch_size=PARQUET_FLUSH_THRESHOLD):
                writer.write_batch(batch)
        # The writer MUST be closed before the rename: Windows refuses to replace
        # a file that still has an open handle (WinError 32).
        writer.close()
        writer = None
        replace_with_retry(temporary, destination)
    finally:
        if writer is not None:
            writer.close()
        temporary.unlink(missing_ok=True)


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


# Provenance values used throughout research: real delayed data is valid for
# offline analysis but never for live decisions; synthetic never enters real
# performance metrics.
PROVENANCE_SYNTHETIC = "SYNTHETIC"
PROVENANCE_REAL_DELAYED = "REAL_DELAYED"
PROVENANCE_REAL_REPLAY = "REAL_REPLAY"
PROVENANCE_REAL_REALTIME = "REAL_REALTIME"
PROVENANCE_UNKNOWN = "UNKNOWN"


def _provenance(synthetic: bool, is_delayed: bool, source_mode: str) -> str:
    """Classify a session's provenance from entitlement and origin."""
    if synthetic:
        return PROVENANCE_SYNTHETIC
    if is_delayed:
        return PROVENANCE_REAL_DELAYED
    if source_mode in {"replay", "historical"}:
        return PROVENANCE_REAL_REPLAY
    if source_mode == "live":
        return PROVENANCE_REAL_REALTIME
    return PROVENANCE_UNKNOWN


def _depth_row(
    event: Mapping[str, object],
    receive_sequence: int | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "timestamp": int(event["timestamp"]),
        "symbol": str(event["symbol"]),
        "side": str(event["side"]),
        "price": str(event["price"]),
        "previous_size": str(event["previous_size"]),
        "new_size": str(event["new_size"]),
    }
    if receive_sequence is not None:
        row["receive_sequence"] = receive_sequence
    return row


def _trade_row(
    event: Mapping[str, object],
    receive_sequence: int | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "timestamp_ns": int(event["timestamp_ns"]),
        "price": str(event["price"]),
        "size": str(event["size"]),
        "aggressor_side": str(event["aggressor_side"]),
        "instrument": str(event["instrument"]),
        "sequence_id": int(event["sequence_id"]),
    }
    if receive_sequence is not None:
        row["receive_sequence"] = receive_sequence
    return row
