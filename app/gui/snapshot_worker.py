"""Background snapshot polling for the Qt application.

Providers may read ``runtime/status.json`` or aggregate backend state.  They
therefore never run on Qt's event thread.  The worker emits immutable snapshots
through a queued Qt signal; rendering remains the GUI thread's only job.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal

from app.gui.view_models import AppSnapshot

SnapshotProvider = Callable[[], AppSnapshot]


class SnapshotWorker(QObject):
    """Poll a snapshot provider on one bounded-lifetime background thread."""

    snapshot_ready = Signal(object)
    provider_failed = Signal(str)

    def __init__(self, provider: SnapshotProvider, *, interval_seconds: float = 0.15) -> None:
        """Store the provider and polling interval without starting work."""
        super().__init__()
        self._provider = provider
        self._interval_seconds = max(0.05, interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        """Return whether the polling thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start at most one polling thread."""
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="mnq-gui-snapshot-reader",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_seconds: float = 1.0) -> bool:
        """Request stop and return whether the worker exited in time."""
        self._stop.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=max(0.0, timeout_seconds))
        return not thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                snapshot = self._provider()
                if not isinstance(snapshot, AppSnapshot):
                    raise TypeError("snapshot provider returned a non-AppSnapshot value")
                self.snapshot_ready.emit(snapshot)
            except Exception as error:  # noqa: BLE001 - provider failure must not kill the GUI
                self.provider_failed.emit(f"{type(error).__name__}: {error}")
            self._stop.wait(self._interval_seconds)
