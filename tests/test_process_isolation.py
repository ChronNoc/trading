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
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

from app.gui.snapshot_codec import decode_snapshot, encode_snapshot
from app.runtime.process_files import SingletonLock, StatusFile, StopRequest, pid_alive

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


# --- runtime files ----------------------------------------------------------------


def test_singleton_lock_refuses_a_second_live_backend(tmp_path: Path) -> None:
    first = SingletonLock(tmp_path)
    assert first.acquire().acquired is True
    second = SingletonLock(tmp_path)
    result = second.acquire()  # same live PID counts as "already running"? No -
    # same pid is allowed (re-entrant); simulate ANOTHER live process instead:
    assert result.acquired is True  # same-pid re-acquire is not a duplicate
    first.release()

    # A DIFFERENT live pid must be refused. Use our parent process when alive.
    other = os.getppid()
    if other and pid_alive(other):
        tmp2 = tmp_path / "b"
        lock = SingletonLock(tmp2)
        lock.path.parent.mkdir(parents=True, exist_ok=True)
        lock.path.write_text(str(other), encoding="utf-8")
        refused = SingletonLock(tmp2).acquire()
        assert refused.acquired is False
        assert f"pid {other}" in refused.reason


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
    stop.request("test")
    assert stop.pending() is True
    stop.clear()
    assert stop.pending() is False


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
                await ws.send(json.dumps({"type": "connected", "timestamp_ns": 1,
                                          "alias": "MNQU6", "addon_version": "0.1.0"}))
                base = 1_752_537_751_000_000_000
                for i in range(start, start + count):
                    await ws.send(json.dumps({
                        "type": "depth_update", "timestamp": base + i * 500_000_000,
                        "symbol": "MNQ", "side": "bid" if i % 2 else "ask",
                        "price": f"{29500 + (i % 40) * 0.25:.2f}",
                        "previous_size": "0", "new_size": str(i % 30 + 1)}))
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
