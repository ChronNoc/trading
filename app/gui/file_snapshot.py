"""GUI-side snapshot provider that reads the backend's status file.

This is the whole GUI/backend coupling: read a JSON file, decode a typed
AppSnapshot. The GUI holds no socket, no thread, and no object of the backend's,
so closing or restarting the GUI cannot interrupt capture even in principle.

Honesty rule: a dead backend must never look healthy. When the heartbeat is
stale or the PID is gone, every component is overridden to FAILED with the
exact reason and age - the GUI renders the truth of the file, not a hope.
"""

from __future__ import annotations

from pathlib import Path

from app.gui.snapshot_codec import decode_snapshot
from app.gui.view_models import AppSnapshot, ComponentHealth, Health
from app.runtime.process_files import (
    HEARTBEAT_STALE_SECONDS,
    StatusFile,
    pid_alive,
)


class FileSnapshotProvider:
    """Callable snapshot provider backed by ``runtime/status.json``."""

    def __init__(self, runtime_dir: Path | str = Path("runtime")) -> None:
        """Bind to the backend's runtime directory."""
        self._status = StatusFile(runtime_dir)
        self._last_good: AppSnapshot | None = None

    def __call__(self) -> AppSnapshot:
        """Return the freshest snapshot, or an honest backend-down view."""
        document = self._status.read()
        if document is None:
            return self._down("no backend status file - backend not running?")
        age = self._status.staleness_seconds(document) or 0.0
        pid = int(document.get("backend_pid", 0) or 0)
        state = str(document.get("state", "RUNNING"))
        try:
            snapshot = decode_snapshot(str(document.get("snapshot", "")))
        except Exception as error:  # noqa: BLE001 - a torn read must not kill the GUI
            return self._down(f"status file unreadable: {type(error).__name__}")
        self._last_good = snapshot

        if state.startswith("STOPPED"):
            return self._overlay(snapshot, f"backend stopped ({state})")
        if age > HEARTBEAT_STALE_SECONDS:
            return self._overlay(
                snapshot,
                f"backend heartbeat stale ({age:.0f}s) - process hung or dead",
            )
        if not pid_alive(pid):
            return self._overlay(snapshot, f"backend pid {pid} is gone (crashed?)")
        return snapshot

    def backend_alive(self) -> bool:
        """Whether a live backend is publishing fresh heartbeats."""
        return self._status.backend_alive()

    def _overlay(self, snapshot: AppSnapshot, reason: str) -> AppSnapshot:
        """Stamp a backend-level failure over an otherwise-decoded snapshot.

        The stale snapshot's numbers stay visible (they are the last truth),
        but the lifecycle and components state the failure loudly.
        """
        from dataclasses import replace

        return replace(
            snapshot,
            lifecycle_state="BACKEND_DOWN",
            blocker=reason,
            components=(
                ComponentHealth("backend", Health.FAIL, reason),
                *snapshot.components,
            ),
        )

    def _down(self, reason: str) -> AppSnapshot:
        base = self._last_good if self._last_good is not None else AppSnapshot()
        return self._overlay(base, reason)
