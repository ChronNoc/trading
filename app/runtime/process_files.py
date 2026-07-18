"""Process-isolation primitives: singleton lock, status file, stop request.

These files under ``runtime/`` are the ONLY coupling between the backend, the
GUI, and the supervisor. The GUI never holds a handle into the backend process,
so closing or restarting the GUI is physically incapable of interrupting
capture — the isolation is structural, not behavioural.

* ``backend.lock`` — the backend's PID. A second backend refuses to start while
  the PID is alive; a stale lock (dead PID: crash or forced kill) is reported
  and cleaned, which doubles as the forced-shutdown marker.
* ``status.json`` — the backend's heartbeat: an atomically-replaced JSON
  document with the encoded AppSnapshot plus backend PID and wall-clock
  heartbeat. A reader treats a stale heartbeat as a dead/hung backend and says
  so; it can never render a crashed backend as healthy.
* ``stop.request`` — a clean-shutdown request from the supervisor/CLI. The
  backend polls it and runs its normal drain path (recorder finalize, feed
  drain, paper flatten).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from app.database.recorder import replace_with_retry, unique_temp_path

DEFAULT_RUNTIME_DIR = Path("runtime")
LOCK_NAME = "backend.lock"
STATUS_NAME = "status.json"
STOP_NAME = "stop.request"
# A backend that has not heartbeat within this window is dead or hung.
HEARTBEAT_STALE_SECONDS = 6.0


def pid_alive(pid: int) -> bool:
    """Return whether ``pid`` refers to a live process (Windows + POSIX)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # exists, but we lack rights - definitely alive
    except OSError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class LockResult:
    """Outcome of a singleton-lock attempt."""

    acquired: bool
    holder_pid: int
    stale_lock_cleaned: bool
    reason: str


class SingletonLock:
    """PID-file singleton: one backend per runtime directory, ever."""

    def __init__(self, runtime_dir: Path | str = DEFAULT_RUNTIME_DIR) -> None:
        """Bind to (and create) the runtime directory."""
        self._dir = Path(runtime_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / LOCK_NAME
        self._owned = False

    @property
    def path(self) -> Path:
        """The lock file location."""
        return self._path

    def acquire(self, pid: int | None = None) -> LockResult:
        """Try to become THE backend. Duplicate launches are refused loudly."""
        own_pid = os.getpid() if pid is None else pid
        stale_cleaned = False
        if self._path.is_file():
            try:
                holder = int(self._path.read_text(encoding="utf-8").strip() or 0)
            except ValueError:
                holder = 0
            if holder and pid_alive(holder) and holder != own_pid:
                return LockResult(
                    acquired=False, holder_pid=holder, stale_lock_cleaned=False,
                    reason=f"backend already running (pid {holder})",
                )
            # Dead holder: the previous backend crashed or was force-killed.
            stale_cleaned = True
            self._path.unlink(missing_ok=True)
        temporary = unique_temp_path(self._path, ".tmp")
        temporary.write_text(str(own_pid), encoding="utf-8")
        try:
            # O_EXCL-style claim: rename fails-soft if a racer won; re-check.
            replace_with_retry(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)
        self._owned = True
        reason = "acquired"
        if stale_cleaned:
            reason = "acquired after cleaning a stale lock (previous backend did not exit cleanly)"
        return LockResult(acquired=True, holder_pid=own_pid,
                          stale_lock_cleaned=stale_cleaned, reason=reason)

    def release(self) -> None:
        """Drop the lock (only if we own it)."""
        if self._owned:
            self._path.unlink(missing_ok=True)
            self._owned = False

    def holder(self) -> int:
        """Return the current lock holder's PID (0 = none)."""
        if not self._path.is_file():
            return 0
        try:
            return int(self._path.read_text(encoding="utf-8").strip() or 0)
        except ValueError:
            return 0


class StatusFile:
    """Atomic heartbeat/status document shared backend -> GUI/supervisor."""

    def __init__(self, runtime_dir: Path | str = DEFAULT_RUNTIME_DIR) -> None:
        """Bind to the runtime directory (created if missing)."""
        self._dir = Path(runtime_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / STATUS_NAME

    @property
    def path(self) -> Path:
        """The status file location."""
        return self._path

    def write(self, snapshot_json: str, *, backend_pid: int | None = None,
              state: str = "RUNNING") -> None:
        """Atomically publish one heartbeat (no fsync: this is a heartbeat,
        not a ledger - losing the last beat in a crash is expected and the
        staleness check covers it)."""
        document = json.dumps({
            "backend_pid": os.getpid() if backend_pid is None else backend_pid,
            "heartbeat_unix": time.time(),
            "state": state,
            "snapshot": snapshot_json,
        })
        temporary = unique_temp_path(self._path, ".tmp")
        temporary.write_text(document, encoding="utf-8")
        try:
            replace_with_retry(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)

    def read(self) -> dict[str, object] | None:
        """Return the last published document, or None when absent/torn."""
        if not self._path.is_file():
            return None
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None  # mid-replace read: the next poll will succeed

    def staleness_seconds(self, document: dict[str, object] | None = None) -> float | None:
        """Age of the last heartbeat, or None when no status exists."""
        payload = self.read() if document is None else document
        if payload is None:
            return None
        return max(0.0, time.time() - float(payload.get("heartbeat_unix", 0.0)))

    def backend_alive(self) -> bool:
        """Fresh heartbeat AND a live PID - both, or the backend is down."""
        payload = self.read()
        if payload is None:
            return False
        age = self.staleness_seconds(payload)
        if age is None or age > HEARTBEAT_STALE_SECONDS:
            return False
        return pid_alive(int(payload.get("backend_pid", 0) or 0))


class StopRequest:
    """The clean-shutdown handshake between supervisor/CLI and backend."""

    def __init__(self, runtime_dir: Path | str = DEFAULT_RUNTIME_DIR) -> None:
        """Bind to the runtime directory."""
        self._path = Path(runtime_dir) / STOP_NAME
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def request(self, reason: str = "stop requested") -> None:
        """Ask the backend to shut down cleanly."""
        self._path.write_text(reason, encoding="utf-8")

    def pending(self) -> bool:
        """Whether a stop has been requested."""
        return self._path.is_file()

    def clear(self) -> None:
        """Consume the request (the backend clears it once honoured)."""
        self._path.unlink(missing_ok=True)
