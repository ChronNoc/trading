"""Bookmap feed guardian: connection robustness and data-quality gating.

Separates two questions the rest of the system needs answered
independently: "is the socket connected?" (connection health) and "is the
data arriving trustworthy?" (data quality). The combined
``risk_connection_ok`` flag is what callers pass into the existing
``risk/limits.py`` connection-health check - that protected module is not
modified.

The guard runs against live, delayed, and Bookmap Replay data. Wall-clock
drift is meaningful only for a true live entitlement, so it is disabled for
delayed and replay data.
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

MODE_FULL = "full"
MODE_TRADES_ONLY = "trades_only"
MODE_STALLED = "stalled"
MODE_OFFLINE = "offline"


@dataclass(frozen=True, slots=True)
class FeedGuardConfig:
    """Tunable thresholds for the feed guard."""

    stale_after_ns: int = 2_000_000_000
    clock_drift_alert_ns: int = 5_000_000_000
    snapshot_ring_size: int = 256
    replay_buffer_size: int = 1024
    source_mode: str = "live"

    def __post_init__(self) -> None:
        """Validate thresholds."""
        if self.stale_after_ns <= 0:
            raise ValueError("stale_after_ns must be positive")
        if self.clock_drift_alert_ns <= 0:
            raise ValueError("clock_drift_alert_ns must be positive")
        if self.snapshot_ring_size <= 0:
            raise ValueError("snapshot_ring_size must be positive")
        if self.replay_buffer_size <= 0:
            raise ValueError("replay_buffer_size must be positive")
        if self.source_mode not in ("live", "delayed", "replay"):
            raise ValueError("source_mode must be 'live', 'delayed', or 'replay'")


class ExponentialBackoff:
    """Deterministic exponential backoff for reconnect loops."""

    def __init__(self, *, base_seconds: float = 0.25, factor: float = 2.0, max_seconds: float = 30.0) -> None:
        """Create a backoff starting at ``base_seconds`` and capping at ``max_seconds``."""
        if base_seconds <= 0 or factor < 1.0 or max_seconds < base_seconds:
            raise ValueError("backoff needs base_seconds > 0, factor >= 1, max_seconds >= base_seconds")
        self.base_seconds = base_seconds
        self.factor = factor
        self.max_seconds = max_seconds
        self._attempts = 0

    @property
    def attempts(self) -> int:
        """Return how many delays have been handed out since the last reset."""
        return self._attempts

    def next_delay(self) -> float:
        """Return the next delay in seconds and advance the attempt counter."""
        delay = min(self.base_seconds * (self.factor ** self._attempts), self.max_seconds)
        self._attempts += 1
        return delay

    def reset(self) -> None:
        """Reset after a successful connection."""
        self._attempts = 0


class SequenceGapDetector:
    """Detects missing sequence ids on the trade stream."""

    def __init__(self) -> None:
        """Create a detector with no observed sequence yet."""
        self._last_sequence_id: int | None = None
        self.gap_count = 0
        self.missed_events = 0
        self.duplicate_count = 0
        self.out_of_order_count = 0
        self.last_classification = "initial"

    def observe(self, sequence_id: int) -> int:
        """Record a sequence id; return how many events were missed before it."""
        missed = 0
        if self._last_sequence_id is not None and sequence_id == self._last_sequence_id:
            self.duplicate_count += 1
            self.last_classification = "duplicate"
            return 0
        if self._last_sequence_id is not None and sequence_id < self._last_sequence_id:
            self.out_of_order_count += 1
            self.last_classification = "out_of_order"
            return 0
        if self._last_sequence_id is not None and sequence_id > self._last_sequence_id + 1:
            missed = sequence_id - self._last_sequence_id - 1
            self.gap_count += 1
            self.missed_events += missed
            self.last_classification = "gap"
        else:
            self.last_classification = "next" if self._last_sequence_id is not None else "initial"
        if self._last_sequence_id is None or sequence_id > self._last_sequence_id:
            self._last_sequence_id = sequence_id
        return missed

    def reset(self) -> None:
        """Start continuity accounting for a new explicit connection boundary."""
        self._last_sequence_id = None
        self.gap_count = 0
        self.missed_events = 0
        self.duplicate_count = 0
        self.out_of_order_count = 0
        self.last_classification = "initial"


class EventReplayBuffer:
    """Bounded in-order buffer that survives sub-second disconnects."""

    def __init__(self, max_events: int) -> None:
        """Create a buffer holding at most ``max_events``."""
        if max_events <= 0:
            raise ValueError("max_events must be positive")
        self._events: deque[Mapping[str, object]] = deque()
        self._max_events = max_events
        self.dropped_events = 0

    def __len__(self) -> int:
        """Return the number of buffered events."""
        return len(self._events)

    def buffer(self, event: Mapping[str, object]) -> None:
        """Store an event, dropping the oldest when full."""
        if len(self._events) >= self._max_events:
            self._events.popleft()
            self.dropped_events += 1
        self._events.append(event)

    def drain(self) -> tuple[Mapping[str, object], ...]:
        """Return all buffered events in arrival order and empty the buffer."""
        drained = tuple(self._events)
        self._events.clear()
        return drained


class BookSnapshotRing:
    """Bounded ring of raw order-book events for post-hoc debugging."""

    def __init__(self, max_entries: int) -> None:
        """Create a ring keeping the newest ``max_entries`` snapshots."""
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._entries: deque[Mapping[str, object]] = deque(maxlen=max_entries)

    def __len__(self) -> int:
        """Return how many snapshots are held."""
        return len(self._entries)

    def record(self, event: Mapping[str, object]) -> None:
        """Store a depth event; the oldest entry falls off automatically."""
        self._entries.append(dict(event))

    def entries(self) -> tuple[Mapping[str, object], ...]:
        """Return the held snapshots, oldest first."""
        return tuple(self._entries)

    def write_jsonl(self, path: Path) -> Path:
        """Dump the ring to a JSONL file for debugging and return the path."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for entry in self._entries:
                handle.write(json.dumps(entry) + "\n")
        return path


@dataclass(frozen=True, slots=True)
class FeedGuardStatus:
    """Independent connection-health and data-quality flags plus counters."""

    connection_healthy: bool
    data_quality_ok: bool
    mode: str
    risk_connection_ok: bool
    reasons: tuple[str, ...]
    depth_age_ns: int | None
    trade_age_ns: int | None
    sequence_gaps: int
    missed_events: int
    malformed_events: int
    out_of_order_events: int
    clock_drift_alerts: int
    duplicate_events: int = 0
    stream_out_of_order_events: int = 0


class FeedGuard:
    """Stateful guard fed every control and market event from the receiver."""

    def __init__(
        self,
        config: FeedGuardConfig | None = None,
        *,
        now_ns: Callable[[], int] | None = None,
    ) -> None:
        """Create a guard; ``now_ns`` is injectable for deterministic tests."""
        self.config = config or FeedGuardConfig()
        self._now_ns = now_ns or time.time_ns
        self.backoff = ExponentialBackoff()
        self.sequence_gaps = SequenceGapDetector()
        self.stream_sequence = SequenceGapDetector()
        self._stream_sequence_observed = False
        self.replay_buffer = EventReplayBuffer(self.config.replay_buffer_size)
        self.snapshot_ring = BookSnapshotRing(self.config.snapshot_ring_size)
        self._connected = False
        self._last_depth_arrival_ns: int | None = None
        self._last_trade_arrival_ns: int | None = None
        self._last_depth_event_ns: int | None = None
        self.malformed_events = 0
        self.out_of_order_events = 0
        self.clock_drift_alerts = 0
        self._clock_drift_active = False
        self._rejection_log: list[str] = []

    @property
    def rejections(self) -> tuple[str, ...]:
        """Return every loud rejection reason recorded so far."""
        return tuple(self._rejection_log)

    def handle_control_event(self, event: Mapping[str, object]) -> None:
        """Track connection state from receiver control events."""
        event_type = str(event.get("type", ""))
        if event_type == "connected":
            self.sequence_gaps.reset()
            self.stream_sequence.reset()
            self._stream_sequence_observed = False
        stream_sequence = event.get("stream_sequence")
        if isinstance(stream_sequence, int):
            self._observe_stream_sequence(stream_sequence)
        if event_type in ("connected", "realtime_started", "prototype_mode", "historical_mode"):
            self._connected = True
            self.backoff.reset()
        elif event_type in ("disconnected", "session_ended"):
            self._connected = False

    def ingest_market_event(self, event: Mapping[str, object]) -> tuple[bool, str | None]:
        """Validate one market event; returns (accepted, rejection_reason)."""
        arrival_ns = self._now_ns()
        event_ns = _event_timestamp_ns(event)
        stream_sequence = event.get("stream_sequence")
        if isinstance(stream_sequence, int):
            integrity_reason = self._observe_stream_sequence(stream_sequence)
            if integrity_reason is not None:
                self._rejection_log.append(integrity_reason)
                return False, integrity_reason

        if self.config.source_mode == "live" and event_ns is not None:
            drift = abs(arrival_ns - event_ns)
            if drift > self.config.clock_drift_alert_ns:
                if not self._clock_drift_active:
                    self.clock_drift_alerts += 1
                self._clock_drift_active = True
            else:
                self._clock_drift_active = False

        if event.get("type") == "depth_update":
            if (
                event_ns is not None
                and self._last_depth_event_ns is not None
                and event_ns < self._last_depth_event_ns
            ):
                self.out_of_order_events += 1
                reason = f"out-of-order depth update: {event_ns} < {self._last_depth_event_ns}"
                self._rejection_log.append(reason)
                return False, reason
            if event_ns is not None:
                self._last_depth_event_ns = event_ns
            self._last_depth_arrival_ns = arrival_ns
            self.snapshot_ring.record(event)
        else:
            sequence_id = event.get("sequence_id")
            if isinstance(sequence_id, int):
                missed = self.sequence_gaps.observe(sequence_id)
                if missed:
                    self._rejection_log.append(
                        f"sequence gap before id {sequence_id}: {missed} event(s) missed",
                    )
            self._last_trade_arrival_ns = arrival_ns
        return True, None

    def _observe_stream_sequence(self, sequence: int) -> str | None:
        """Account one global wire sequence and identify duplicates/reordering."""
        self._stream_sequence_observed = True
        missed = self.stream_sequence.observe(sequence)
        if missed:
            reason = f"stream sequence gap before {sequence}: {missed} event(s) missed"
            self._rejection_log.append(reason)
            return None
        if self.stream_sequence.last_classification == "duplicate":
            return f"duplicate stream sequence {sequence}"
        if self.stream_sequence.last_classification == "out_of_order":
            return f"out-of-order stream sequence {sequence}"
        return None

    def record_malformed(self, reason: str) -> None:
        """Count a malformed message loudly instead of swallowing it."""
        self.malformed_events += 1
        self._rejection_log.append(f"malformed message: {reason}")

    def status(self) -> FeedGuardStatus:
        """Compute the current dual-flag health status."""
        now = self._now_ns()
        depth_age = None if self._last_depth_arrival_ns is None else now - self._last_depth_arrival_ns
        trade_age = None if self._last_trade_arrival_ns is None else now - self._last_trade_arrival_ns
        depth_fresh = depth_age is not None and depth_age <= self.config.stale_after_ns
        trade_fresh = trade_age is not None and trade_age <= self.config.stale_after_ns

        reasons: list[str] = []
        if not self._connected:
            mode = MODE_OFFLINE
            reasons.append("feed disconnected")
        elif depth_fresh and trade_fresh:
            mode = MODE_FULL
        elif trade_fresh:
            mode = MODE_TRADES_ONLY
            reasons.append("depth stalled; running reduced-confidence trades-only mode")
        else:
            mode = MODE_STALLED
            reasons.append("no fresh market data within the staleness window")

        if self._clock_drift_active:
            reasons.append("feed timestamps drifting from local clock")

        continuity = self.stream_sequence if self._stream_sequence_observed else self.sequence_gaps
        integrity_ok = (
            continuity.missed_events == 0
            and continuity.duplicate_count == 0
            and continuity.out_of_order_count == 0
        )
        if not integrity_ok:
            reasons.append("stream continuity failed; session must be invalidated")
        data_quality_ok = (
            mode in (MODE_FULL, MODE_TRADES_ONLY)
            and not self._clock_drift_active
            and integrity_ok
        )
        connection_healthy = self._connected
        return FeedGuardStatus(
            connection_healthy=connection_healthy,
            data_quality_ok=data_quality_ok,
            mode=mode,
            risk_connection_ok=connection_healthy and data_quality_ok,
            reasons=tuple(reasons),
            depth_age_ns=depth_age,
            trade_age_ns=trade_age,
            sequence_gaps=continuity.gap_count,
            missed_events=continuity.missed_events,
            malformed_events=self.malformed_events,
            out_of_order_events=self.out_of_order_events,
            clock_drift_alerts=self.clock_drift_alerts,
            duplicate_events=continuity.duplicate_count,
            stream_out_of_order_events=continuity.out_of_order_count,
        )


def _event_timestamp_ns(event: Mapping[str, object]) -> int | None:
    for key in ("timestamp_ns", "timestamp"):
        value = event.get(key)
        if isinstance(value, int):
            return value
    return None
