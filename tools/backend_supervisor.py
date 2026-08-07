"""Persistent owner and recovery supervisor for the capture backend.

The GUI starts (or attaches to) this supervisor, never a one-shot detached
backend.  The supervisor owns a separate OS lease, verifies backend process
identity and configuration, and restarts a crashed or hung backend with bounded
backoff.  An intentional stop request is never restarted.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

BACKOFF_INITIAL_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 60.0
# Bump whenever capture/protocol semantics change. Including this in the process
# fingerprint prevents a GUI launched from new source from silently attaching to
# an old detached backend that is still parsing with obsolete capture logic.
CAPTURE_RUNTIME_REVISION = "capture-reliability-v2"
# How long a just-spawned backend may take to publish its first healthy
# heartbeat.  Cold starts import the full app and can exceed 30s on a
# CPU-contended machine; giving up early orphans a backend that is still
# booting, so this window is deliberately generous.
STARTUP_HANDSHAKE_SECONDS = 90.0
SUPERVISOR_LOCK_NAME = "supervisor.lock"
SUPERVISOR_GUARD_NAME = "supervisor.lock.guard"


@dataclass(frozen=True, slots=True)
class BackendSpec:
    """Backend-defining settings used for spawn and attachment checks."""

    host: str = "127.0.0.1"
    port: int = 8765
    delayed_data_minutes: int = 0
    output_root: str = "data/raw"
    report_root: str = "data/reports"
    session_config: str = "config/session_profiles.yaml"
    paper_ledger_path: str = "data/paper/paper_trades.jsonl"
    log_dir: str = "logs"
    processed_root: str = "data/processed"
    labels_root: str = "data/labels"
    research_state_root: str = "data/research_state"
    models_root: str = "data/models"

    @property
    def fingerprint(self) -> str:
        """Return a stable fingerprint for attachment compatibility."""
        from app.runtime.process_files import configuration_fingerprint

        return configuration_fingerprint({
            "capture_runtime_revision": CAPTURE_RUNTIME_REVISION,
            "host": self.host,
            "port": self.port,
            "delayed_data_minutes": self.delayed_data_minutes,
            "output_root": str(Path(self.output_root).resolve()),
            "report_root": str(Path(self.report_root).resolve()),
            "session_config": str(Path(self.session_config).resolve()),
            "paper_ledger_path": str(Path(self.paper_ledger_path).resolve()),
            "log_dir": str(Path(self.log_dir).resolve()),
            "processed_root": str(Path(self.processed_root).resolve()),
            "labels_root": str(Path(self.labels_root).resolve()),
            "research_state_root": str(Path(self.research_state_root).resolve()),
            "models_root": str(Path(self.models_root).resolve()),
        })

    def backend_args(self, runtime_dir: Path) -> list[str]:
        """Build the authoritative backend CLI arguments."""
        return [
            "--runtime-dir", str(runtime_dir),
            "--host", self.host,
            "--port", str(self.port),
            "--delayed-data-minutes", str(self.delayed_data_minutes),
            "--output-root", self.output_root,
            "--report-root", self.report_root,
            "--session-config", self.session_config,
            "--paper-ledger-path", self.paper_ledger_path,
            "--log-dir", self.log_dir,
            "--processed-root", self.processed_root,
            "--labels-root", self.labels_root,
            "--research-state-root", self.research_state_root,
            "--models-root", self.models_root,
            "--config-fingerprint", self.fingerprint,
        ]

    def supervisor_args(self, runtime_dir: Path) -> list[str]:
        """Build arguments for a detached persistent supervisor."""
        return [
            "--runtime-dir", str(runtime_dir),
            "--host", self.host,
            "--port", str(self.port),
            "--delayed-data-minutes", str(self.delayed_data_minutes),
            "--output-root", self.output_root,
            "--report-root", self.report_root,
            "--session-config", self.session_config,
            "--paper-ledger-path", self.paper_ledger_path,
            "--log-dir", self.log_dir,
            "--processed-root", self.processed_root,
            "--labels-root", self.labels_root,
            "--research-state-root", self.research_state_root,
            "--models-root", self.models_root,
        ]


def _log(runtime_dir: Path, message: str) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now(UTC).isoformat()} {message}\n"
    with (runtime_dir / "supervisor.log").open("a", encoding="utf-8") as handle:
        handle.write(line)
    print(message, flush=True)


def _detached_creation_flags() -> int:
    if sys.platform != "win32":
        return 0
    return subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)


def spawn_backend(runtime_dir: Path, *, spec: BackendSpec | None = None) -> subprocess.Popen[bytes]:
    """Start one detached backend and close the parent's log handle."""
    selected = spec or BackendSpec()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    handle = (runtime_dir / "backend.out").open("ab", buffering=0)
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "tools.start_backend", *selected.backend_args(runtime_dir)],
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=_detached_creation_flags(),
            cwd=str(Path(__file__).resolve().parent.parent),
        )
    finally:
        handle.close()


def spawn_supervisor(runtime_dir: Path, *, spec: BackendSpec) -> subprocess.Popen[bytes]:
    """Start the persistent supervisor detached from the GUI process."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    handle = (runtime_dir / "supervisor.out").open("ab", buffering=0)
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "tools.backend_supervisor", *spec.supervisor_args(runtime_dir)],
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=_detached_creation_flags(),
            cwd=str(Path(__file__).resolve().parent.parent),
        )
    finally:
        handle.close()


def _supervisor_lock(runtime_dir: Path):  # noqa: ANN202
    from app.runtime.process_files import SingletonLock

    return SingletonLock(
        runtime_dir,
        lock_name=SUPERVISOR_LOCK_NAME,
        guard_name=SUPERVISOR_GUARD_NAME,
        owner_label="supervisor",
    )


def ensure_supervisor(
    runtime_dir: Path,
    *,
    spec: BackendSpec,
    wait_seconds: float = STARTUP_HANDSHAKE_SECONDS,
) -> bool:
    """Ensure a persistent supervisor and compatible healthy backend exist."""
    from app.runtime.process_files import StatusFile

    runtime_dir.mkdir(parents=True, exist_ok=True)
    supervisor_identity = _supervisor_lock(runtime_dir).read_identity()
    status = StatusFile(runtime_dir)
    if supervisor_identity is not None and supervisor_identity.alive:
        if status.backend_alive(expected_fingerprint=spec.fingerprint):
            _log(runtime_dir, f"supervisor already running (pid {supervisor_identity.pid}); attaching")
            return True
        _log(runtime_dir, f"supervisor pid {supervisor_identity.pid} is recovering the backend")
    else:
        spawn_supervisor(runtime_dir, spec=spec)

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if status.backend_alive(expected_fingerprint=spec.fingerprint):
            identity = _supervisor_lock(runtime_dir).read_identity()
            _log(runtime_dir, f"supervisor/backend ready (supervisor pid {identity.pid if identity else 'unknown'})")
            return True
        time.sleep(0.25)
    _log(runtime_dir, "supervisor did not publish a compatible healthy backend within the startup window")
    return False


def ensure_backend(
    runtime_dir: Path,
    *,
    port: int = 8765,
    wait_seconds: float = 30.0,
    delayed_data_minutes: int = 0,
    spec: BackendSpec | None = None,
) -> bool:
    """Ensure one compatible backend exists (used internally by supervisor)."""
    from app.runtime.process_files import SingletonLock, StatusFile

    selected = spec or BackendSpec(port=port, delayed_data_minutes=delayed_data_minutes)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    status = StatusFile(runtime_dir)
    lock = SingletonLock(runtime_dir)
    holder = lock.read_identity()
    if holder is not None and holder.alive:
        if status.backend_alive(expected_fingerprint=selected.fingerprint):
            _log(runtime_dir, f"compatible backend already running (pid {holder.pid})")
            return True
        _log(runtime_dir, f"backend pid {holder.pid} is stale or incompatible; recovering")
        _recover_backend(runtime_dir, holder)

    spawn_backend(runtime_dir, spec=selected)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if status.backend_alive(expected_fingerprint=selected.fingerprint):
            current = lock.read_identity()
            _log(runtime_dir, f"backend started (pid {current.pid if current else 'unknown'})")
            return True
        time.sleep(0.25)
    _log(runtime_dir, "backend did not become healthy within the startup window")
    return False


def _recover_backend(runtime_dir: Path, identity: object, *, grace_seconds: float = 8.0) -> None:
    """Request graceful stop, then terminate only the verified same process."""
    from app.runtime.process_files import SUPERVISOR_RECOVERY_REQUESTER, StopRequest

    pid = int(getattr(identity, "pid", 0))
    StopRequest(runtime_dir).request(
        "supervisor recovery: stale heartbeat or incompatible configuration",
        requester=SUPERVISOR_RECOVERY_REQUESTER,
    )
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not bool(getattr(identity, "alive", False)):
            return
        time.sleep(0.25)
    if not bool(getattr(identity, "alive", False)):
        return
    record = {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "pid": pid,
        "nonce": str(getattr(identity, "nonce", "")),
        "reason": "heartbeat/configuration recovery exceeded graceful deadline",
    }
    with (runtime_dir / "forced_shutdowns.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    time.sleep(0.5)


def supervise(
    runtime_dir: Path,
    *,
    spec: BackendSpec,
    max_restarts: int | None = None,
) -> int:
    """Own and recover the backend until an intentional stop request."""
    from app.runtime.process_files import ProcessIdentity, SingletonLock, StatusFile, StopRequest

    lease = _supervisor_lock(runtime_dir)
    lease_result = lease.acquire(identity=ProcessIdentity.current(config_fingerprint=spec.fingerprint))
    if not lease_result.acquired:
        _log(runtime_dir, lease_result.reason)
        return 3
    status = StatusFile(runtime_dir)
    stop = StopRequest(runtime_dir)
    # A freshly launched supervisor exists to RUN the backend. Any stop request
    # already on disk is from a previous lifecycle - a prior exit, or a recovery
    # bounce a force-killed backend never consumed - and must not kill the
    # backend we are about to start. A live user stop can only arrive AFTER we
    # are running and is honoured below.
    if stop.pending():
        _log(runtime_dir, "clearing a stale stop request from a previous lifecycle")
        stop.clear()
    restarts = 0
    backoff = BACKOFF_INITIAL_SECONDS
    try:
        if not ensure_backend(runtime_dir, spec=spec, wait_seconds=STARTUP_HANDSHAKE_SECONDS):
            # The spawned backend may still be booting; exiting here would
            # orphan it with no owner to recover a later crash.  Fall into
            # the recovery loop instead of giving up.
            _log(runtime_dir, "initial handshake incomplete; supervisor continues recovering")
        while True:
            time.sleep(2.0)
            if stop.externally_requested():
                _log(runtime_dir, "intentional stop requested; supervisor will not restart backend")
                return 0
            document = status.read()
            state = str(document.get("state", "")) if document else ""
            if state.startswith("STOPPED"):
                _log(runtime_dir, f"backend stopped intentionally ({state}); not restarting")
                return 0
            if status.backend_alive(expected_fingerprint=spec.fingerprint):
                backoff = BACKOFF_INITIAL_SECONDS
                continue
            restarts += 1
            _log(runtime_dir, f"backend unhealthy (restart #{restarts}); recovering in {backoff:.0f}s")
            if max_restarts is not None and restarts > max_restarts:
                _log(runtime_dir, "restart limit reached; giving up")
                return 4
            holder = SingletonLock(runtime_dir).read_identity()
            if holder is not None and holder.alive:
                _recover_backend(runtime_dir, holder)
            time.sleep(backoff)
            backoff = min(BACKOFF_MAX_SECONDS, backoff * 2)
            if stop.externally_requested():
                _log(runtime_dir, "intentional stop requested during recovery; not respawning")
                return 0
            # The recovery bounce above writes an internal stop to unstick a hung
            # backend. A force-killed backend never consumes it, so clear that
            # non-external stop here - otherwise the replacement backend reads it
            # on its first poll and stops immediately (the "starts, then stops 2s
            # later" failure). A user stop was already handled just above.
            if stop.pending():
                stop.clear()
            if not ensure_backend(runtime_dir, spec=spec, wait_seconds=STARTUP_HANDSHAKE_SECONDS):
                _log(runtime_dir, "replacement backend did not complete its health handshake")
    finally:
        lease.release()


def request_stop(runtime_dir: Path, *, wait_seconds: float = 30.0) -> int:
    """Ask backend and supervisor to stop; wait for both leases to release."""
    from app.runtime.process_files import SingletonLock, StatusFile, StopRequest

    StopRequest(runtime_dir).request("stop via backend_supervisor --stop", requester="cli")
    status = StatusFile(runtime_dir)
    backend_lock = SingletonLock(runtime_dir)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        backend = backend_lock.read_identity()
        supervisor = _supervisor_lock(runtime_dir).read_identity()
        backend_alive = backend is not None and backend.alive
        supervisor_alive = supervisor is not None and supervisor.alive
        if not backend_alive and not supervisor_alive:
            document = status.read()
            state = str(document.get("state", "unknown")) if document else "unknown"
            print(f"backend/supervisor stopped ({state})", flush=True)
            return 0
        time.sleep(0.25)
    print("backend/supervisor did not stop within the wait window", file=sys.stderr, flush=True)
    return 1


def _spec_from_args(args: argparse.Namespace) -> BackendSpec:
    return BackendSpec(
        host=args.host,
        port=args.port,
        delayed_data_minutes=args.delayed_data_minutes,
        output_root=str(args.output_root),
        report_root=str(args.report_root),
        session_config=str(args.session_config),
        paper_ledger_path=str(args.paper_ledger_path),
        log_dir=str(args.log_dir),
        processed_root=str(args.processed_root),
        labels_root=str(args.labels_root),
        research_state_root=str(args.research_state_root),
        models_root=str(args.models_root),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI dispatch."""
    parser = argparse.ArgumentParser(description="Persistent MNQ backend supervisor.")
    parser.add_argument("--runtime-dir", type=Path, default=Path("runtime"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--delayed-data-minutes", type=int, default=0)
    parser.add_argument("--output-root", type=Path, default=Path("data/raw"))
    parser.add_argument("--report-root", type=Path, default=Path("data/reports"))
    parser.add_argument("--session-config", type=Path, default=Path("config/session_profiles.yaml"))
    parser.add_argument("--paper-ledger-path", type=Path, default=Path("data/paper/paper_trades.jsonl"))
    parser.add_argument("--log-dir", type=Path, default=Path("logs"))
    parser.add_argument("--processed-root", type=Path, default=Path("data/processed"))
    parser.add_argument("--labels-root", type=Path, default=Path("data/labels"))
    parser.add_argument("--research-state-root", type=Path, default=Path("data/research_state"))
    parser.add_argument("--models-root", type=Path, default=Path("data/models"))
    parser.add_argument("--ensure", action="store_true")
    parser.add_argument("--ensure-supervisor", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    spec = _spec_from_args(args)

    if args.stop:
        return request_stop(args.runtime_dir)
    if args.status:
        from app.runtime.process_files import StatusFile

        status = StatusFile(args.runtime_dir)
        alive = status.backend_alive(expected_fingerprint=spec.fingerprint)
        age = status.staleness_seconds()
        backend = __import__("app.runtime.process_files", fromlist=["SingletonLock"]).SingletonLock(args.runtime_dir).read_identity()
        supervisor = _supervisor_lock(args.runtime_dir).read_identity()
        print(
            f"backend alive: {alive}  pid: {backend.pid if backend else 0}  "
            f"supervisor pid: {supervisor.pid if supervisor else 0}  "
            f"heartbeat age: {'n/a' if age is None else f'{age:.1f}s'}",
        )
        return 0 if alive else 1
    if args.ensure_supervisor:
        return 0 if ensure_supervisor(args.runtime_dir, spec=spec) else 2
    if args.ensure:
        return 0 if ensure_backend(args.runtime_dir, spec=spec) else 2
    return supervise(args.runtime_dir, spec=spec)


if __name__ == "__main__":
    raise SystemExit(main())
