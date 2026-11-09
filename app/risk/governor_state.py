"""Save and restore the Lucid governor's book, so a restart cannot reset the account.

A Lucid evaluation runs for days or weeks; the backend restarts far more often than that. Before
this file existed every restart forgot the day's loss, the losing streak, the day lock, a retired
account and the high-water mark, and the paper account itself went back to $25,000. The governor
now writes its whole book (``GovernorBook.to_state``) here after every change and reads it back
at start-up.

The file lives next to the paper ledger (``data/paper/governor_state.json``, gitignored).
Deleting it while the backend is stopped starts a NEW evaluation attempt; that is the only way
to reset the account, and it is deliberate.

Writes are atomic (a temporary file in the same folder, flushed to disk, then renamed over the
old one), so a crash mid-write leaves the previous complete state, never half a file.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

STATE_FILE_NAME = "governor_state.json"
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF_SECONDS = 0.05


def default_state_path(paper_ledger_path: Path | str) -> Path:
    """Where the governor's state is kept for a given paper ledger (the same folder)."""
    return Path(paper_ledger_path).parent / STATE_FILE_NAME


def load_book_state(path: Path | str) -> dict[str, object] | None:
    """Return the saved book document, or None when nothing was saved yet.

    A file that exists but cannot be read or parsed raises: the caller must then refuse to
    trade rather than silently start a fresh account.
    """
    path = Path(path)
    if not path.is_file():
        return None
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return document


def save_book_state(path: Path | str, state: Mapping[str, object]) -> None:
    """Write the book document atomically (temp file, fsync, rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(dict(state), stream, indent=1, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                # Windows: a reader (antivirus, indexer) can hold the old file for a moment.
                if attempt == _REPLACE_ATTEMPTS - 1:
                    raise
                time.sleep(_REPLACE_BACKOFF_SECONDS)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
