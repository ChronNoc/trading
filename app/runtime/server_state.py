"""Thread-safe holder for the *actual* bound receiver-server state.

The GUI must not infer "receiver listening" from the mere existence of a runtime
controller. This holder is set only when the WebSocket server has genuinely bound
its socket (host/port), and cleared when it stops, so the pipeline panel reflects
the real bind state. It is shared between the asyncio receiver thread and the GUI
thread, hence the lock.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ReceiverBinding:
    """A snapshot of whether the receiver socket is actually bound."""

    listening: bool = False
    host: str = ""
    port: int = 0

    @property
    def url(self) -> str:
        """Return the bound URL, or an empty string when not listening."""
        return f"ws://{self.host}:{self.port}/bookmap" if self.listening else ""


class ReceiverStatusHolder:
    """Mutable, thread-safe holder the receiver updates and the GUI reads."""

    def __init__(self) -> None:
        """Create an unbound holder."""
        self._lock = threading.Lock()
        self._binding = ReceiverBinding()

    def mark_bound(self, host: str, port: int) -> None:
        """Record that the server socket is genuinely bound and listening."""
        with self._lock:
            self._binding = ReceiverBinding(listening=True, host=host, port=port)

    def mark_unbound(self) -> None:
        """Record that the server is no longer listening."""
        with self._lock:
            self._binding = ReceiverBinding()

    def snapshot(self) -> ReceiverBinding:
        """Return the current binding snapshot (safe from any thread)."""
        with self._lock:
            return self._binding
