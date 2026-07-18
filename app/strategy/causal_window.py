"""Time-spanned, sampled causal window shared by streaming paper and replay.

The defect this replaces, measured on the real feed: the strategy window was
``deque(maxlen=200)`` — 200 *events*. At the real ~1,331 events/sec that window
spans **~0.12 seconds** of market time. Every order-flow pattern the strategy
checks (a reload cycle, 400 contracts absorbed at a defended block, a reaction
then continuation) plays out over seconds to minutes, so every one of the
18,614 real evaluations rejected: the conditions were structurally
unsatisfiable inside 120 milliseconds. The identical windowing drove offline
replay, which is why the one clean recorded session also produced zero accepts.
Same code, same mis-sizing — consistent, and consistently meaningless.

The window is now sized in **market time** and *sampled*:

* Snapshots are committed at most every ``sample_interval_ms``; between
  commits, the newest state REPLACES the live head, so ``window[-1]`` is always
  the current market state.
* The window spans up to ``span_seconds`` of market time (evicting from the
  left), bounded by ``max_snapshots`` so evaluation cost is independent of the
  event rate.

Why sampling is semantics-preserving for the canonical strategy (verified
against ``app/strategy/order_flow.py``, not assumed):

* Every volume/CVD/absorption measure reads **cumulative** fields
  (``executed_buy_volume`` …) as first-vs-last deltas across the window.
  Deltas over endpoints are identical no matter how many intermediate
  snapshots existed. Trades are never "coalesced away" — their volume lives in
  the cumulative counters carried by every later snapshot.
* Block detection counts per-snapshot observations of a price level. With
  time-spaced samples, ``durable_block_min_snapshots = 3`` finally means
  "persists ~3 sampling intervals" (real durability) instead of "persists 3
  consecutive raw events ≈ 2 milliseconds".
* No condition reads an individual print size from a single snapshot.

This is plumbing, not strategy: no threshold changes.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

# Cost ceiling: evaluation work is O(len(window) * book_levels) and must not
# scale with the event rate. 180s / 250ms = 721 samples nominal.
DEFAULT_MAX_SNAPSHOTS = 2_000


@dataclass(frozen=True, slots=True)
class WindowInfo:
    """Observable sizing of the window, for status displays and tests."""

    snapshots: int
    span_seconds: float
    first_event_index: int
    last_event_index: int


class CausalWindow:
    """Holds sampled MarketState snapshots covering a span of market time."""

    def __init__(
        self,
        *,
        span_seconds: float,
        sample_interval_ms: float,
        max_snapshots: int = DEFAULT_MAX_SNAPSHOTS,
    ) -> None:
        """Create an empty window. ``sample_interval_ms=0`` keeps every event."""
        if span_seconds < 0 or sample_interval_ms < 0:
            raise ValueError("span and sample interval must be non-negative")
        if max_snapshots < 2:
            raise ValueError("max_snapshots must allow at least first and last")
        self._span_ns = int(span_seconds * 1e9)
        self._interval_ns = int(sample_interval_ms * 1e6)
        self._max = max_snapshots
        self._snapshots: deque[object] = deque()
        self._event_indices: deque[int] = deque()
        self._last_committed_ns: int | None = None

    def observe(self, state: object, *, event_index: int = 0) -> None:
        """Advance the window with one already-seen state (causal by definition).

        The newest state always becomes ``window[-1]``: it replaces the live
        head until the sampling interval elapses, then the head is committed
        and a new head begins. Cumulative first-vs-last measures are unaffected
        by the replacement; the current market view is never stale.
        """
        ts = int(getattr(state, "timestamp_ns", 0))
        if (
            self._snapshots
            and self._last_committed_ns is not None
            and ts - self._last_committed_ns < self._interval_ns
        ):
            self._snapshots[-1] = state
            self._event_indices[-1] = event_index
        else:
            self._snapshots.append(state)
            self._event_indices.append(event_index)
            self._last_committed_ns = ts
        # Evict by span (keeping at least two points for first-vs-last deltas),
        # then by the hard cost cap.
        while len(self._snapshots) > 2 and self._span_ns and (
            ts - int(getattr(self._snapshots[0], "timestamp_ns", 0)) > self._span_ns
        ):
            self._snapshots.popleft()
            self._event_indices.popleft()
        while len(self._snapshots) > self._max:
            self._snapshots.popleft()
            self._event_indices.popleft()

    def clear(self) -> None:
        """Drop everything (used after a causality gap: rebuild from clean data)."""
        self._snapshots.clear()
        self._event_indices.clear()
        self._last_committed_ns = None

    def view(self) -> tuple[object, ...]:
        """Return the snapshots oldest-to-newest for strategy evaluation."""
        return tuple(self._snapshots)

    def __len__(self) -> int:
        """Number of held snapshots."""
        return len(self._snapshots)

    @property
    def span_seconds(self) -> float:
        """Market time covered by the window, in seconds."""
        if len(self._snapshots) < 2:
            return 0.0
        first = int(getattr(self._snapshots[0], "timestamp_ns", 0))
        last = int(getattr(self._snapshots[-1], "timestamp_ns", 0))
        return max(0.0, (last - first) / 1e9)

    def info(self) -> WindowInfo:
        """Return observable sizing for status displays."""
        return WindowInfo(
            snapshots=len(self._snapshots),
            span_seconds=self.span_seconds,
            first_event_index=self._event_indices[0] if self._event_indices else 0,
            last_event_index=self._event_indices[-1] if self._event_indices else 0,
        )
