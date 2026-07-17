"""Crash, hang, and error diagnostics — so a failure leaves evidence.

The app was reported as "extremely slow, non-responsive, crashes a lot" and the
repository had no faulthandler, no excepthook, and no log file. A crash left
nothing behind, so every diagnosis had to be a guess. That is what this fixes.

Four separate failure modes, each of which was previously silent:

* **Hard crash** (segfault in Qt/pyarrow, C-level abort) — Python's normal
  traceback machinery never runs. ``faulthandler`` writes the native stack to a
  file that survives the process.
* **Uncaught exception on the main thread** — printed to a stderr nobody reads
  when launched from a shortcut.
* **Uncaught exception on a worker thread** — ``threading.excepthook`` default
  prints and the thread dies silently. The receiver dying this way would look
  exactly like "the feed just stopped".
* **A hang** — nothing crashes, so nothing is logged, and the UI simply freezes.
  ``StallWatchdog`` notices the missing heartbeat and dumps EVERY thread's stack,
  which is what actually identifies a GIL-starvation or deadlock.

Logs rotate, so a long soak cannot fill the disk.
"""

from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Callable

LOG_FORMAT = "%(asctime)s %(levelname)-8s [%(threadName)s] %(name)s: %(message)s"
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5
DEFAULT_STALL_SECONDS = 10.0

_crash_file: Any = None  # kept open for the process lifetime by design


def install_diagnostics(
    log_dir: Path | str = Path("logs"),
    *,
    level: int = logging.INFO,
) -> logging.Logger:
    """Install crash/exception logging. Safe to call more than once.

    Returns the application logger. The crash file handle is deliberately kept
    open for the whole process: faulthandler writes to it from a signal handler,
    where opening a file is not possible.
    """
    global _crash_file  # noqa: PLW0603 - the handle must outlive this call

    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("mnq")
    logger.setLevel(level)
    log_path = directory / "assistant.log"

    # Idempotent means "do not stack duplicate handlers for the same file" - NOT
    # "ignore a new destination forever". Re-pointing at a different directory
    # must actually move the log, or a later caller writes into the void.
    existing = [h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    already_here = any(Path(h.baseFilename) == log_path.resolve() for h in existing)
    if not already_here:
        for handler in existing:
            logger.removeHandler(handler)
            handler.close()
        handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=MAX_LOG_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        logger.addHandler(handler)

    crash_path = directory / "crash.log"
    if _crash_file is None or Path(getattr(_crash_file, "name", "")) != crash_path.resolve():
        if _crash_file is not None:
            faulthandler.disable()
            _crash_file.close()
        _crash_file = crash_path.resolve().open("a", encoding="utf-8")
        faulthandler.enable(file=_crash_file, all_threads=True)

    _install_excepthooks(logger)
    return logger


def _install_excepthooks(logger: logging.Logger) -> None:
    """Route uncaught exceptions - main thread AND workers - into the log."""
    previous_hook = sys.excepthook

    def _main_hook(kind: type[BaseException], value: BaseException, tb: Any) -> None:
        if issubclass(kind, KeyboardInterrupt):  # a deliberate Ctrl+C is not a crash
            previous_hook(kind, value, tb)
            return
        logger.critical("uncaught exception on the main thread",
                        exc_info=(kind, value, tb))
        previous_hook(kind, value, tb)

    def _thread_hook(args: Any) -> None:
        if issubclass(args.exc_type, SystemExit):
            return
        # A worker dying silently is why "the feed just stopped" was undiagnosable.
        logger.critical("uncaught exception on thread %r - this thread is now dead",
                        getattr(args.thread, "name", "?"),
                        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = _main_hook
    threading.excepthook = _thread_hook


def install_asyncio_handler(loop: Any, logger: logging.Logger | None = None) -> None:
    """Log unhandled asyncio exceptions instead of letting them vanish."""
    log = logger or logging.getLogger("mnq")

    def _handler(_loop: Any, context: dict[str, Any]) -> None:
        error = context.get("exception")
        message = context.get("message", "unhandled asyncio error")
        if error is not None:
            log.error("asyncio: %s", message, exc_info=error)
        else:
            log.error("asyncio: %s (%r)", message, context)

    loop.set_exception_handler(_handler)


def dump_all_thread_stacks() -> str:
    """Return every live thread's stack. The only way to see a hang."""
    frames = sys._current_frames()  # noqa: SLF001 - the documented way to do this
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    blocks: list[str] = []
    for ident, frame in frames.items():
        stack = "".join(traceback.format_stack(frame))
        blocks.append(f"--- thread {names.get(ident, '?')} ({ident}) ---\n{stack}")
    return "\n".join(blocks)


class StallWatchdog:
    """Detect a frozen main loop and dump every thread's stack.

    A hang crashes nothing, so nothing is logged and the window simply stops
    repainting. The heartbeat is pumped by the thread being watched; if it stops
    arriving, that thread is stuck and the stacks say exactly where.

    The watchdog reports and never kills: a false positive must not take down a
    healthy app that was merely busy.
    """

    def __init__(
        self,
        *,
        stall_seconds: float = DEFAULT_STALL_SECONDS,
        logger: logging.Logger | None = None,
        on_stall: Callable[[str], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Create a watchdog. Nothing runs until ``start`` is called."""
        import time

        self._stall_seconds = stall_seconds
        self._logger = logger or logging.getLogger("mnq")
        self._on_stall = on_stall
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._last_beat = self._clock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stalls = 0
        self._reported = False

    def heartbeat(self) -> None:
        """Pump from the watched thread. Cheap enough for a GUI timer."""
        with self._lock:
            self._last_beat = self._clock()
            self._reported = False

    @property
    def stalls_detected(self) -> int:
        """How many distinct stalls have been reported."""
        with self._lock:
            return self._stalls

    def is_stalled(self, *, now: float | None = None) -> bool:
        """Whether the heartbeat is older than the stall threshold."""
        moment = self._clock() if now is None else now
        with self._lock:
            return (moment - self._last_beat) >= self._stall_seconds

    def check(self, *, now: float | None = None) -> bool:
        """Report a stall once per stall episode. Returns whether it reported."""
        if not self.is_stalled(now=now):
            return False
        with self._lock:
            if self._reported:  # already reported this episode; do not spam
                return False
            self._reported = True
            self._stalls += 1
            elapsed = (self._clock() if now is None else now) - self._last_beat
        stacks = dump_all_thread_stacks()
        self._logger.error(
            "main loop stalled for %.1fs (threshold %.1fs) - all thread stacks follow:\n%s",
            elapsed, self._stall_seconds, stacks,
        )
        if self._on_stall is not None:
            self._on_stall(stacks)
        return True

    def start(self, *, poll_seconds: float = 2.0) -> None:
        """Start watching on a background thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def _loop() -> None:
            while not self._stop.wait(poll_seconds):
                try:
                    self.check()
                except Exception:  # noqa: BLE001,S110 - a watchdog must never crash the app
                    pass

        self._thread = threading.Thread(target=_loop, name="stall-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop watching."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
