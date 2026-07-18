"""Bounded hand-off from the capture loop to the analysis thread.

The production failure this fixes, measured on the real Bookmap stream at
~1,331 events/sec: strategy evaluation ran INLINE on the receiver's asyncio
loop (`_dispatch_market_event` → controller update → paper engine update →
full two-direction order-flow evaluation every 25 events). While an evaluation
ran, the loop could not ``recv()``; the websockets library stopped reading;
TCP backpressure filled the Java ForwardingQueue; Java dropped messages — the
GUI's "Session drops" climbing 17,030 → 43,296 — and the session was
permanently invalidated. Persisted rate ~1,285/s vs arrival ~1,331/s: a small
sustained deficit that compounds forever.

The rule now: **the capture loop does parse → guard → one state update →
record → O(1) offer.** Everything analytical (controller context, paper
evaluation, strategy checks) runs on THIS feed's dedicated thread.

Loss semantics are strictly one-way. If the analysis thread cannot keep up,
events are skipped for ANALYSIS ONLY — recording upstream is untouched — and
the skip is loudly counted and reported to the paper engine as a causality
gap so it can refuse new entries rather than trade on a stream with holes.
Nothing here can ever cause a recording drop.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Mapping

# ~37 seconds of headroom at the observed real rate (1,331 ev/s). A burst can
# park here without touching the socket; a sustained overrun skips analysis
# work loudly instead of stalling capture.
DEFAULT_CAPACITY = 50_000


@dataclass(frozen=True, slots=True)
class AnalysisFeedMetrics:
    """Point-in-time counters for conservation accounting and the GUI."""

    offered: int
    processed: int
    skipped: int
    depth: int
    high_water: int
    capacity: int
    causality_gaps: int
    # Time the most recently PROCESSED event spent inside this feed (offer ->
    # processed), in ms. This is pipeline lag only - it deliberately excludes
    # the intentional 15-minute source delay, which is not an application fault.
    lag_ms: float | None

    @property
    def backlog_fraction(self) -> float:
        """Queue fullness as 0..1."""
        return self.depth / self.capacity if self.capacity else 0.0


class AnalysisFeed:
    """Single consumer thread pulling (event, state) pairs for analysis sinks.

    ``offer`` is the only hot-path call and is O(1): append under a lock, no
    allocation beyond the tuple, never blocks, never raises into capture.
    """

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_CAPACITY,
        on_batch_start: Callable[[], None] | None = None,
        pressure_check: Callable[[], bool] | None = None,
    ) -> None:
        """Create a stopped feed; call ``start`` to begin consuming.

        ``pressure_check`` implements capture priority ACROSS threads: when it
        returns True (the recorder queue is filling), this consumer sleeps and
        yields the GIL to the writer instead of competing with it. Analysis
        backlog parks in this queue; only a genuine overrun skips (paper-only).
        """
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._pressure_check = pressure_check
        self._pressure_pauses = 0
        self._queue: deque[tuple[Mapping[str, object], object]] = deque()
        self._lock = threading.Lock()
        self._ready = threading.Condition(self._lock)
        self._sinks: list[Callable[[Mapping[str, object], object], None]] = []
        self._gap_sinks: list[Callable[[int], None]] = []
        self._on_batch_start = on_batch_start
        self._offered = 0
        self._processed = 0
        self._skipped = 0
        self._high_water = 0
        self._causality_gaps = 0
        self._pending_gap = 0
        self._last_lag_ms: float | None = None
        self._last_error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- wiring ----------------------------------------------------------------

    def add_sink(self, sink: Callable[[Mapping[str, object], object], None]) -> None:
        """Register an analysis consumer called with (event, state) in order."""
        self._sinks.append(sink)

    def add_gap_sink(self, sink: Callable[[int], None]) -> None:
        """Register a callback told how many events were skipped for analysis."""
        self._gap_sinks.append(sink)

    # -- the hot path ----------------------------------------------------------

    def offer(self, event: Mapping[str, object], state: object) -> bool:
        """Queue one accepted event for analysis. O(1); never blocks capture.

        Returns False when the event had to be skipped for analysis (recording
        is unaffected either way).
        """
        with self._lock:
            self._offered += 1
            if len(self._queue) >= self._capacity:
                # Analysis is behind. Capture must not wait: skip loudly.
                self._skipped += 1
                self._pending_gap += 1
                return False
            self._queue.append((event, state, time.monotonic_ns()))
            depth = len(self._queue)
            if depth > self._high_water:
                self._high_water = depth
            self._ready.notify()
            return True

    # -- the analysis thread ---------------------------------------------------

    def start(self) -> None:
        """Start the consumer thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="mnq-analysis-feed", daemon=True,
        )
        self._thread.start()

    def stop(self, *, drain_seconds: float = 5.0) -> bool:
        """Stop, draining what is queued within a bounded budget.

        Returns whether the queue fully drained; an incomplete drain only
        affects analysis (paper), never the recording.
        """
        deadline = time.monotonic() + drain_seconds
        while time.monotonic() < deadline:
            with self._lock:
                if not self._queue:
                    break
            time.sleep(0.02)
        self._stop.set()
        with self._lock:
            self._ready.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=drain_seconds)
        with self._lock:
            return not self._queue

    def _run(self) -> None:
        batch: list[tuple[Mapping[str, object], object, int]] = []
        while True:
            gap = 0
            with self._lock:
                while not self._queue and not self._stop.is_set():
                    self._ready.wait(timeout=0.25)
                if self._stop.is_set() and not self._queue:
                    return
                # Take a bounded batch so metrics stay fresh and stop() stays
                # responsive even under sustained load.
                take = min(len(self._queue), 500)
                batch = [self._queue.popleft() for _ in range(take)]
                if self._pending_gap:
                    gap, self._pending_gap = self._pending_gap, 0
                    self._causality_gaps += 1
            if gap:
                for gap_sink in self._gap_sinks:
                    try:
                        gap_sink(gap)
                    except Exception:  # noqa: BLE001,S110 - analysis must not die
                        pass
            # Capture priority: while the RECORDER is under pressure, yield the
            # GIL to its writer thread instead of competing. The backlog parks
            # here (bounded); recording always wins the contention.
            if self._pressure_check is not None:
                paused = False
                while not self._stop.is_set():
                    try:
                        if not self._pressure_check():
                            break
                    except Exception:  # noqa: BLE001 - a broken check must not stall analysis
                        break
                    paused = True
                    time.sleep(0.005)
                if paused:
                    with self._lock:
                        self._pressure_pauses += 1
            if self._on_batch_start is not None:
                try:
                    self._on_batch_start()
                except Exception:  # noqa: BLE001,S110
                    pass
            for event, state, offered_ns in batch:
                for sink in self._sinks:
                    try:
                        sink(event, state)
                    except Exception as error:  # noqa: BLE001 - one bad sink event must not stop analysis
                        self._last_error = f"{type(error).__name__}: {error}"
                with self._lock:
                    self._processed += 1
                    self._last_lag_ms = (time.monotonic_ns() - offered_ns) / 1e6
            batch.clear()

    # -- read model ------------------------------------------------------------

    def metrics(self) -> AnalysisFeedMetrics:
        """Return conservation counters (safe from any thread)."""
        with self._lock:
            return AnalysisFeedMetrics(
                offered=self._offered,
                processed=self._processed,
                skipped=self._skipped,
                depth=len(self._queue),
                high_water=self._high_water,
                capacity=self._capacity,
                causality_gaps=self._causality_gaps,
                lag_ms=self._last_lag_ms,
            )

    @property
    def last_error(self) -> str:
        """The most recent sink error, if any."""
        return self._last_error
