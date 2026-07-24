"""Process isolation: GUI restart is structurally incapable of touching capture.

The backend is its own OS process; the GUI only reads ``runtime/status.json``.
These tests cover the codec, the runtime files, and — the decisive one — a REAL
backend subprocess capturing real WebSocket events while the GUI-side provider
is created, read, destroyed, and recreated, with zero effect on capture.

Everything runs in temp directories on ephemeral ports.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

from app.gui.snapshot_codec import decode_snapshot, encode_snapshot
from app.runtime.process_files import (
    ProcessIdentity,
    SingletonLock,
    StatusFile,
    StopRequest,
    configuration_fingerprint,
)

REPO = Path(__file__).resolve().parent.parent


# --- codec: the snapshot must cross the process boundary losslessly ---------------


def _rich_snapshot():  # noqa: ANN202
    from tests.test_app_window import _rich_snapshot as build

    return build()


def test_codec_round_trips_the_full_snapshot_tree() -> None:
    original = _rich_snapshot()
    decoded = decode_snapshot(encode_snapshot(original))
    assert decoded == original, "the GUI must see EXACTLY what the backend published"


def test_codec_preserves_decimal_and_enum_types() -> None:
    decoded = decode_snapshot(encode_snapshot(_rich_snapshot()))
    assert isinstance(decoded.market.last_price, Decimal)
    assert decoded.market.last_price == Decimal("29268.50")
    assert isinstance(decoded.paper.balance, Decimal)
    from app.gui.view_models import Capability, Health

    assert isinstance(decoded.components[0].health, Health)
    assert isinstance(decoded.capabilities[0][1], Capability)


def test_codec_rejects_a_different_schema_version() -> None:
    payload = json.loads(encode_snapshot(_rich_snapshot()))
    payload["schema_version"] = 999
    try:
        decode_snapshot(json.dumps(payload))
    except ValueError as error:
        assert "schema" in str(error)
    else:  # pragma: no cover
        raise AssertionError("an unknown schema must be refused, not misparsed")


def test_codec_rejects_missing_and_unknown_snapshot_fields() -> None:
    payload = json.loads(encode_snapshot(_rich_snapshot()))
    del payload["snapshot"]["market"]["contract"]
    payload["snapshot"]["market"]["invented"] = "not real"
    with pytest.raises(ValueError, match="fields mismatch"):
        decode_snapshot(json.dumps(payload))


def test_codec_rejects_float_money_values() -> None:
    payload = json.loads(encode_snapshot(_rich_snapshot()))
    payload["snapshot"]["paper"]["balance"] = 25000.0
    with pytest.raises(ValueError, match="Decimal fields"):
        decode_snapshot(json.dumps(payload))


def test_codec_rejects_truncated_fixed_tuples() -> None:
    payload = json.loads(encode_snapshot(_rich_snapshot()))
    payload["snapshot"]["capabilities"][0] = ["Aggregated depth"]
    with pytest.raises(ValueError, match="fixed tuple length"):
        decode_snapshot(json.dumps(payload))


# --- runtime files ----------------------------------------------------------------


def test_singleton_lock_refuses_a_second_live_backend(tmp_path: Path) -> None:
    first = SingletonLock(tmp_path)
    assert first.acquire().acquired is True
    second = SingletonLock(tmp_path)
    result = second.acquire()
    assert result.acquired is False, "a separate lock object must never bypass the OS lease"
    assert result.holder_pid == os.getpid()
    first.release()


def test_singleton_lock_cleans_a_stale_dead_pid(tmp_path: Path) -> None:
    """A crashed backend's lock is the forced-shutdown marker; report and clean."""
    lock = SingletonLock(tmp_path)
    lock.path.write_text("999999999", encoding="utf-8")  # not a real pid
    result = SingletonLock(tmp_path).acquire()
    assert result.acquired is True
    assert result.stale_lock_cleaned is True
    assert "did not exit cleanly" in result.reason


def test_status_file_heartbeat_and_staleness(tmp_path: Path) -> None:
    status = StatusFile(tmp_path)
    assert status.read() is None
    assert status.backend_alive() is False
    status.write(encode_snapshot(_rich_snapshot()))
    document = status.read()
    assert document is not None
    assert int(document["backend_pid"]) == os.getpid()
    age = status.staleness_seconds(document)
    assert age is not None and age < 2.0
    assert status.backend_alive() is True  # our own pid, fresh beat


def test_stop_request_round_trip(tmp_path: Path) -> None:
    stop = StopRequest(tmp_path)
    assert stop.pending() is False
    request_id = stop.request("test", requester="pytest")
    assert stop.pending() is True
    payload = stop.read()
    assert payload is not None
    assert payload["request_id"] == request_id
    assert payload["requester"] == "pytest"
    assert payload["reason"] == "test"
    assert float(payload["requested_at_unix"]) > 0
    stop.clear()
    assert stop.pending() is False


def test_status_rejects_an_incompatible_backend_configuration(tmp_path: Path) -> None:
    status = StatusFile(tmp_path)
    expected = configuration_fingerprint({"port": 8765, "mode": "paper"})
    other = configuration_fingerprint({"port": 9999, "mode": "paper"})
    identity = ProcessIdentity.current(config_fingerprint=expected)
    status.write(encode_snapshot(_rich_snapshot()), identity=identity)
    assert status.backend_alive(expected_fingerprint=expected) is True
    assert status.backend_alive(expected_fingerprint=other) is False


# --- the GUI-side provider is honest about a dead backend -------------------------


def test_provider_reports_backend_down_when_no_status_exists(tmp_path: Path) -> None:
    from app.gui.file_snapshot import FileSnapshotProvider

    snapshot = FileSnapshotProvider(tmp_path)()
    assert snapshot.lifecycle_state == "BACKEND_DOWN"
    assert "not running" in snapshot.blocker
    assert snapshot.components[0].name == "backend"


def test_provider_reports_a_stale_heartbeat_never_a_false_ok(tmp_path: Path) -> None:
    """A hung/crashed backend must not leave the GUI displaying OK."""
    from app.gui.file_snapshot import FileSnapshotProvider

    status = StatusFile(tmp_path)
    status.write(encode_snapshot(_rich_snapshot()))
    # Rewrite the document with an OLD heartbeat (simulate a hang).
    document = status.read()
    document["heartbeat_unix"] = time.time() - 60.0
    status.path.write_text(json.dumps(document), encoding="utf-8")
    snapshot = FileSnapshotProvider(tmp_path)()
    assert snapshot.lifecycle_state == "BACKEND_DOWN"
    assert "stale" in snapshot.blocker
    # The last real numbers stay visible - only the health is overridden.
    assert snapshot.market.contract == "MNQU6"


def test_provider_reads_a_fresh_backend_snapshot_verbatim(tmp_path: Path) -> None:
    from app.gui.file_snapshot import FileSnapshotProvider

    StatusFile(tmp_path).write(encode_snapshot(_rich_snapshot()))
    snapshot = FileSnapshotProvider(tmp_path)()
    assert snapshot == _rich_snapshot()


# --- THE integration proof: real backend process, real events, GUI restarts ------


def test_gui_restart_cannot_interrupt_the_real_backend(tmp_path: Path) -> None:
    """Spawn the actual backend process; capture must be untouched by GUI churn."""
    import asyncio

    import websockets

    from app.gui.file_snapshot import FileSnapshotProvider

    runtime = tmp_path / "runtime"
    raw = tmp_path / "raw"
    process = subprocess.Popen(
        [sys.executable, "-m", "tools.start_backend",
         "--runtime-dir", str(runtime), "--port", "0",
         "--output-root", str(raw), "--max-seconds", "120"],
        cwd=str(REPO), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        status = StatusFile(runtime)
        deadline = time.monotonic() + 30
        port = 0
        while time.monotonic() < deadline:
            document = status.read()
            if document is not None:
                try:
                    snapshot = decode_snapshot(str(document["snapshot"]))
                except Exception:  # noqa: BLE001 - racing the first write
                    time.sleep(0.2)
                    continue
                if snapshot.capture.receiver_listening:
                    binding = json.loads((runtime / "binding.json").read_text(encoding="utf-8")) \
                        if (runtime / "binding.json").is_file() else {}
                    port = int(binding.get("port", 0))
                    break
            time.sleep(0.2)
        assert port, "backend must publish its bound port"

        async def send(count: int, start: int) -> None:
            uri = f"ws://127.0.0.1:{port}/bookmap"
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({
                    "type": "connected", "timestamp_ns": 1,
                    "alias": "MNQU6", "symbol": "MNQ", "addon_version": "0.1.0",
                    "protocol_version": "1.2", "stream_id": "pytest-stream",
                    "connection_id": f"pytest-connection-{start}",
                    "session_id": f"pytest-session-{start}",
                    "provider": "pytest", "capabilities": "aggregated_depth",
                    "stream_sequence": 1,
                }))
                base = 1_752_537_751_000_000_000
                for i in range(start, start + count):
                    await ws.send(json.dumps({
                        "type": "depth_update", "timestamp": base + i * 500_000_000,
                        "symbol": "MNQ", "side": "bid" if i % 2 else "ask",
                        "price": f"{29500 + (i % 40) * 0.25:.2f}",
                        "previous_size": "0", "new_size": str(i % 30 + 1),
                        "stream_sequence": i - start + 2,
                    }))
                await asyncio.sleep(0.3)

        # 1. GUI attaches and sees events flowing.
        asyncio.run(send(300, 0))
        gui_one = FileSnapshotProvider(runtime)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if gui_one().paper.evaluations >= 0 and gui_one().capture.analysis_offered >= 300:
                break
            time.sleep(0.2)
        seen_before = gui_one().capture.analysis_offered
        assert seen_before >= 300, "the GUI must see the backend's real counters"

        # 2. "GUI restart": drop the provider entirely, send MORE events with no
        #    GUI attached, then attach a brand-new provider.
        del gui_one
        asyncio.run(send(300, 300))
        gui_two = FileSnapshotProvider(runtime)
        deadline = time.monotonic() + 15
        seen_after = 0
        while time.monotonic() < deadline:
            seen_after = gui_two().capture.analysis_offered
            if seen_after >= 600:
                break
            time.sleep(0.2)
        assert seen_after >= 600, (
            "capture must continue across a GUI restart - events sent while no "
            f"GUI existed must be there ({seen_after} < 600)"
        )
        assert gui_two().lifecycle_state != "BACKEND_DOWN"

        # 3. A duplicate backend launch is refused while this one is alive.
        duplicate = subprocess.run(
            [sys.executable, "-m", "tools.start_backend",
             "--runtime-dir", str(runtime), "--port", "0", "--max-seconds", "5"],
            cwd=str(REPO), capture_output=True, text=True, timeout=30,
        )
        assert duplicate.returncode == 3
        assert "already running" in duplicate.stderr

        # 4. Clean stop via the stop file: backend drains and reports STOPPED.
        StopRequest(runtime).request("test stop")
        process.wait(timeout=40)
        final = status.read()
        assert final is not None
        assert str(final["state"]).startswith("STOPPED")
        assert SingletonLock(runtime).holder() == 0, "the lock must be released"
        # The recording it made while GUIs came and went is real and finalized.
        manifests = list(raw.rglob("session_manifest.json"))
        assert manifests, "the backend must have recorded a session"
    finally:
        if process.poll() is None:
            process.kill()


def _tail(path: Path, lines: int = 25) -> str:
    if not path.is_file():
        return f"<{path.name} missing>"
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def test_supervisor_restarts_a_crashed_backend_and_then_stops_cleanly(tmp_path: Path) -> None:
    """A healthy backend crash must never hide inside a startup grace window.

    Every phase synchronizes on the runtime files (lock identity + status
    heartbeat) under a generous deadline rather than assuming how long a real
    subprocess takes on a CPU-contended machine.  What it verifies is
    unchanged: the supervisor replaces a crashed backend, and an intentional
    stop drains everything cleanly.
    """
    runtime = tmp_path / "runtime"
    command = [
        sys.executable, "-m", "tools.backend_supervisor",
        "--runtime-dir", str(runtime),
        "--port", "0",
        "--output-root", str(tmp_path / "raw"),
        "--report-root", str(tmp_path / "reports"),
        "--paper-ledger-path", str(tmp_path / "paper" / "ledger.jsonl"),
        "--log-dir", str(tmp_path / "logs"),
        "--processed-root", str(tmp_path / "processed"),
        "--labels-root", str(tmp_path / "labels"),
        "--research-state-root", str(tmp_path / "research"),
    ]
    # An unread PIPE can fill and block the child; a file never can, and it
    # doubles as the post-mortem transcript on failure.
    console_path = tmp_path / "supervisor_console.log"
    with console_path.open("wb") as console:
        supervisor = subprocess.Popen(
            command, cwd=str(REPO), stdout=console, stderr=subprocess.STDOUT,
        )
    backend_lock = SingletonLock(runtime)
    status = StatusFile(runtime)

    def diagnostics() -> str:
        return (
            f"supervisor returncode={supervisor.poll()}\n"
            f"--- supervisor console ---\n{_tail(console_path)}\n"
            f"--- supervisor.log ---\n{_tail(runtime / 'supervisor.log')}\n"
            f"--- backend.out ---\n{_tail(runtime / 'backend.out')}"
        )

    def wait_for(condition, deadline_seconds: float, description: str):  # noqa: ANN001, ANN202
        """Poll until truthy; fail fast (with logs) if the supervisor dies."""
        deadline = time.monotonic() + deadline_seconds
        while time.monotonic() < deadline and supervisor.poll() is None:
            value = condition()
            if value:
                return value
            time.sleep(0.25)
        value = condition()  # one last look after deadline/supervisor exit
        if value:
            return value
        raise AssertionError(f"{description}\n{diagnostics()}")

    try:
        def healthy_identity():  # noqa: ANN202
            candidate = backend_lock.read_identity()
            if candidate is not None and status.backend_alive():
                return candidate
            return None

        first = wait_for(healthy_identity, 120.0, "supervisor must establish a healthy backend")

        os.kill(first.pid, signal.SIGTERM)

        def replacement_identity():  # noqa: ANN202
            candidate = backend_lock.read_identity()
            if candidate is not None and candidate.pid != first.pid and status.backend_alive():
                return candidate
            return None

        replacement = wait_for(
            replacement_identity, 180.0, "supervisor must replace the crashed backend",
        )
        assert replacement.pid != first.pid

        StopRequest(runtime).request("supervisor regression complete", requester="pytest")
        supervisor.wait(timeout=120)
        assert supervisor.returncode == 0, diagnostics()

        # The backend drains detached from the supervisor, so the supervisor
        # exiting first is normal: poll for the backend's own completion.
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            document = status.read()
            stopped = document is not None and str(document.get("state", "")).startswith("STOPPED")
            if stopped and backend_lock.read_identity() is None:
                break
            time.sleep(0.25)
        final = status.read()
        assert final is not None
        assert str(final["state"]).startswith("STOPPED"), diagnostics()
        assert backend_lock.read_identity() is None, diagnostics()
    finally:
        if supervisor.poll() is None:
            StopRequest(runtime).request("pytest cleanup", requester="pytest")
            try:
                supervisor.wait(timeout=30)
            except subprocess.TimeoutExpired:
                supervisor.kill()
                supervisor.wait(timeout=10)
        # Never leak a detached backend past the test, even on failure.
        leftover = backend_lock.read_identity()
        if leftover is not None and leftover.alive:
            StopRequest(runtime).request("pytest cleanup", requester="pytest")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and leftover.alive:
                time.sleep(0.25)
            if leftover.alive:
                try:
                    os.kill(leftover.pid, signal.SIGTERM)
                except OSError:
                    pass
