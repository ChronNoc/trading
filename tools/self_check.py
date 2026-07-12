"""Automated self-check: run the test suite and write a dated status report.

Intended to run nightly (Windows Task Scheduler) so a regression surfaces
in logs/self_check/ before the next trading-prototype session, keeping the
tooling as consistent as the trading rules it protects. Read-only apart
from the report file - it never touches data, config, or broker code.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_REPORT_ROOT = Path("logs/self_check")
DEFAULT_PYTEST_BASETEMP = Path(".pytest-tmp/self-check-pytest")
_OUTPUT_TAIL_LINES = 40

CheckRunner = Callable[[], tuple[int, str]]


def _run_pytest(*, basetemp: Path = DEFAULT_PYTEST_BASETEMP) -> tuple[int, str]:
    """Run the full pytest suite and capture its output."""
    basetemp.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(basetemp),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout + completed.stderr


def run_self_check(
    *,
    runner: CheckRunner | None = None,
    report_root: Path = DEFAULT_REPORT_ROOT,
    pytest_basetemp: Path = DEFAULT_PYTEST_BASETEMP,
    now: datetime | None = None,
) -> Path:
    """Run the check, write a markdown report, and return the report path."""
    moment = now or datetime.now(timezone.utc)
    exit_code, output = runner() if runner is not None else _run_pytest(basetemp=pytest_basetemp)
    verdict = "PASS" if exit_code == 0 else "FAIL"

    tail = "\n".join(output.strip().splitlines()[-_OUTPUT_TAIL_LINES:])
    lines = [
        f"# Self-check {verdict}",
        "",
        f"- Timestamp: {moment.strftime('%Y-%m-%d %H:%M UTC')}",
        f"- Test-suite exit code: {exit_code}",
        "",
        "## Output tail",
        "",
        "```",
        tail,
        "```",
        "",
    ]
    if verdict == "FAIL":
        lines.insert(
            2,
            "**The prototype should not be trusted until this is fixed.** Re-run the suite and inspect the failures.",
        )
        lines.insert(3, "")

    report_root.mkdir(parents=True, exist_ok=True)
    report_path = report_root / f"self_check_{moment.strftime('%Y-%m-%d_%H%M%S')}.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the nightly self-check."""
    parser = argparse.ArgumentParser(description="Run the MNQ assistant self-check and write a status report.")
    parser.add_argument("--report-root", type=Path, default=DEFAULT_REPORT_ROOT)
    parser.add_argument("--pytest-basetemp", type=Path, default=DEFAULT_PYTEST_BASETEMP)
    args = parser.parse_args(argv)
    report_path = run_self_check(report_root=args.report_root, pytest_basetemp=args.pytest_basetemp)
    content = report_path.read_text(encoding="utf-8")
    passed = content.startswith("# Self-check PASS")
    print(f"Self-check report: {report_path}")
    print("Result: PASS" if passed else "Result: FAIL - inspect the report before trusting the prototype.")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
