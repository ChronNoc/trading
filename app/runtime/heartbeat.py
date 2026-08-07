"""Decoupled liveness heartbeat for the capture backend.

The supervisor judges the backend alive from the freshness of the
``runtime/status.json`` heartbeat. Publishing that heartbeat from the backend's
main loop coupled liveness to the cost of BUILDING the full GUI snapshot: under
real-time load (tens of thousands of events/second) the snapshot build plus GIL
contention pushed the status write past the staleness window, so a perfectly
healthy, recording backend looked dead and was needlessly restarted - losing
in-flight events and invalidating the session.

:class:`HeartbeatWriter` publishes the heartbeat on its own thread from an
already-encoded snapshot supplied by a provider (a :class:`SnapshotCache` the
main loop refreshes best-effort). Its per-write work is tiny - format a small
document around a cached string and atomically replace one file - so it keeps
the heartbeat fresh even while the main loop is busy, and a stalled or failing
snapshot build never makes a live backend look dead. Liveness (does the process
still beat?) is now independent of freshness (is the snapshot up to date?).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol


class _StatusSink(Protocol):
    def write(  # noqa: D401, D102 - structural type for StatusFile.write
        self, snapshot_json: str, *, identity: object, state: str,
    ) -> None: ...


class SnapshotCache:
    """Thread-safe holder for the latest already-encoded snapshot string."""

    __slots__ = ("_lock", "_value")

    def __init__(self, initial: str) -> None:
        """Seed the cache with an initial encoded snapshot."""
        self._lock = threading.Lock()
        self._value = initial

    def set(self, value: str) -> None:
        """Replace the cached snapshot (called by the main loop, best-effort)."""
        with self._lock:
            self._value = value

    def get(self) -> str:
        """Return the latest cached snapshot (called by the heartbeat thread)."""
        with self._lock:
            return self._value


class HeartbeatWriter:
    """Publishes a liveness heartbeat on its own thread, decoupled from the build.

    ``snapshot_provider`` must be cheap - typically :meth:`SnapshotCache.get`.
    The writer never builds a snapshot itself, so its cadence is unaffected by
    how long the expensive build takes or how contended the GIL is under load.
    """

    def __init__(
        self,
        status: _StatusSink,
        identity: object,
        snapshot_provider: Callable[[], str],
        *,
        interval_seconds: float,
        state: str = "RUNNING",
        logger: object | None = None,
    ) -> None:
        """Create a writer; ``start`` launches the thread, ``stop`` ends it."""
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._status = status
        self._identity = identity
        self._snapshot_provider = snapshot_provider
        self._interval = float(interval_seconds)
        self._state = state
        self._logger = logger
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._consecutive_failures = 0

    @property
    def consecutive_failures(self) -> int:
        """Number of status writes that have failed back-to-back (0 when healthy)."""
        return self._consecutive_failures

    def beat_once(self) -> bool:
        """Publish one heartbeat now; return whether the write succeeded."""
        try:
            self._status.write(
                self._snapshot_provider(), identity=self._identity, state=self._state,
            )
            self._consecutive_failures = 0
            return True
        except Exception as error:  # noqa: BLE001 - a failed beat must not crash capture
            self._consecutive_failures += 1
            if self._logger is not None:
                self._logger.error(
                    "heartbeat write failed (%d in a row): %s",
                    self._consecutive_failures, error,
                )
            return False

    def start(self) -> None:
        """Publish an immediate heartbeat, then beat every interval on a thread."""
        if self._thread is not None:
            raise RuntimeError("HeartbeatWriter already started")
        self.beat_once()  # fresh status before the loop, so no startup stale gap
        self._thread = threading.Thread(
            target=self._loop, name="mnq-backend-heartbeat", daemon=True,
        )
        self._thread.start()

    def _loop(self) -> None:
        # wait() returns True the moment stop is set, so shutdown is prompt and
        # the interval is otherwise honoured without a busy loop.
        while not self._stop.wait(self._interval):
            self.beat_once()

    def stop(self, *, timeout: float = 2.0) -> None:
        """Stop beating and join the thread. Idempotent.

        Callers MUST stop the writer before publishing a terminal state
        (STOPPED_*/FAILED_*), or a late ``RUNNING`` beat could overwrite it.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
            self._thread = None
