"""Runtime state machine for the automatic MNQ assistant."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum


class RuntimeState(StrEnum):
    """Supported automatic runtime states."""

    STARTING = "STARTING"
    WAITING_FOR_BOOKMAP = "WAITING_FOR_BOOKMAP"
    RECORDING_ONLY = "RECORDING_ONLY"
    AUTO_WARMUP = "AUTO_WARMUP"
    SHADOW_READY = "SHADOW_READY"
    SHADOW_ACTIVE = "SHADOW_ACTIVE"
    DATA_STALE = "DATA_STALE"
    CONNECTION_LOST = "CONNECTION_LOST"
    PROFILE_UNAVAILABLE = "PROFILE_UNAVAILABLE"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


class RuntimeMode(StrEnum):
    """Supported runtime modes for the automatic launcher."""

    SHADOW = "SHADOW"


@dataclass(frozen=True, slots=True)
class RuntimeTransition:
    """One runtime state transition."""

    timestamp_utc: datetime
    previous_state: RuntimeState
    new_state: RuntimeState
    reason: str


@dataclass(slots=True)
class RuntimeStateMachine:
    """Small explicit state machine for the one-click runtime."""

    state: RuntimeState = RuntimeState.STARTING
    transitions: list[RuntimeTransition] = field(default_factory=list)

    def start(self, *, now: datetime | None = None) -> RuntimeState:
        """Move from STARTING into the Bookmap wait state."""
        return self.transition(RuntimeState.WAITING_FOR_BOOKMAP, "runtime started", now=now)

    def bookmap_connected(self, *, now: datetime | None = None) -> RuntimeState:
        """Mark Bookmap connected and begin raw recording."""
        return self.transition(RuntimeState.RECORDING_ONLY, "Bookmap connected", now=now)

    def warmup(self, *, now: datetime | None = None) -> RuntimeState:
        """Mark the runtime as warming up automatic context."""
        return self.transition(RuntimeState.AUTO_WARMUP, "market data warmup in progress", now=now)

    def shadow_ready(self, *, now: datetime | None = None) -> RuntimeState:
        """Mark the runtime as ready to make shadow decisions."""
        return self.transition(RuntimeState.SHADOW_READY, "shadow decision pipeline ready", now=now)

    def shadow_active(self, *, now: datetime | None = None) -> RuntimeState:
        """Mark the runtime as actively logging shadow decisions."""
        return self.transition(RuntimeState.SHADOW_ACTIVE, "shadow decisions active", now=now)

    def data_stale(self, *, now: datetime | None = None) -> RuntimeState:
        """Mark the runtime as blocked by stale market data."""
        return self.transition(RuntimeState.DATA_STALE, "market data is stale", now=now)

    def connection_lost(self, *, now: datetime | None = None) -> RuntimeState:
        """Mark the runtime as blocked by a Bookmap disconnect."""
        return self.transition(RuntimeState.CONNECTION_LOST, "Bookmap connection lost", now=now)

    def profile_unavailable(self, *, now: datetime | None = None) -> RuntimeState:
        """Mark the runtime as recording only because no usable profile is available."""
        return self.transition(RuntimeState.PROFILE_UNAVAILABLE, "strategy profile unavailable", now=now)

    def stopping(self, *, now: datetime | None = None) -> RuntimeState:
        """Mark the runtime as stopping."""
        return self.transition(RuntimeState.STOPPING, "runtime stopping", now=now)

    def stopped(self, *, now: datetime | None = None) -> RuntimeState:
        """Mark the runtime as fully stopped."""
        return self.transition(RuntimeState.STOPPED, "runtime stopped", now=now)

    def transition(
        self,
        new_state: RuntimeState,
        reason: str,
        *,
        now: datetime | None = None,
    ) -> RuntimeState:
        """Record a state transition and return the current state."""
        if new_state == self.state:
            return self.state
        timestamp = (now or datetime.now(UTC)).astimezone(UTC)
        self.transitions.append(
            RuntimeTransition(
                timestamp_utc=timestamp,
                previous_state=self.state,
                new_state=new_state,
                reason=reason,
            ),
        )
        self.state = new_state
        return self.state

