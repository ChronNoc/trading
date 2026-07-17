"""Cross-thread shutdown signalling so capture drains instead of being killed.

The receiver runs an asyncio loop on a background thread while Qt owns the main
thread. Marking that thread ``daemon=True`` and letting the process exit kills it
at an arbitrary instruction: in-flight recorder batches are lost, the session is
never finalized (so no automatic episode build), and an open simulated position
is abandoned rather than closed.

This is the handshake that prevents that. The GUI asks for a stop, the receiver
loop wakes, runs its own ``finally`` (stop research, stop the controller, close
the server, flush the recorder), and reports that it drained. The GUI waits a
bounded time and reports honestly if the drain did not complete.

Bounded is the point: a shutdown that can hang forever is not a shutdown, so the
wait always returns and an unclean exit is *reported* rather than hidden.
"""

from __future__ import annotations

import threading
from typing import Any


class ShutdownSignal:
    """A thread-safe stop request for an asyncio loop owned by another thread."""

    def __init__(self) -> None:
        """Create an unbound, unrequested signal."""
        self._lock = threading.Lock()
        self._loop: Any = None
        self._future: Any = None
        self._requested = False
        self._drained = threading.Event()
        self._drain_error: str = ""

    # -- receiver side --------------------------------------------------------

    def bind(self, loop: Any, future: Any) -> None:
        """Attach the running loop and the future the receiver awaits.

        If a stop was already requested before binding (a very fast close), it is
        honoured immediately rather than lost.
        """
        with self._lock:
            self._loop, self._future = loop, future
            already_requested = self._requested
        if already_requested:
            self._resolve()

    def mark_drained(self, error: str = "") -> None:
        """Called by the receiver once its shutdown path has fully completed."""
        self._drain_error = error
        self._drained.set()

    # -- GUI / caller side ----------------------------------------------------

    def request_stop(self) -> None:
        """Ask the receiver loop to stop. Safe from any thread; idempotent."""
        with self._lock:
            self._requested = True
            bound = self._future is not None
        if bound:
            self._resolve()

    def wait_for_drain(self, timeout: float) -> bool:
        """Block up to ``timeout`` seconds for a real drain. Never hangs.

        Returns True only if capture actually finished its shutdown path.
        """
        if not self._drained.wait(timeout=timeout):
            return False
        return not self._drain_error

    @property
    def stop_requested(self) -> bool:
        """Whether a stop has been requested."""
        with self._lock:
            return self._requested

    @property
    def drain_error(self) -> str:
        """The receiver's shutdown error, if its drain path failed."""
        return self._drain_error

    def _resolve(self) -> None:
        with self._lock:
            loop, future = self._loop, self._future
        if loop is None or future is None:
            return

        def _set() -> None:
            if not future.done():
                future.set_result(None)

        try:
            loop.call_soon_threadsafe(_set)
        except RuntimeError:
            # The loop is already closed: there is nothing left to stop.
            self._drained.set()
