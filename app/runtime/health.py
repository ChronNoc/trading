"""Runtime health monitoring and append-only health events."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class HealthEvent:
    """One timestamped runtime health event."""

    timestamp_utc: str
    component: str
    status: str
    message: str


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """Current health state exposed to the GUI."""

    bookmap_connected: bool
    recording: bool
    data_stale: bool
    dropped_message_count: int
    last_event_age_ms: int | None
    gui_healthy: bool


@dataclass(slots=True)
class HealthMonitor:
    """Track component health with deterministic clock injection."""

    stale_after_ns: int = 5_000_000_000
    bookmap_connected: bool = False
    recording: bool = False
    gui_healthy: bool = True
    dropped_message_count: int = 0
    last_market_timestamp_ns: int | None = None
    events: list[HealthEvent] = field(default_factory=list)

    def record_event(
        self,
        component: str,
        status: str,
        message: str,
        *,
        now: datetime | None = None,
    ) -> HealthEvent:
        """Append a timestamped health event."""
        timestamp = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
        event = HealthEvent(
            timestamp_utc=timestamp,
            component=component,
            status=status,
            message=message,
        )
        self.events.append(event)
        return event

    def mark_bookmap_connected(self, *, now: datetime | None = None) -> None:
        """Mark the Bookmap stream as connected."""
        self.bookmap_connected = True
        self.recording = True
        self.record_event("bookmap", "connected", "Bookmap stream connected", now=now)

    def mark_bookmap_disconnected(self, reason: str, *, now: datetime | None = None) -> None:
        """Mark the Bookmap stream as disconnected."""
        self.bookmap_connected = False
        self.record_event("bookmap", "disconnected", reason, now=now)

    def mark_market_event(self, timestamp_ns: int) -> None:
        """Record the most recent market-event timestamp."""
        self.last_market_timestamp_ns = timestamp_ns

    def mark_gui_failed(self, message: str, *, now: datetime | None = None) -> None:
        """Mark the GUI unhealthy without stopping recording."""
        self.gui_healthy = False
        self.record_event("gui", "failed", message, now=now)

    def mark_dropped_messages(
        self,
        dropped_message_count: int,
        *,
        now: datetime | None = None,
    ) -> None:
        """Track the maximum dropped-message count reported by the bridge."""
        self.dropped_message_count = max(self.dropped_message_count, dropped_message_count)
        self.record_event(
            "bookmap",
            "data_gap",
            f"dropped messages reported: {self.dropped_message_count}",
            now=now,
        )

    def data_age_ns(self, current_timestamp_ns: int | None) -> int | None:
        """Return the latest market data age in nanoseconds."""
        if current_timestamp_ns is None or self.last_market_timestamp_ns is None:
            return None
        return max(current_timestamp_ns - self.last_market_timestamp_ns, 0)

    def is_data_stale(self, current_timestamp_ns: int | None) -> bool:
        """Return whether current data age exceeds the stale threshold."""
        age_ns = self.data_age_ns(current_timestamp_ns)
        if age_ns is None:
            return True
        return age_ns > self.stale_after_ns

    def snapshot(self, *, current_timestamp_ns: int | None = None) -> HealthStatus:
        """Return a GUI-friendly health snapshot."""
        age_ns = self.data_age_ns(current_timestamp_ns)
        return HealthStatus(
            bookmap_connected=self.bookmap_connected,
            recording=self.recording,
            data_stale=self.is_data_stale(current_timestamp_ns),
            dropped_message_count=self.dropped_message_count,
            last_event_age_ms=None if age_ns is None else age_ns // 1_000_000,
            gui_healthy=self.gui_healthy,
        )

    def write_events_jsonl(self, path: Path) -> Path:
        """Write health events as newline-delimited JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for event in self.events:
                handle.write(json.dumps(asdict(event), sort_keys=True, separators=(",", ":")) + "\n")
        return path

