"""Clean shutdown: capture must drain, never be killed mid-write.

The defect these guard: the receiver ran as a ``daemon=True`` thread, so closing
the window let the process exit kill it at an arbitrary point. In-flight recorder
batches were lost, the session was never finalized (so the automatic episode
build never ran), and an open simulated position was abandoned.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app.runtime.shutdown import ShutdownSignal


def _receiver_loop(signal: ShutdownSignal, drained: list[str], *, fail: bool = False) -> None:
    """A miniature of the real receiver: await the stop future, then drain."""

    async def main() -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        signal.bind(loop, future)
        error = ""
        try:
            await future
        finally:
            try:
                if fail:
                    raise RuntimeError("recorder flush failed")
                drained.append("flushed")
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
            finally:
                signal.mark_drained(error)

    asyncio.run(main())


def test_stop_request_wakes_the_receiver_and_it_drains() -> None:
    signal = ShutdownSignal()
    drained: list[str] = []
    thread = threading.Thread(target=_receiver_loop, args=(signal, drained))
    thread.start()
    # Let the loop reach its await before requesting the stop.
    for _ in range(200):
        if signal._future is not None:  # noqa: SLF001 - waiting for the bind
            break
        time.sleep(0.01)
    assert signal.request_stop() is None
    assert signal.wait_for_drain(timeout=5) is True
    thread.join(timeout=5)
    assert drained == ["flushed"], "the receiver's flush path must actually run"


def test_a_stop_requested_before_bind_is_not_lost() -> None:
    """A very fast window close must still stop the receiver."""
    signal = ShutdownSignal()
    signal.request_stop()  # before the loop even exists
    drained: list[str] = []
    thread = threading.Thread(target=_receiver_loop, args=(signal, drained))
    thread.start()
    assert signal.wait_for_drain(timeout=5) is True
    thread.join(timeout=5)
    assert drained == ["flushed"], "an early stop must be honoured on bind, not dropped"


def test_a_failed_drain_is_reported_not_swallowed() -> None:
    """Losing the recording tail must be visible, never silent."""
    signal = ShutdownSignal()
    drained: list[str] = []
    thread = threading.Thread(target=_receiver_loop, args=(signal, drained), kwargs={"fail": True})
    thread.start()
    for _ in range(200):
        if signal._future is not None:  # noqa: SLF001
            break
        time.sleep(0.01)
    signal.request_stop()
    assert signal.wait_for_drain(timeout=5) is False, "a failed flush must not report success"
    assert "recorder flush failed" in signal.drain_error
    thread.join(timeout=5)


def test_wait_for_drain_is_bounded_and_never_hangs() -> None:
    """A shutdown that can hang forever is not a shutdown."""
    signal = ShutdownSignal()
    signal.request_stop()
    started = time.monotonic()
    assert signal.wait_for_drain(timeout=0.2) is False
    assert time.monotonic() - started < 2.0, "the wait must return promptly"


def test_request_stop_is_idempotent() -> None:
    signal = ShutdownSignal()
    signal.request_stop()
    signal.request_stop()
    assert signal.stop_requested is True


def test_stopping_an_already_closed_loop_does_not_raise() -> None:
    """A receiver that already exited must not break the GUI's shutdown path."""
    signal = ShutdownSignal()
    loop = asyncio.new_event_loop()
    future = loop.create_future()
    signal.bind(loop, future)
    loop.close()
    signal.request_stop()  # must not raise
    assert signal.wait_for_drain(timeout=1) is True


# --- the production wiring must stay in place -----------------------------------


def test_launcher_drains_capture_before_exiting() -> None:
    """Regression guard: the GUI must not exit while capture is mid-write."""
    from pathlib import Path

    source = Path("tools/start_assistant.py").read_text(encoding="utf-8")
    assert "_shutdown_receiver(shutdown, receiver_thread)" in source, (
        "the launcher must drain capture when the window closes"
    )
    assert "shutdown.request_stop()" in source
    assert "shutdown.wait_for_drain(timeout)" in source
    assert "await stop_future" in source, "the receiver must await a stoppable future"
    assert "unclean shutdown" in source, "an incomplete drain must be reported"


def test_shutdown_flattens_an_open_simulated_position() -> None:
    """An abandoned open position would leave the ledger incoherent."""
    from pathlib import Path

    source = Path("tools/start_assistant.py").read_text(encoding="utf-8")
    assert "paper_engine.flatten()" in source


# --- integration: the REAL receiver, not a miniature -----------------------------


def test_the_real_receiver_binds_and_then_drains_cleanly(tmp_path) -> None:
    """End-to-end: start the actual receiver thread, close it the way the GUI does.

    Uses an ephemeral port and a temporary output root, so no real recording,
    port, or user directory is touched.
    """
    from app.runtime.controller import AutomaticRuntimeController
    from app.runtime.server_state import ReceiverStatusHolder
    from tools.start_assistant import AssistantConfig, _run_receiver_thread, _shutdown_receiver

    config = AssistantConfig(port=0, output_root=tmp_path / "raw",
                             report_root=tmp_path / "reports", gui=False)
    controller = AutomaticRuntimeController.from_config(
        config.session_config, report_root=config.report_root,
    )
    status = ReceiverStatusHolder()
    signal = ShutdownSignal()
    thread = threading.Thread(
        target=_run_receiver_thread,
        args=(config, controller, status, None, None, None, signal),
        name="test-receiver", daemon=True,
    )
    thread.start()
    for _ in range(500):  # wait for a real socket bind
        if status.snapshot().listening:
            break
        time.sleep(0.02)
    assert status.snapshot().listening, "the receiver must actually bind before we stop it"

    assert _shutdown_receiver(signal, thread, timeout=10.0) is True, signal.drain_error
    assert not thread.is_alive(), "the receiver thread must exit, not be killed at process exit"
    assert signal.drain_error == ""
