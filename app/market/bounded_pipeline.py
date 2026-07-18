"""Bounded, measured capture pipeline: intake queue -> processing -> recorder queue.

Replaces the unmeasured direct path with explicit bounded stages:

1. **WebSocket intake queue** (:class:`BoundedIntakeBuffer`): an asyncio task
   reads the socket without backpressure (the Bookmap side never blocks) into a
   bounded queue; overflow is NEVER silent - it is counted and surfaced as an
   explicit ``data_gap`` control message in the stream itself.
2. **Validation/FeedGuard + MarketState** run in the pipeline worker between the
   two queues (the existing receiver), with end-to-end lag measured.
3. **Recorder batch queue** (:class:`RecorderPipeline`): a dedicated writer
   thread drains a bounded queue into the real recorder, measuring ingress rate,
   persisted rate, batch sizes, flush duration, flush failures, occupancy, and
   high-water marks. Data capture owns this thread - GUI or research slowness
   cannot stall it.
4. **GUI snapshot coalescing** is the existing ``CurrentMarketState`` store: the
   GUI polls the latest state on a timer, so bursts coalesce naturally; the
   pipeline exposes the counters the GUI displays.

Clean shutdown drains every queue before finalizing. All behavior is
deterministic under an injected clock in tests.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Mapping


@dataclass(slots=True)
class StageMetrics:
    """Live counters for one bounded stage."""

    capacity: int = 0
    occupancy: int = 0
    high_water: int = 0
    ingress: int = 0
    egress: int = 0
    overflow: int = 0

    def snapshot(self) -> dict[str, int]:
        """Return a plain-dict copy for the GUI."""
        return {"capacity": self.capacity, "occupancy": self.occupancy,
                "high_water": self.high_water, "ingress": self.ingress,
                "egress": self.egress, "overflow": self.overflow}


@dataclass(slots=True)
class PipelineMetricsSnapshot:
    """Everything the GUI displays about the capture pipeline - real numbers."""

    intake: dict[str, int] = field(default_factory=dict)
    recorder: dict[str, int] = field(default_factory=dict)
    ingress_events_per_second: float = 0.0
    persisted_events_per_second: float = 0.0
    last_batch_size: int = 0
    last_flush_duration_ms: float = 0.0
    flush_failures: int = 0
    end_to_end_lag_ms: float = 0.0


class PipelineStateHolder:
    """Thread-safe holder linking the live pipeline to the GUI and research health."""

    def __init__(self) -> None:
        """Create an empty holder (no connection yet)."""
        self._lock = threading.Lock()
        self._intake: BoundedIntakeBuffer | None = None
        self._recorder: RecorderPipeline | None = None

    def attach(self, intake: object, recorder: object) -> None:
        """Attach the current connection's intake buffer and recorder pipeline.

        Duck-typed on ``metrics_snapshot``, NOT isinstance: the recorder is now
        wrapped by RotatingRecorder (which delegates metrics to the current
        inner pipeline), and the old isinstance check silently dropped it -
        losing recorder metrics AND the capture-priority pressure signal.
        """
        with self._lock:
            self._intake = intake if isinstance(intake, BoundedIntakeBuffer) else None
            self._recorder = recorder if hasattr(recorder, "metrics_snapshot") else None

    def snapshot(self) -> PipelineMetricsSnapshot:
        """Return the current combined pipeline measurements."""
        with self._lock:
            intake, recorder = self._intake, self._recorder
        snap = recorder.metrics_snapshot() if recorder is not None else PipelineMetricsSnapshot()
        if intake is not None:
            snap.intake = intake.metrics.snapshot()
        return snap

    def worst_queue_occupancy_fraction(self) -> float:
        """Return the fullest queue's occupancy fraction (for research throttling)."""
        snap = self.snapshot()
        fractions = []
        for stage in (snap.intake, snap.recorder):
            capacity = stage.get("capacity", 0)
            if capacity:
                fractions.append(stage.get("occupancy", 0) / capacity)
        return max(fractions) if fractions else 0.0


class BoundedIntakeBuffer:
    """Bounded async intake between the socket and the receiver.

    ``pump()`` reads the underlying connection into the queue; iteration yields
    from the queue. On overflow the OLDEST unprocessed frame is replaced by an
    explicit synthetic ``data_gap`` control frame carrying the loss count, so
    downstream (recorder manifest, GUI) always sees the gap.
    """

    def __init__(self, connection: object, *, capacity: int = 10_000) -> None:
        """Wrap an async-iterable connection with a bounded queue."""
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._connection = connection
        # Only real frames (or the close sentinel) are queued; gap markers are
        # synthesized out-of-band so they never consume capacity.
        self._queue: asyncio.Queue[str | bytes] = asyncio.Queue(maxsize=capacity)
        self.metrics = StageMetrics(capacity=capacity)
        self._lost = 0
        self._gap_pending = False
        self._closed = False

    def _force_put(self, item: str | bytes | None) -> None:
        """Enqueue without blocking, evicting AT MOST ONE item to make room.

        Every queued item is a real market frame (or the close sentinel), so an
        eviction is always genuine data loss and is always counted. Gap markers
        are deliberately kept OUT of the queue - see :meth:`__anext__` - so a
        marker can never displace a real frame nor be miscounted as lost data.
        """
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - race
                pass
            else:
                self._lost += 1  # every queued item is a real market frame
                self.metrics.overflow += 1
        self._queue.put_nowait(item)

    async def pump(self) -> None:
        """Read the socket until it closes; never blocks the socket on a full queue."""
        try:
            async for message in self._connection:  # type: ignore[attr-defined]
                self.metrics.ingress += 1
                if self._queue.full():
                    # Loss is loud but coalesced: the new frame displaces exactly
                    # ONE old frame, and the gap is announced out-of-band so the
                    # announcement itself never costs another real event.
                    self._gap_pending = True
                self._force_put(message)
                self.metrics.occupancy = self._queue.qsize()
                self.metrics.high_water = max(self.metrics.high_water, self.metrics.occupancy)
        finally:
            # Closing is signalled out-of-band too: a queued sentinel would have
            # displaced (and lost) one more real frame on a full queue.
            self._closed = True

    def __aiter__(self) -> "BoundedIntakeBuffer":
        """Iterate messages out of the bounded queue."""
        return self

    async def __anext__(self) -> str | bytes:
        """Yield the next frame, announcing any pending gap first.

        The gap marker is synthesized here rather than queued: it occupies no
        queue capacity, so announcing loss can never cause more loss, and it is
        always delivered (a queued marker could be stranded behind the close
        sentinel and never seen).
        """
        if self._gap_pending:
            self._gap_pending = False
            return json.dumps({
                "type": "data_gap", "timestamp_ns": time.time_ns(),
                "reason": "receiver intake queue overflow",
                "receiver_intake_lost": self._lost,
            })
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                if self._closed:
                    raise StopAsyncIteration  # closed and fully drained
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=0.05)
                except (TimeoutError, asyncio.TimeoutError):
                    continue  # re-check the closed flag, then keep waiting
            self.metrics.occupancy = self._queue.qsize()
            self.metrics.egress += 1
            return item


class RecorderPipeline:
    """Bounded writer-thread wrapper around a session recorder.

    ``record()`` is non-blocking for the caller: events go into a bounded queue
    and a dedicated thread persists them in batches, timing every flush. On
    overflow the loss is counted on the recorder itself via
    ``note_rejected_event`` (which flows into the session manifest) - never
    silent. ``finalize()`` drains the queue first, so clean shutdown loses
    nothing.
    """

    def __init__(
        self,
        recorder: object,
        *,
        capacity: int = 50_000,
        batch_size: int = 500,
        clock: Callable[[], float] = time.perf_counter,
        events_prevalidated: bool = False,
    ) -> None:
        """Wrap ``recorder`` (delegating everything else to it).

        ``events_prevalidated=True`` routes writes through the recorder's
        ``record_normalized`` fast path. Only the streaming wiring may set it:
        events there come out of ``parse_stream_message`` already validated,
        and re-validating each one in the writer thread (a JSON round-trip per
        event) made the writer the throughput ceiling under real load.
        """
        if capacity <= 0 or batch_size <= 0:
            raise ValueError("capacity and batch_size must be positive")
        self._recorder = recorder
        self._write = (
            getattr(recorder, "record_normalized", None)
            if events_prevalidated else None
        ) or recorder.record
        self._clock = clock
        self._batch_size = batch_size
        self._queue: deque[tuple[Mapping[str, object], float]] = deque()
        self._capacity = capacity
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.metrics = StageMetrics(capacity=capacity)
        self._rate_window: deque[tuple[float, int, int]] = deque(maxlen=120)
        self.last_batch_size = 0
        self.last_flush_duration_ms = 0.0
        self.flush_failures = 0
        self.lost_events = 0  # events that could not be persisted after retries
        self.fail_closed = False  # a write failure invalidates the segment
        self._max_write_attempts = 3
        self.end_to_end_lag_ms = 0.0
        self._thread = threading.Thread(target=self._writer_loop, name="recorder-writer", daemon=True)
        self._thread.start()

    # -- the recorder interface used by the receiver ----------------------------

    def record(self, event: Mapping[str, object]) -> None:
        """Enqueue one market event for persistence (non-blocking)."""
        with self._lock:
            if len(self._queue) >= self._capacity:
                self.metrics.overflow += 1
                # Loud, manifest-visible loss accounting on the real recorder.
                note = getattr(self._recorder, "note_rejected_event", None)
                if note is not None:
                    note("recorder queue overflow: event not persisted")
                return
            self._queue.append((event, self._clock()))
            self.metrics.ingress += 1
            self.metrics.occupancy = len(self._queue)
            self.metrics.high_water = max(self.metrics.high_water, self.metrics.occupancy)
        self._wake.set()

    def record_control_event(self, event: Mapping[str, object]) -> object:
        """Control events are rare; delegate synchronously - after draining when
        the control event can FINALIZE the session.

        ``session_ended`` finalized the underlying recorder while this queue
        still held the session's tail; the writer then failed every remaining
        event with "cannot record after finalization". At the real event rate
        that silently cost the last seconds of EVERY session. Market events
        precede their session_ended on the wire, so draining first preserves
        exact ordering.
        """
        if str(event.get("type", "")) in ("session_ended", "disconnected"):
            self.drain()
        return self._recorder.record_control_event(event)

    def finalize(self, *, clean_shutdown: bool, reason: str | None = None) -> None:
        """Drain the queue completely, stop the writer, then finalize.

        A segment that lost events (write failures or queue overflow) can NEVER
        finalize as clean, however the caller asked - a crash or a failed write
        must produce an unclean manifest, not a falsely complete session.
        """
        self.drain()
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=10)
        lost = self.lost_events + self.metrics.overflow
        if lost or self.fail_closed:
            clean_shutdown = False
            reason = (
                f"recorder lost {lost} event(s): "
                f"{self.lost_events} write failure(s), {self.metrics.overflow} queue overflow(s)"
            )
        self._recorder.finalize(clean_shutdown=clean_shutdown, reason=reason)

    def drain(self) -> None:
        """Block until every queued event has been persisted."""
        while True:
            with self._lock:
                empty = not self._queue
            if empty:
                return
            self._wake.set()
            time.sleep(0.005)

    def __getattr__(self, name: str) -> object:
        """Delegate every other recorder attribute/method to the real recorder."""
        return getattr(self._recorder, name)

    # -- metrics -----------------------------------------------------------------

    def metrics_snapshot(self) -> PipelineMetricsSnapshot:
        """Return the GUI-facing measurement snapshot."""
        now = self._clock()
        ingress_rate = persisted_rate = 0.0
        window = [entry for entry in self._rate_window if now - entry[0] <= 10.0]
        if len(window) >= 2:
            span = window[-1][0] - window[0][0]
            if span > 0:
                ingress_rate = (window[-1][1] - window[0][1]) / span
                persisted_rate = (window[-1][2] - window[0][2]) / span
        return PipelineMetricsSnapshot(
            recorder=self.metrics.snapshot(),
            ingress_events_per_second=round(ingress_rate, 1),
            persisted_events_per_second=round(persisted_rate, 1),
            last_batch_size=self.last_batch_size,
            last_flush_duration_ms=round(self.last_flush_duration_ms, 2),
            flush_failures=self.flush_failures,
            end_to_end_lag_ms=round(self.end_to_end_lag_ms, 2),
        )

    # -- writer thread -------------------------------------------------------------

    def _writer_loop(self) -> None:
        while not self._stop.is_set() or self._has_pending():
            self._wake.wait(timeout=0.25)
            self._wake.clear()
            batch: list[tuple[Mapping[str, object], float]] = []
            with self._lock:
                while self._queue and len(batch) < self._batch_size:
                    batch.append(self._queue.popleft())
                self.metrics.occupancy = len(self._queue)
            if not batch:
                continue
            started = self._clock()
            persisted = self._write_batch(batch)
            if persisted < len(batch):
                # Fail closed: the un-persisted events are REAL loss. Count them,
                # push the reason into the session manifest, and mark the segment
                # unclean so it can never be presented as a complete recording.
                continue
            finished = self._clock()
            self.metrics.egress += len(batch)
            self.last_batch_size = len(batch)
            self.last_flush_duration_ms = (finished - started) * 1000.0
            self.end_to_end_lag_ms = (finished - batch[0][1]) * 1000.0
            self._rate_window.append((finished, self.metrics.ingress, self.metrics.egress))

    def _write_batch(self, batch: list[tuple[Mapping[str, object], float]]) -> int:
        """Persist a batch event-by-event with bounded retries; return count written.

        A single bad event must not discard the whole batch (the old behaviour
        lost every queued event and only bumped a counter). Each event is retried
        briefly; an event that still cannot be written is counted as real loss,
        recorded on the session manifest, and marks the recording unclean.
        """
        written = 0
        for event, _enqueued in batch:
            for attempt in range(self._max_write_attempts):
                try:
                    self._write(event)
                    written += 1
                    break
                except Exception as error:  # noqa: BLE001 - retry, then fail closed
                    if attempt + 1 >= self._max_write_attempts:
                        self.flush_failures += 1
                        self.lost_events += 1
                        self.fail_closed = True
                        note = getattr(self._recorder, "note_rejected_event", None)
                        if note is not None:
                            note(f"recorder write failed after retries: {type(error).__name__}: {error}")
                        break
                    time.sleep(0.005 * (attempt + 1))
        return written

    def _has_pending(self) -> bool:
        with self._lock:
            return bool(self._queue)
