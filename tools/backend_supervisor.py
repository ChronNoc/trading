"""Supervise the capture backend: start detached, monitor, restart, stop.

    .venv\\Scripts\\python.exe -m tools.backend_supervisor            # supervise
    .venv\\Scripts\\python.exe -m tools.backend_supervisor --ensure  # start if absent, then exit
    .venv\\Scripts\\python.exe -m tools.backend_supervisor --stop    # clean shutdown
    .venv\\Scripts\\python.exe -m tools.backend_supervisor --status  # one-line health

The supervisor is the only component that creates or kills backend processes.
The GUI never does either - it only reads ``runtime/status.json`` - so GUI
restarts cannot interrupt capture.

Restart policy: a backend that exits or stops heartbeating is restarted with
bounded exponential backoff (2s -> 60s). A backend stopped by ``--stop`` is NOT
restarted: an intentional stop leaves an intentional silence. Restart count and
reasons are appended to ``runtime/supervisor.log``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

BACKOFF_INITIAL_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 60.0
HEARTBEAT_GRACE_SECONDS = 30.0  # startup time before the first heartbeat is due


def _log(runtime_dir: Path, message: str) -> None:
    line = f"{datetime.now(UTC).isoformat()} {message}\n"
    with (runtime_dir / "supervisor.log").open("a", encoding="utf-8") as handle:
        handle.write(line)
    print(message, flush=True)


def spawn_backend(runtime_dir: Path, *, port: int = 8765,
                  delayed_data_minutes: int = 0) -> subprocess.Popen:
    """Start the backend as a DETACHED process that survives its parent."""
    creation = 0
    if sys.platform == "win32":
        creation = (subprocess.CREATE_NEW_PROCESS_GROUP
                    | getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
    log_path = runtime_dir / "backend.out"
    handle = log_path.open("a", encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, "-m", "tools.start_backend",
         "--runtime-dir", str(runtime_dir), "--port", str(port),
         "--delayed-data-minutes", str(delayed_data_minutes)],
        stdout=handle, stderr=subprocess.STDOUT,
        creationflags=creation,
        cwd=str(Path(__file__).resolve().parent.parent),
    )


def ensure_backend(runtime_dir: Path, *, port: int = 8765, wait_seconds: float = 20.0,
                   delayed_data_minutes: int = 0) -> bool:
    """Start the backend when none is alive; attach when one already is.

    Duplicate batch-file launches land here: an alive backend is left alone.
    Returns whether a live backend exists at return time.
    """
    from app.runtime.process_files import SingletonLock, StatusFile, pid_alive

    runtime_dir.mkdir(parents=True, exist_ok=True)
    status = StatusFile(runtime_dir)
    lock = SingletonLock(runtime_dir)
    holder = lock.holder()
    if holder and pid_alive(holder):
        _log(runtime_dir, f"backend already running (pid {holder}); attaching")
        return True
    spawn_backend(runtime_dir, port=port, delayed_data_minutes=delayed_data_minutes)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if status.backend_alive():
            _log(runtime_dir, f"backend started (pid {lock.holder()})")
            return True
        time.sleep(0.25)
    _log(runtime_dir, "backend did not become healthy within the startup window")
    return False


def supervise(runtime_dir: Path, *, port: int = 8765, max_restarts: int | None = None,
              delayed_data_minutes: int = 0) -> int:
    """Monitor and restart the backend until an intentional stop."""
    from app.runtime.process_files import StatusFile, StopRequest

    status = StatusFile(runtime_dir)
    stop = StopRequest(runtime_dir)
    restarts = 0
    backoff = BACKOFF_INITIAL_SECONDS
    if not ensure_backend(runtime_dir, port=port,
                          delayed_data_minutes=delayed_data_minutes):
        return 2
    grace_until = time.monotonic() + HEARTBEAT_GRACE_SECONDS
    while True:
        time.sleep(2.0)
        if stop.pending():
            _log(runtime_dir, "stop requested; supervisor exiting (backend drains itself)")
            return 0
        document = status.read()
        state = str(document.get("state", "")) if document else ""
        if state.startswith("STOPPED"):
            _log(runtime_dir, f"backend stopped intentionally ({state}); not restarting")
            return 0
        if status.backend_alive() or time.monotonic() < grace_until:
            backoff = BACKOFF_INITIAL_SECONDS  # healthy: reset the backoff
            continue
        restarts += 1
        _log(runtime_dir,
             f"backend unhealthy (restart #{restarts}); restarting in {backoff:.0f}s")
        if max_restarts is not None and restarts > max_restarts:
            _log(runtime_dir, "restart limit reached; giving up")
            return 3
        time.sleep(backoff)
        backoff = min(BACKOFF_MAX_SECONDS, backoff * 2)
        spawn_backend(runtime_dir, port=port, delayed_data_minutes=delayed_data_minutes)
        grace_until = time.monotonic() + HEARTBEAT_GRACE_SECONDS


def request_stop(runtime_dir: Path, *, wait_seconds: float = 30.0) -> int:
    """Ask the backend to drain and stop; wait for it to report STOPPED."""
    from app.runtime.process_files import SingletonLock, StatusFile, StopRequest, pid_alive

    StopRequest(runtime_dir).request("stop via backend_supervisor --stop")
    status = StatusFile(runtime_dir)
    lock = SingletonLock(runtime_dir)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        holder = lock.holder()
        if not holder or not pid_alive(holder):
            document = status.read()
            state = str(document.get("state", "unknown")) if document else "unknown"
            print(f"backend stopped ({state})", flush=True)
            return 0
        time.sleep(0.25)
    print("backend did not stop within the wait window", file=sys.stderr, flush=True)
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """CLI dispatch."""
    parser = argparse.ArgumentParser(description="Backend supervisor.")
    parser.add_argument("--runtime-dir", type=Path, default=Path("runtime"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--ensure", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    if args.stop:
        return request_stop(args.runtime_dir)
    if args.status:
        from app.runtime.process_files import SingletonLock, StatusFile

        status = StatusFile(args.runtime_dir)
        alive = status.backend_alive()
        age = status.staleness_seconds()
        print(f"backend alive: {alive}  pid: {SingletonLock(args.runtime_dir).holder()}  "
              f"heartbeat age: {'n/a' if age is None else f'{age:.1f}s'}")
        return 0 if alive else 1
    if args.ensure:
        return 0 if ensure_backend(args.runtime_dir, port=args.port) else 2
    return supervise(args.runtime_dir, port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
