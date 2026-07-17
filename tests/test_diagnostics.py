"""Diagnostics: a crash, a dead thread, or a hang must leave evidence.

The app was reported as "extremely slow, non-responsive, crashes a lot" while the
repository had no faulthandler, no excepthook, and no log file — so every failure
left nothing behind and every diagnosis was a guess.
"""

from __future__ import annotations

import logging
import logging.handlers
import threading
import time
from pathlib import Path

from app.runtime.diagnostics import (
    StallWatchdog,
    dump_all_thread_stacks,
    install_asyncio_handler,
    install_diagnostics,
)


def test_install_creates_rotating_log_and_crash_file(tmp_path: Path) -> None:
    logger = install_diagnostics(tmp_path)
    logger.info("hello")
    for handler in logger.handlers:
        handler.flush()
    assert (tmp_path / "assistant.log").is_file()
    assert (tmp_path / "crash.log").is_file(), "faulthandler needs a pre-opened file"
    assert "hello" in (tmp_path / "assistant.log").read_text(encoding="utf-8")


def test_install_is_idempotent_and_does_not_stack_handlers(tmp_path: Path) -> None:
    """Calling twice for the same directory must not double every log line."""
    first = install_diagnostics(tmp_path)
    count = len(first.handlers)
    second = install_diagnostics(tmp_path)
    assert second is first
    assert len(second.handlers) == count


def test_reinstalling_elsewhere_actually_moves_the_log(tmp_path: Path) -> None:
    """Idempotent must not mean 'silently ignore the new destination'."""
    first_dir, second_dir = tmp_path / "one", tmp_path / "two"
    install_diagnostics(first_dir)
    logger = install_diagnostics(second_dir)
    logger.info("second home")
    for handler in logger.handlers:
        handler.flush()
    assert "second home" in (second_dir / "assistant.log").read_text(encoding="utf-8")
    assert len([h for h in logger.handlers
                if isinstance(h, logging.handlers.RotatingFileHandler)]) == 1


def test_a_dying_worker_thread_is_logged(tmp_path: Path) -> None:
    """A silent thread death is why 'the feed just stopped' was undiagnosable."""
    logger = install_diagnostics(tmp_path)

    def explode() -> None:
        raise RuntimeError("receiver thread died")

    thread = threading.Thread(target=explode, name="doomed-receiver")
    thread.start()
    thread.join(timeout=5)
    for handler in logger.handlers:
        handler.flush()
    log = (tmp_path / "assistant.log").read_text(encoding="utf-8")
    assert "receiver thread died" in log
    assert "doomed-receiver" in log
    assert "this thread is now dead" in log


def test_dump_all_thread_stacks_includes_live_threads() -> None:
    """The only way to see a hang is every thread's stack."""
    started, release = threading.Event(), threading.Event()

    def park() -> None:
        started.set()
        release.wait(timeout=10)

    thread = threading.Thread(target=park, name="parked-thread")
    thread.start()
    try:
        assert started.wait(timeout=5)
        dump = dump_all_thread_stacks()
        assert "parked-thread" in dump
        assert "park" in dump, "the stack must show where the thread actually is"
    finally:
        release.set()
        thread.join(timeout=5)


# --- stall detection -------------------------------------------------------------


def test_watchdog_is_quiet_while_the_heartbeat_arrives() -> None:
    clock = {"t": 100.0}
    dog = StallWatchdog(stall_seconds=10.0, clock=lambda: clock["t"])
    clock["t"] = 105.0
    dog.heartbeat()
    clock["t"] = 112.0
    assert dog.is_stalled() is False
    assert dog.check() is False
    assert dog.stalls_detected == 0


def test_watchdog_reports_a_stall_with_thread_stacks(tmp_path: Path) -> None:
    """A frozen UI crashes nothing, so the watchdog is the only evidence."""
    logger = install_diagnostics(tmp_path)
    clock = {"t": 100.0}
    captured: list[str] = []
    dog = StallWatchdog(stall_seconds=10.0, logger=logger,
                        on_stall=captured.append, clock=lambda: clock["t"])
    dog.heartbeat()
    clock["t"] = 121.0  # 21s with no heartbeat
    assert dog.is_stalled() is True
    assert dog.check() is True
    assert dog.stalls_detected == 1
    assert captured and "thread" in captured[0]
    for handler in logger.handlers:
        handler.flush()
    log = (tmp_path / "assistant.log").read_text(encoding="utf-8")
    assert "main loop stalled for 21.0s" in log


def test_a_single_stall_is_reported_once_not_every_poll() -> None:
    """Spamming a stack dump every 2s would bury the log and worsen the stall."""
    clock = {"t": 100.0}
    dog = StallWatchdog(stall_seconds=5.0, clock=lambda: clock["t"])
    dog.heartbeat()
    clock["t"] = 110.0
    assert dog.check() is True
    clock["t"] = 112.0
    assert dog.check() is False, "the same stall episode must not report twice"
    assert dog.stalls_detected == 1


def test_recovery_then_a_new_stall_reports_again() -> None:
    clock = {"t": 100.0}
    dog = StallWatchdog(stall_seconds=5.0, clock=lambda: clock["t"])
    dog.heartbeat()
    clock["t"] = 110.0
    assert dog.check() is True
    clock["t"] = 111.0
    dog.heartbeat()  # recovered
    clock["t"] = 130.0
    assert dog.check() is True, "a NEW stall must be reported"
    assert dog.stalls_detected == 2


def test_watchdog_thread_starts_and_stops_cleanly() -> None:
    dog = StallWatchdog(stall_seconds=60.0)
    dog.start(poll_seconds=0.05)
    dog.start(poll_seconds=0.05)  # idempotent
    time.sleep(0.15)
    dog.heartbeat()
    dog.stop()
    assert dog.stalls_detected == 0


def test_asyncio_handler_logs_unhandled_errors(tmp_path: Path) -> None:
    import asyncio

    logger = install_diagnostics(tmp_path)
    loop = asyncio.new_event_loop()
    try:
        install_asyncio_handler(loop, logger)
        loop.call_exception_handler({"message": "boom", "exception": ValueError("bad")})
    finally:
        loop.close()
    for handler in logger.handlers:
        handler.flush()
    log = (tmp_path / "assistant.log").read_text(encoding="utf-8")
    assert "boom" in log and "ValueError" in log


def test_watchdog_never_raises_out_of_its_own_loop() -> None:
    """A watchdog that can crash the app is worse than no watchdog."""

    def bad_sink(_stacks: str) -> None:
        raise RuntimeError("sink exploded")

    clock = {"t": 100.0}
    dog = StallWatchdog(stall_seconds=1.0, on_stall=bad_sink, clock=lambda: clock["t"])
    dog.heartbeat()
    clock["t"] = 200.0
    dog.start(poll_seconds=0.05)
    time.sleep(0.2)
    dog.stop()  # must not have taken the process down


# --- the production wiring must stay in place -----------------------------------


def test_launcher_installs_diagnostics_before_anything_can_fail() -> None:
    """A crash with no log is why the 'crashes a lot' report was undiagnosable."""
    source = Path("tools/start_assistant.py").read_text(encoding="utf-8")
    assert "install_diagnostics(config.log_dir)" in source
    assert "install_asyncio_handler(asyncio.get_running_loop())" in source


def test_gui_pumps_a_stall_heartbeat_and_watches_for_freezes() -> None:
    """The GIL-starvation freeze produced no evidence at all; it must now."""
    source = Path("tools/start_assistant.py").read_text(encoding="utf-8")
    assert "StallWatchdog(stall_seconds=GUI_STALL_SECONDS)" in source
    assert "heartbeat_timer.timeout.connect(watchdog.heartbeat)" in source
    assert "watchdog.start()" in source
    assert "watchdog.stop()" in source


def test_logs_directory_is_gitignored() -> None:
    """Logs are runtime noise and must never be committed."""
    assert "logs/" in Path(".gitignore").read_text(encoding="utf-8")
