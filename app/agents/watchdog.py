"""Rule-based runtime watchdog for the prototype dashboard.

Watches successive dashboard snapshots for anomalies the user would
otherwise only notice by staring at counters: feed gaps, stalled trade
events while depth keeps flowing, and paused playback. Purely
observational - it never touches the pipeline it watches.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.prototype.scenarios import PrototypeDashboardSnapshot

SEVERITY_OK = "ok"
SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"

_TRADE_STALL_CHECKS = 3


@dataclass(frozen=True, slots=True)
class WatchdogReport:
    """One watchdog status line with severity for GUI display."""

    severity: str
    message: str


class Watchdog:
    """Stateful anomaly checker fed one dashboard snapshot per refresh."""

    def __init__(self) -> None:
        """Create a watchdog with no observed history."""
        self._last_depth_events: int | None = None
        self._last_trade_events: int | None = None
        self._trade_stall_checks = 0

    def evaluate(self, snapshot: PrototypeDashboardSnapshot) -> WatchdogReport:
        """Evaluate the current snapshot against the previous one."""
        stalled = self._update_trade_stall(snapshot)

        if snapshot.runtime_state == "not running":
            return WatchdogReport(SEVERITY_INFO, "Watchdog: prototype runtime not running.")
        if snapshot.synthetic_status == "reconnecting":
            return WatchdogReport(
                SEVERITY_WARNING,
                "Watchdog: feed disconnected - reconnecting; decisions stay blocked until data resumes.",
            )
        if snapshot.synthetic_status == "data gap exercise":
            return WatchdogReport(
                SEVERITY_WARNING,
                "Watchdog: data gap in progress - decisions are blocked during the gap.",
            )
        if snapshot.paused:
            return WatchdogReport(SEVERITY_INFO, "Watchdog: playback paused by user.")
        if stalled:
            return WatchdogReport(
                SEVERITY_WARNING,
                "Watchdog: depth events continue but trade events stalled - check the trade wiring.",
            )
        return WatchdogReport(
            SEVERITY_OK,
            f"Watchdog: feeds healthy ({snapshot.depth_events} depth / {snapshot.trade_events} trade events).",
        )

    def _update_trade_stall(self, snapshot: PrototypeDashboardSnapshot) -> bool:
        """Track consecutive refreshes where depth grows but trades do not."""
        depth_grew = (
            self._last_depth_events is not None and snapshot.depth_events > self._last_depth_events
        )
        trades_static = (
            self._last_trade_events is not None and snapshot.trade_events == self._last_trade_events
        )
        if depth_grew and trades_static and snapshot.trade_events > 0:
            self._trade_stall_checks += 1
        else:
            self._trade_stall_checks = 0
        self._last_depth_events = snapshot.depth_events
        self._last_trade_events = snapshot.trade_events
        return self._trade_stall_checks >= _TRADE_STALL_CHECKS
