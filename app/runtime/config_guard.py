"""Config-diff alert for the protected production config - item 40.

Any change to ``config/production_config.yaml`` must be explicitly
approved by a human before the system treats the config as trusted. No
silent hot-reload: an unapproved change is a hard refusal. This module
never writes the production config itself; it only hashes and compares.

Approve a reviewed change with:

    .venv\\Scripts\\python.exe -m app.runtime.config_guard approve
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("config/production_config.yaml")
DEFAULT_APPROVAL_PATH = Path("config/.production_config.approved.sha256")


class UnapprovedConfigChangeError(RuntimeError):
    """Raised when the production config changed without human approval."""


@dataclass(frozen=True, slots=True)
class ConfigCheckResult:
    """Outcome of a config trust check."""

    trusted: bool
    reason: str


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_config_trusted(
    config_path: Path = DEFAULT_CONFIG_PATH,
    approval_path: Path = DEFAULT_APPROVAL_PATH,
) -> ConfigCheckResult:
    """Compare the config hash against the last human-approved hash."""
    if not config_path.is_file():
        return ConfigCheckResult(trusted=True, reason="no production config exists yet")
    current = _hash_file(config_path)
    if not approval_path.is_file():
        return ConfigCheckResult(
            trusted=False,
            reason="production config has never been approved - run the approve command after review",
        )
    approved = approval_path.read_text(encoding="utf-8").strip()
    if current != approved:
        return ConfigCheckResult(
            trusted=False,
            reason="production config changed since the last approval - review it, then approve explicitly",
        )
    return ConfigCheckResult(trusted=True, reason="config matches the approved hash")


def require_trusted_config(
    config_path: Path = DEFAULT_CONFIG_PATH,
    approval_path: Path = DEFAULT_APPROVAL_PATH,
) -> None:
    """Raise loudly when the config is not trusted."""
    result = check_config_trusted(config_path, approval_path)
    if not result.trusted:
        raise UnapprovedConfigChangeError(result.reason)


def approve_config(
    config_path: Path = DEFAULT_CONFIG_PATH,
    approval_path: Path = DEFAULT_APPROVAL_PATH,
) -> str:
    """Record the current config hash as human-approved. Deliberate action only."""
    if not config_path.is_file():
        raise FileNotFoundError(f"nothing to approve: {config_path} does not exist")
    digest = _hash_file(config_path)
    approval_path.parent.mkdir(parents=True, exist_ok=True)
    approval_path.write_text(digest + "\n", encoding="utf-8")
    return digest


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: ``check`` (default) or ``approve``."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    command = arguments[0] if arguments else "check"
    if command == "approve":
        digest = approve_config()
        print(f"Approved production config: sha256 {digest}")
        return 0
    result = check_config_trusted()
    print(("TRUSTED: " if result.trusted else "NOT TRUSTED: ") + result.reason)
    return 0 if result.trusted else 1


if __name__ == "__main__":
    raise SystemExit(main())
