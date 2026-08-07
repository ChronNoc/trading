"""CI enforcement of AGENTS.md's protected-files rule - item 48.

Fails when a diff touches a protected file without the explicit marker
file ``.protected-change-approved`` present at the repo root. Run in CI:

    .venv\\Scripts\\python.exe -m tools.check_protected_files
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

PROTECTED_FILES = (
    "app/risk/limits.py",
    "app/risk/sizing.py",
    "app/risk/kill_switch.py",
    "app/execution/live_execution.py",
    "app/execution/reconciliation.py",
    "config/production_config.yaml",
)
MARKER_FILENAME = ".protected-change-approved"


def check_diff(changed_files: Sequence[str], *, marker_present: bool) -> tuple[bool, str]:
    """Return (ok, message) for a list of changed paths."""
    normalized = {path.replace("\\", "/") for path in changed_files}
    touched = sorted(normalized & set(PROTECTED_FILES))
    if not touched:
        return True, "no protected files touched"
    if marker_present:
        return True, f"protected files changed WITH explicit approval marker: {touched}"
    return False, (
        f"protected files changed without the {MARKER_FILENAME} marker: {touched} - "
        "AGENTS.md requires an explicit task flag for these files"
    )


def changed_files_in_working_tree(repo_root: Path) -> tuple[str, ...]:
    """List files changed in the working tree plus staged changes."""
    completed = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        cwd=repo_root,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git diff failed: {completed.stderr.strip()}")
    return tuple(line.strip() for line in completed.stdout.splitlines() if line.strip())


def main(argv: Sequence[str] | None = None) -> int:
    """Run the check against the current repository."""
    repo_root = Path(__file__).resolve().parents[1]
    changed = changed_files_in_working_tree(repo_root)
    marker_present = (repo_root / MARKER_FILENAME).is_file()
    ok, message = check_diff(changed, marker_present=marker_present)
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
