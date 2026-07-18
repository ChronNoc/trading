"""Automatic session rotation after data-loss invalidation.

The production failure this fixes: a session was invalidated by bridge drops
(17,030 rising to 43,296) and the application simply kept recording INTO the
invalidated session forever. Every further hour of capture was poured into a
session that could never be research-eligible.

Policy, exactly as honest as the data:

* A session that lost events STAYS invalid. Nothing here hides, resets, or
  relabels drop counters; the damaged session finalizes with its full loss
  accounting and remains available for diagnostics, ineligible for research.
* The instant damage is FIRST observed, the paper engine is told: bridge drops
  mean the analysis tape already has holes, so the open simulated position is
  closed (DATA_GAP) and no entry is allowed until re-warmed on clean data.
* Once the pipeline has been healthy (no NEW damage) for a full quiet window,
  the damaged session is finalized and a FRESH session begins. Clean events
  stop being mixed into a dead session.
* Rotation happens between events on the single consume thread, so ordering
  is preserved exactly and no event is lost in the swap: an event triggers
  rotation and is then recorded as the first event of the new session.

``RotatingRecorder`` wraps the (pipelined) session recorder, so the receiver's
consume loop needs no changes at all.
"""

from __future__ import annotations

import time
from typing import Callable, Mapping

# No new damage for this long => the pipeline is healthy again; rotate.
DEFAULT_HEALTHY_SECONDS = 30.0
# Never rotate more often than this (a flapping feed must not shred sessions).
DEFAULT_MIN_SESSION_SECONDS = 120.0


def session_damage(recorder: object) -> int:
    """Return the recorder's total observed data-loss count (0 = intact).

    Counts only INTEGRITY damage: bridge drops (the Java queue overflowed
    upstream) and events the recorder itself failed to persist. Guard
    rejections and malformed events are quality accounting, not loss of
    accepted data, and do not invalidate a session.
    """
    dropped = int(getattr(recorder, "dropped_message_count", 0) or 0)
    lost = int(getattr(recorder, "lost_events", 0) or 0)
    overflow = 0
    metrics = getattr(recorder, "metrics", None)
    if metrics is not None:
        overflow = int(getattr(metrics, "overflow", 0) or 0)
    return dropped + lost + overflow


class RotatingRecorder:
    """Delegating recorder that swaps in a fresh session after data loss heals.

    Everything not defined here (record, flush, counters, manifest paths…)
    delegates to the CURRENT inner recorder, so the consume loop and the
    finalize path see a normal recorder at all times.
    """

    def __init__(
        self,
        make_recorder: Callable[[], object],
        *,
        on_rotated_out: Callable[[object], None] | None = None,
        on_damage: Callable[[int], None] | None = None,
        healthy_seconds: float = DEFAULT_HEALTHY_SECONDS,
        min_session_seconds: float = DEFAULT_MIN_SESSION_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create the wrapper and open the first session immediately."""
        self._make = make_recorder
        self._on_rotated_out = on_rotated_out
        self._on_damage = on_damage
        self._healthy_seconds = healthy_seconds
        self._min_session_seconds = min_session_seconds
        self._clock = clock
        self._inner = make_recorder()
        self._session_started = clock()
        self._known_damage = 0
        self._bridge_drop_baseline = 0
        self._baseline_initialized = False
        self._first_damage_at: float | None = None
        self._last_damage_at: float | None = None
        self.rotations = 0
        self.first_drop_reason = ""

    # -- the consume-loop surface ---------------------------------------------

    def record(self, event: Mapping[str, object]) -> object:
        """Record one market event, rotating first if the session healed."""
        self._observe_damage()
        if self._should_rotate():
            self._rotate()
        return self._inner.record(event)

    def record_control_event(self, event: Mapping[str, object]) -> object:
        """Delegate control events to the current session's recorder."""
        if (
            str(event.get("type", "")) == "connected"
            and "dropped_message_count" in event
            and not self._baseline_initialized
        ):
            # Java reports a process-lifetime high-water mark. Drops from an old
            # connection must not instantly invalidate a new session.
            self._bridge_drop_baseline = int(event["dropped_message_count"])
            self._baseline_initialized = True
        result = self._inner.record_control_event(event)
        self._observe_damage()  # heartbeats carry the bridge drop counter
        return result

    def __getattr__(self, name: str) -> object:
        """Everything else (finalize, counters, paths) is the current session."""
        return getattr(self._inner, name)

    # -- damage tracking --------------------------------------------------------

    def _observe_damage(self) -> None:
        lifetime_drops = int(getattr(self._inner, "dropped_message_count", 0) or 0)
        current_bridge_drops = max(0, lifetime_drops - self._bridge_drop_baseline)
        lost = int(getattr(self._inner, "lost_events", 0) or 0)
        metrics = getattr(self._inner, "metrics", None)
        overflow = int(getattr(metrics, "overflow", 0) or 0) if metrics is not None else 0
        damage = current_bridge_drops + lost + overflow
        if damage > self._known_damage:
            now = self._clock()
            if self._first_damage_at is None:
                self._first_damage_at = now
                self.first_drop_reason = (
                    f"session invalidated: {damage} event(s) lost "
                    f"(bridge drops / persistence failures)"
                )
                if self._on_damage is not None:
                    try:
                        self._on_damage(damage)
                    except Exception:  # noqa: BLE001,S110 - reporting must not break capture
                        pass
            elif self._on_damage is not None:
                try:
                    self._on_damage(damage - self._known_damage)
                except Exception:  # noqa: BLE001,S110
                    pass
            self._known_damage = damage
            self._last_damage_at = now

    def _should_rotate(self) -> bool:
        if self._first_damage_at is None or self._last_damage_at is None:
            return False  # intact session: never rotate it away
        now = self._clock()
        if now - self._session_started < self._min_session_seconds:
            return False  # flapping protection
        return (now - self._last_damage_at) >= self._healthy_seconds

    def _rotate(self) -> None:
        """Finalize the damaged session and begin a fresh one."""
        old = self._inner
        try:
            old.finalize(
                clean_shutdown=False,
                reason=(
                    f"rotated away after data loss ({self._known_damage} event(s)); "
                    "pipeline healthy again - starting a clean session"
                ),
            )
        except Exception:  # noqa: BLE001,S110 - a broken finalize must not stop capture
            pass
        if self._on_rotated_out is not None:
            try:
                self._on_rotated_out(old)
            except Exception:  # noqa: BLE001,S110
                pass
        self._inner = self._make()
        self._bridge_drop_baseline = int(getattr(old, "dropped_message_count", 0) or 0)
        self._baseline_initialized = True
        self._session_started = self._clock()
        self._known_damage = 0
        self._first_damage_at = None
        self._last_damage_at = None
        self.first_drop_reason = ""
        self.rotations += 1

    # -- read model --------------------------------------------------------------

    @property
    def session_damaged(self) -> bool:
        """Whether the CURRENT session has observed any data loss."""
        return self._first_damage_at is not None

    @property
    def current_session_id(self) -> str:
        """The active session's id."""
        return str(getattr(self._inner, "session_id", ""))
