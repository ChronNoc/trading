"""Where the user's Databento API key is kept: outside the repository, never logged.

The repository is public, so the key must never be written inside it. It is read from the
``DATABENTO_API_KEY`` environment variable if set (the variable Databento's own tools use), and
otherwise from a small file in the user's application-data folder
(``%APPDATA%\\MNQ Intelligence\\databento_api_key.txt`` on Windows), which only that Windows user
can read by default. The user types the key into the app themselves; nothing else ever does.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

ENV_VAR = "DATABENTO_API_KEY"
APP_FOLDER = "MNQ Intelligence"
KEY_FILE_NAME = "databento_api_key.txt"


def key_file(environ: Mapping[str, str] | None = None) -> Path:
    """The file the key is saved in (never inside the repository)."""
    environ = os.environ if environ is None else environ
    base = environ.get("APPDATA") or str(Path.home() / ".config")
    return Path(base) / APP_FOLDER / KEY_FILE_NAME


def validate_api_key(key: str) -> str:
    """Return the cleaned key, or raise ``ValueError`` with a plain reason."""
    cleaned = (key or "").strip()
    if not cleaned:
        raise ValueError("The API key is empty.")
    if any(character.isspace() for character in cleaned):
        raise ValueError("The API key must not contain spaces.")
    if not cleaned.startswith("db-"):
        raise ValueError('Databento API keys start with "db-". Copy it from your Databento portal (API Keys).')
    if not 10 <= len(cleaned) <= 128 or not cleaned.replace("-", "").replace("_", "").isalnum():
        raise ValueError("That does not look like a Databento API key.")
    return cleaned


def load_api_key(environ: Mapping[str, str] | None = None, path: Path | None = None) -> str | None:
    """The key from the environment, else from the key file, else None."""
    environ = os.environ if environ is None else environ
    from_env = (environ.get(ENV_VAR) or "").strip()
    if from_env:
        return from_env
    path = path or key_file(environ)
    try:
        stored = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return stored or None


def save_api_key(key: str, path: Path | None = None) -> Path:
    """Validate and save the key to the key file (replacing any earlier one)."""
    cleaned = validate_api_key(key)
    path = path or key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(cleaned + "\n", encoding="utf-8")
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)  # owner-only where the OS honours it (POSIX); Windows keeps the profile ACL
    except OSError:
        pass
    return path


def forget_api_key(path: Path | None = None) -> bool:
    """Delete the saved key file. Returns whether there was one."""
    path = path or key_file()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def describe_api_key(key: str | None) -> str:
    """A safe label for the key: ``db-...WXYZ`` (never the whole key)."""
    if not key:
        return "not set"
    return f"db-...{key[-4:]}" if len(key) > 8 else "set"
