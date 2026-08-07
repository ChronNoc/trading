"""Durable process ownership, heartbeat, and shutdown-request primitives.

The previous implementation treated a replaceable PID file as a singleton
lock.  Two starters could both pass the read/check phase, and PID reuse could
make a dead backend appear alive.  ``SingletonLock`` now holds an operating
system byte-range/file lock for its entire lifetime and publishes versioned
process identity as metadata only after that lease is owned.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Mapping

from app.database.recorder import replace_with_retry, unique_temp_path

DEFAULT_RUNTIME_DIR = Path("runtime")
LOCK_NAME = "backend.lock"
LOCK_GUARD_NAME = "backend.lock.guard"
STATUS_NAME = "status.json"
STOP_NAME = "stop.request"
PROCESS_IDENTITY_VERSION = 1
STATUS_DOCUMENT_VERSION = 2
STOP_REQUEST_VERSION = 1
HEARTBEAT_STALE_SECONDS = 6.0

# Requester stamped on the stop the SUPERVISOR writes to bounce a hung backend
# during recovery. It is the ONE stop a supervisor must not read as an external
# request to shut the whole service down - otherwise the supervisor aborts its
# own recovery and refuses to restart the backend. See
# ``StopRequest.externally_requested``.
SUPERVISOR_RECOVERY_REQUESTER = "backend_supervisor"


def pid_alive(pid: int) -> bool:
    """Return whether ``pid`` currently refers to a running process."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_process_creation_time(pid) is not None
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def process_creation_time(pid: int) -> float | None:
    """Return process creation time as a Unix timestamp when available."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        return _windows_process_creation_time(pid)
    # Linux procfs is used only as an identity-strengthening signal.  Falling
    # back to PID liveness keeps the module portable on macOS and other POSIX.
    try:
        stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        boot_line = next(
            line for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines()
            if line.startswith("btime ")
        )
        boot_time = float(boot_line.split()[1])
        ticks = float(os.sysconf("SC_CLK_TCK"))
        return boot_time + float(stat_fields[21]) / ticks
    except (OSError, StopIteration, ValueError, IndexError):
        return None


def _windows_process_creation_time(pid: int) -> float | None:
    """Read a Windows process creation time without third-party packages."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        process_query_limited_information, False, pid,
    )
    if not handle:
        return None
    creation = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    try:
        ok = ctypes.windll.kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        filetime = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return filetime / 10_000_000 - 11_644_473_600
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def process_identity_matches(pid: int, started_unix: float | None) -> bool:
    """Return whether PID and creation time identify the same live process."""
    observed = process_creation_time(pid)
    if observed is None:
        return False if sys.platform == "win32" else pid_alive(pid)
    if started_unix is None:
        return True
    return abs(observed - started_unix) < 1.0


def configuration_fingerprint(values: Mapping[str, object]) -> str:
    """Return a stable hash for backend-defining configuration values."""
    encoded = json.dumps(dict(values), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """Versioned identity preventing stale PID and incompatible attachment."""

    pid: int
    process_started_unix: float | None
    nonce: str
    executable: str
    app_version: str
    git_commit: str
    config_fingerprint: str
    version: int = PROCESS_IDENTITY_VERSION

    @classmethod
    def current(
        cls,
        *,
        config_fingerprint: str = "",
        app_version: str = "0.1.0",
        git_commit: str = "unknown",
    ) -> "ProcessIdentity":
        """Build identity for the current process."""
        pid = os.getpid()
        return cls(
            pid=pid,
            process_started_unix=process_creation_time(pid),
            nonce=uuid.uuid4().hex,
            executable=str(Path(sys.executable).resolve()),
            app_version=app_version,
            git_commit=git_commit,
            config_fingerprint=config_fingerprint,
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "ProcessIdentity":
        """Decode identity metadata, rejecting malformed versions."""
        version = int(payload.get("version", 0) or 0)
        if version != PROCESS_IDENTITY_VERSION:
            raise ValueError(f"unsupported process identity version {version}")
        started = payload.get("process_started_unix")
        return cls(
            pid=int(payload.get("pid", 0) or 0),
            process_started_unix=None if started is None else float(started),
            nonce=str(payload.get("nonce", "")),
            executable=str(payload.get("executable", "")),
            app_version=str(payload.get("app_version", "")),
            git_commit=str(payload.get("git_commit", "")),
            config_fingerprint=str(payload.get("config_fingerprint", "")),
            version=version,
        )

    @property
    def alive(self) -> bool:
        """Return whether this identity still names the same process."""
        return process_identity_matches(self.pid, self.process_started_unix)


@dataclass(frozen=True, slots=True)
class LockResult:
    """Outcome of a singleton-lock attempt."""

    acquired: bool
    holder_pid: int
    stale_lock_cleaned: bool
    reason: str
    identity: ProcessIdentity | None = None


class SingletonLock:
    """One backend lease per runtime directory, enforced by the operating system."""

    def __init__(
        self,
        runtime_dir: Path | str = DEFAULT_RUNTIME_DIR,
        *,
        lock_name: str = LOCK_NAME,
        guard_name: str = LOCK_GUARD_NAME,
        owner_label: str = "backend",
    ) -> None:
        """Bind to the runtime directory without acquiring its lease."""
        self._dir = Path(runtime_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / lock_name
        self._guard_path = self._dir / guard_name
        self._owner_label = owner_label
        self._guard: BinaryIO | None = None
        self._identity: ProcessIdentity | None = None

    @property
    def path(self) -> Path:
        """Return the human-readable identity metadata path."""
        return self._path

    def acquire(
        self,
        pid: int | None = None,
        *,
        identity: ProcessIdentity | None = None,
    ) -> LockResult:
        """Acquire the exclusive OS lease and publish process identity."""
        if self._guard is not None:
            own = self._identity
            return LockResult(
                acquired=True,
                holder_pid=own.pid if own is not None else os.getpid(),
                stale_lock_cleaned=False,
                reason="already acquired by this lock object",
                identity=own,
            )
        guard = self._guard_path.open("a+b")
        if self._guard_path.stat().st_size == 0:
            try:
                guard.seek(0)
                guard.write(b"0")
                guard.flush()
            except PermissionError:
                # Another Windows process locked byte zero between open/stat.
                guard.close()
                holder_identity = self.read_identity()
                holder = holder_identity.pid if holder_identity is not None else self.holder()
                return LockResult(
                    acquired=False,
                    holder_pid=holder,
                    stale_lock_cleaned=False,
                    reason=f"{self._owner_label} is already running and owns the runtime lease (pid {holder or 'unknown'})",
                    identity=holder_identity,
                )
        guard.seek(0)
        try:
            _lock_guard(guard)
        except OSError:
            guard.close()
            holder_identity = self.read_identity()
            holder = holder_identity.pid if holder_identity is not None else self.holder()
            return LockResult(
                acquired=False,
                holder_pid=holder,
                stale_lock_cleaned=False,
                reason=f"{self._owner_label} is already running and owns the runtime lease (pid {holder or 'unknown'})",
                identity=holder_identity,
            )

        previous = self.read_identity()
        stale_cleaned = previous is not None or self._path.exists()
        own_pid = os.getpid() if pid is None else pid
        own = identity or ProcessIdentity.current()
        if own.pid != own_pid:
            own = ProcessIdentity(
                pid=own_pid,
                process_started_unix=process_creation_time(own_pid),
                nonce=own.nonce,
                executable=own.executable,
                app_version=own.app_version,
                git_commit=own.git_commit,
                config_fingerprint=own.config_fingerprint,
            )
        _atomic_write_json(self._path, asdict(own))
        self._guard = guard
        self._identity = own
        reason = "acquired"
        if stale_cleaned:
            reason = "acquired after recovering an unleased identity (previous backend did not exit cleanly)"
        return LockResult(True, own.pid, stale_cleaned, reason, own)

    def release(self) -> None:
        """Release the lease and remove only this owner's metadata."""
        if self._guard is None:
            return
        current = self.read_identity()
        if current is not None and self._identity is not None and current.nonce == self._identity.nonce:
            self._path.unlink(missing_ok=True)
        _unlock_guard(self._guard)
        self._guard.close()
        self._guard = None
        self._identity = None

    def read_identity(self) -> ProcessIdentity | None:
        """Return current metadata, including backward-compatible PID files."""
        if not self._path.is_file():
            return None
        try:
            raw = self._path.read_text(encoding="utf-8").strip()
            if raw.isdigit():
                pid = int(raw)
                return ProcessIdentity(
                    pid=pid,
                    process_started_unix=process_creation_time(pid),
                    nonce="legacy",
                    executable="",
                    app_version="",
                    git_commit="",
                    config_fingerprint="",
                )
            return ProcessIdentity.from_payload(json.loads(raw))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def holder(self) -> int:
        """Return metadata PID, or zero when no valid identity exists."""
        identity = self.read_identity()
        return 0 if identity is None else identity.pid


def _lock_guard(handle: BinaryIO) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_guard(handle: BinaryIO) -> None:
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class StatusFile:
    """Atomic heartbeat/status document shared backend to GUI/supervisor."""

    def __init__(self, runtime_dir: Path | str = DEFAULT_RUNTIME_DIR) -> None:
        """Bind to the runtime directory."""
        self._dir = Path(runtime_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / STATUS_NAME

    @property
    def path(self) -> Path:
        """Return the status path."""
        return self._path

    def write(
        self,
        snapshot_json: str,
        *,
        backend_pid: int | None = None,
        state: str = "RUNNING",
        identity: ProcessIdentity | None = None,
        supervisor: Mapping[str, object] | None = None,
    ) -> None:
        """Atomically publish one versioned heartbeat document."""
        process = identity or ProcessIdentity.current()
        if backend_pid is not None and process.pid != backend_pid:
            process = ProcessIdentity(
                pid=backend_pid,
                process_started_unix=process_creation_time(backend_pid),
                nonce=process.nonce,
                executable=process.executable,
                app_version=process.app_version,
                git_commit=process.git_commit,
                config_fingerprint=process.config_fingerprint,
            )
        document: dict[str, object] = {
            "document_version": STATUS_DOCUMENT_VERSION,
            "backend_pid": process.pid,
            "process_started_unix": process.process_started_unix,
            "process_nonce": process.nonce,
            "config_fingerprint": process.config_fingerprint,
            "heartbeat_unix": time.time(),
            "state": state,
            "snapshot": snapshot_json,
        }
        if supervisor is not None:
            document["supervisor"] = dict(supervisor)
        _atomic_write_json(self._path, document)

    def read(self) -> dict[str, object] | None:
        """Return the last document, or ``None`` when absent/torn."""
        if not self._path.is_file():
            return None
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (json.JSONDecodeError, OSError):
            return None

    def staleness_seconds(self, document: dict[str, object] | None = None) -> float | None:
        """Return heartbeat age, or ``None`` when no status exists."""
        payload = self.read() if document is None else document
        if payload is None:
            return None
        try:
            return max(0.0, time.time() - float(payload.get("heartbeat_unix", 0.0)))
        except (TypeError, ValueError):
            return None

    def backend_alive(self, *, expected_fingerprint: str | None = None) -> bool:
        """Require fresh heartbeat, matching identity, and optional configuration."""
        payload = self.read()
        if payload is None:
            return False
        age = self.staleness_seconds(payload)
        if age is None or age > HEARTBEAT_STALE_SECONDS:
            return False
        if expected_fingerprint is not None and str(payload.get("config_fingerprint", "")) != expected_fingerprint:
            return False
        started = payload.get("process_started_unix")
        return process_identity_matches(
            int(payload.get("backend_pid", 0) or 0),
            None if started is None else float(started),
        )


class StopRequest:
    """Atomic, versioned clean-shutdown request."""

    def __init__(self, runtime_dir: Path | str = DEFAULT_RUNTIME_DIR) -> None:
        """Bind to the runtime directory."""
        self._path = Path(runtime_dir) / STOP_NAME
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def request(self, reason: str = "stop requested", *, requester: str = "cli") -> str:
        """Ask the backend to stop and return the request identifier."""
        request_id = uuid.uuid4().hex
        _atomic_write_json(self._path, {
            "version": STOP_REQUEST_VERSION,
            "request_id": request_id,
            "requested_at_unix": time.time(),
            "requester": requester,
            "reason": reason,
        })
        return request_id

    def read(self) -> dict[str, object] | None:
        """Return request details, accepting legacy plain-text requests."""
        if not self._path.is_file():
            return None
        try:
            raw = self._path.read_text(encoding="utf-8")
            value = json.loads(raw)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return {"version": 0, "request_id": "legacy", "reason": raw.strip()}
        except OSError:
            return None

    def pending(self) -> bool:
        """Return whether a stop request exists."""
        return self._path.is_file()

    def clear(self) -> None:
        """Consume the request once honoured."""
        self._path.unlink(missing_ok=True)

    def requester(self) -> str | None:
        """Return who asked to stop, ``""`` for an unattributed/legacy request, or None."""
        payload = self.read()
        if payload is None:
            return None
        value = payload.get("requester", "")
        return str(value)

    def externally_requested(self) -> bool:
        """Return whether a stop came from OUTSIDE the supervisor's own recovery.

        The supervisor must give up (stop and not restart) only for an external
        stop - a human's GUI/CLI request, or any non-recovery source. Its own
        recovery bounce (the stop it writes to replace a hung backend) carries
        :data:`SUPERVISOR_RECOVERY_REQUESTER` and must never be mistaken for one,
        or the supervisor aborts its own recovery. An unattributed/legacy request
        is treated as external (fail safe: honour an unexplained stop).
        """
        requester = self.requester()
        if requester is None:
            return False
        return requester != SUPERVISOR_RECOVERY_REQUESTER


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = unique_temp_path(path, ".tmp")
    temporary.write_text(json.dumps(dict(payload), sort_keys=True), encoding="utf-8")
    try:
        replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class CommandFile:
    """Bounded GUI -> backend command channel: one pending command file.

    The GUI writes ``runtime/execution_command.json`` atomically (uuid
    command_id + name + args); the backend consumes it exactly once per poll
    and hands it to the owning service, which de-duplicates by command_id. The
    file lives in the same user-owned runtime directory as the status file, so
    the trust boundary is the same filesystem ACL that already protects the
    lock and heartbeat.
    """

    NAME = "execution_command.json"

    def __init__(self, runtime_dir: Path | str = DEFAULT_RUNTIME_DIR) -> None:
        """Bind to the runtime directory (created if missing)."""
        self._path = Path(runtime_dir) / self.NAME
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        """The command file location."""
        return self._path

    def submit(self, name: str, args: dict[str, object] | None = None) -> str:
        """Write one command atomically; returns its command_id."""
        import uuid

        command_id = uuid.uuid4().hex
        payload = json.dumps({
            "command_id": command_id,
            "name": name,
            "args": dict(args or {}),
            "submitted_at_unix": time.time(),
            "submitter_pid": os.getpid(),
        })
        temporary = unique_temp_path(self._path, ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        try:
            replace_with_retry(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)
        return command_id

    def consume(self) -> dict[str, object] | None:
        """Take the pending command (exactly once), or None when absent/torn."""
        if not self._path.is_file():
            return None
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None  # mid-replace read: next poll gets it
        self._path.unlink(missing_ok=True)
        return payload if isinstance(payload, dict) else None
