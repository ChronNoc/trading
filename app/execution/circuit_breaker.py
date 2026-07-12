"""Latency circuit breaker - item 38.

Trips open when the signal-to-intended-execution latency spikes past a
threshold; while open, no execution intent may proceed. Reset is a
deliberate manual call, never automatic.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BreakerStatus:
    """Current breaker state with the reason it is open."""

    open: bool
    reason: str
    trip_count: int


class LatencyCircuitBreaker:
    """Trips on abnormal signal-to-execution latency."""

    def __init__(self, *, max_latency_ms: int = 500, trip_after: int = 3) -> None:
        """Trip after ``trip_after`` consecutive samples above ``max_latency_ms``."""
        if max_latency_ms <= 0 or trip_after <= 0:
            raise ValueError("max_latency_ms and trip_after must be positive")
        self.max_latency_ms = max_latency_ms
        self.trip_after = trip_after
        self._consecutive_spikes = 0
        self._open = False
        self._trip_count = 0
        self._reason = "breaker closed"

    def record_latency(self, latency_ms: int) -> BreakerStatus:
        """Record one signal-to-intended-execution latency sample."""
        if latency_ms > self.max_latency_ms:
            self._consecutive_spikes += 1
            if self._consecutive_spikes >= self.trip_after and not self._open:
                self._open = True
                self._trip_count += 1
                self._reason = (
                    f"tripped: {self._consecutive_spikes} consecutive samples above "
                    f"{self.max_latency_ms}ms (last {latency_ms}ms)"
                )
        else:
            self._consecutive_spikes = 0
        return self.status()

    def allow_execution(self) -> bool:
        """Whether execution intents may currently proceed."""
        return not self._open

    def reset(self) -> None:
        """Manual reset - the only way the breaker closes again."""
        self._open = False
        self._consecutive_spikes = 0
        self._reason = "breaker closed after manual reset"

    def status(self) -> BreakerStatus:
        """Return the current breaker state."""
        return BreakerStatus(open=self._open, reason=self._reason, trip_count=self._trip_count)
