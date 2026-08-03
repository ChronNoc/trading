"""One-click desktop launcher for the MNQ Intelligence assistant.

A thin, robust wrapper around the EXISTING startup architecture
(``tools.start_assistant`` -> backend supervisor + attached GUI). It does NOT
create a parallel backend: ``start_assistant`` already detects a running
backend, avoids duplicates, waits for readiness, starts the GUI, and preserves
logs. Delayed-paper mode only - it never enables live trading or broker
execution (delayed data structurally cannot reach a broker).

It is meant to run windowless via ``pythonw.exe`` (no console). Because there is
then no console to read, any startup failure is surfaced in a native Windows
message box that names the log directory - a one-click launch never fails
silently.

    .venv\\Scripts\\pythonw.exe -m tools.launch_desktop
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Sequence

# Delayed-paper mode: records the source delay and simulates causally; broker
# routing is disabled and LIVE stays locked. No live/broker flags are ever set.
DEFAULT_ARGS: tuple[str, ...] = ("--delayed-data-minutes", "15")

ERROR_TITLE = "MNQ Intelligence could not start"


def repo_root() -> Path:
    """Resolve the repository root from this file's location."""
    return Path(__file__).resolve().parent.parent


def show_error(title: str, message: str) -> None:
    """Show a native Windows error dialog; fall back to stderr elsewhere."""
    try:
        import ctypes

        # MB_ICONERROR (0x10) | MB_SETFOREGROUND (0x10000)
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10 | 0x10000)
    except Exception:  # noqa: BLE001 - non-Windows / headless: stderr is the fallback
        print(f"{title}: {message}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    """Launch the assistant, surfacing any startup failure clearly."""
    root = repo_root()
    os.chdir(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    log_dir = root / "logs"

    try:
        from tools.start_assistant import AssistantStartupError
        from tools.start_assistant import main as assistant_main
    except Exception as error:  # noqa: BLE001 - broken environment
        show_error(
            ERROR_TITLE,
            "Failed to load the application (check the .venv Python environment):\n\n"
            f"{type(error).__name__}: {error}\n\nLogs: {log_dir}")
        return 2

    forwarded = list(argv) if argv is not None else list(DEFAULT_ARGS)
    try:
        return int(assistant_main(forwarded))
    except SystemExit as exit_error:
        return int(exit_error.code or 0)
    except AssistantStartupError as error:
        show_error(ERROR_TITLE, f"Startup failed:\n\n{error}\n\nLogs and details: {log_dir}")
        return 2
    except Exception as error:  # noqa: BLE001 - last resort; never fail silently
        show_error(
            "MNQ Intelligence crashed during startup",
            f"{type(error).__name__}: {error}\n\nSee the log directory: {log_dir}")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
