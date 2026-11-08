"""Write or verify the strategy constants audit report.

    .venv\\Scripts\\python.exe -m tools.constants_audit           # rewrite the report
    .venv\\Scripts\\python.exe -m tools.constants_audit --check   # exit 1 if stale or unrated

The report is ``docs/audits/strategy_constants_audit.md``. It is generated from the
code, so it is never edited by hand; see ``app/research/constants_audit.py`` for the
rubric and the per-parameter ratings.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from app.research.constants_audit import REPORT_RELATIVE_PATH, render_report, stale_ratings, unrated


def _normalise(text: str) -> str:
    return text.replace("\r\n", "\n").rstrip("\n")


def main(argv: Sequence[str] | None = None) -> int:
    """Rewrite the report, or with ``--check`` verify it without writing."""
    parser = argparse.ArgumentParser(description="Write or verify the strategy constants audit.")
    parser.add_argument("--check", action="store_true", help="verify only; exit 1 if stale or unrated")
    parser.add_argument("--output", type=Path, default=Path(REPORT_RELATIVE_PATH))
    args = parser.parse_args(argv)

    missing, stale = unrated(), stale_ratings()
    for key in missing:
        print(f"UNRATED parameter (add it to RATINGS in app/research/constants_audit.py): {key}")
    for key in stale:
        print(f"STALE rating (parameter no longer exists): {key}")
    if missing or stale:
        return 1

    text = render_report()
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.is_file() else ""
        if _normalise(current) != _normalise(text):
            print(f"{args.output} is out of date; run: python -m tools.constants_audit")
            return 1
        print(f"{args.output} is up to date.")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
