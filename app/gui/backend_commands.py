"""GUI-side execution command submitter (file write on a worker, never on Qt).

The GUI's ONLY influence over the backend's Tradovate DEMO service is the
bounded command file. This helper owns that write and performs it on a
short-lived background thread, so a click handler returns immediately and the
Qt thread never touches the filesystem. Results come back through the normal
status snapshot (``demo_last_command_result``) - there is no reply channel to
block on.
"""

from __future__ import annotations

import threading
from pathlib import Path

from app.runtime.process_files import CommandFile


class ExecutionCommander:
    """Submit execution commands to the backend without blocking the GUI."""

    def __init__(self, runtime_dir: Path | str = Path("runtime")) -> None:
        """Bind to the backend's runtime directory."""
        self._command_file = CommandFile(runtime_dir)
        self.last_submitted: str = ""

    def submit(self, name: str, args: dict[str, object] | None = None) -> None:
        """Queue one command; the write happens on a background thread."""
        def _write() -> None:
            try:
                self.last_submitted = self._command_file.submit(name, args)
            except Exception:  # noqa: BLE001,S110 - the status snapshot shows outcomes
                pass

        threading.Thread(target=_write, name="execution-command", daemon=True).start()
