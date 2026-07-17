"""Regression guards: tests must never write temp trees into the repository."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _tracked_files() -> list[str]:
    result = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, capture_output=True,
                            text=True, check=False)
    return result.stdout.splitlines()


def test_no_test_temp_trees_are_tracked() -> None:
    """pytest temp dirs (426 of them once) must never be committed again."""
    offenders = [
        path for path in _tracked_files()
        if path.startswith("pytest-clean-") or path.startswith(".pytest-tmp/")
        or "/pytest-clean-" in path
    ]
    assert not offenders, f"test temp trees are tracked: {offenders[:5]} ({len(offenders)} total)"


def test_no_credentials_or_env_files_are_tracked() -> None:
    """No .env / secret-ish file may ever enter version control."""
    offenders = [
        path for path in _tracked_files()
        if path.endswith(".env") or "secret" in path.lower() or "credential" in path.lower()
    ]
    assert not offenders, f"credential-like files tracked: {offenders}"


def test_gitignore_covers_test_temp_trees() -> None:
    """The ignore rules that prevent the regression must stay in place."""
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "pytest-clean-*/" in ignored
    assert ".env" in ignored


def test_pytest_basetemp_is_outside_the_repo() -> None:
    """A pytest run must not be configured to write its temp tree into the repo.

    The historical failure was `--basetemp` (or a default tmp root) pointing at
    the repository, which committed hundreds of throwaway files.
    """
    config = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    # If a basetemp is pinned in config at all, it must not be a repo-relative path.
    for line in config.splitlines():
        stripped = line.strip()
        if stripped.startswith("basetemp"):
            value = stripped.split("=", 1)[1].strip().strip("\"'")
            assert not (REPO_ROOT / value).resolve().is_relative_to(REPO_ROOT), (
                f"pytest basetemp writes inside the repo: {value}"
            )
