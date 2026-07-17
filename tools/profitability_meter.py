"""Print how close the bot is to PROVEN profitable, from real evidence only.

    .venv\\Scripts\\python.exe -m tools.profitability_meter

Read-only: no network, no writes, no orders. The meter is an ordered ladder of
evidence gates — a gate counts only once every earlier gate has passed, so the
bot can never look "80% profitable" while it has zero completed setups. Any gate
past the sample threshold reports ``insufficient_evidence`` rather than a
fabricated pass.

This prints evidence, not a forecast, and it never asserts profitability the
data does not support.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from app.research.profitability_progress import load_progress

_BAR_WIDTH = 40


def _bar(fraction: float) -> str:
    filled = int(round(fraction * _BAR_WIDTH))
    return "[" + "#" * filled + "-" * (_BAR_WIDTH - filled) + "]"


def main(argv: Sequence[str] | None = None) -> int:
    """Print the profitability ladder and return 0 (reporting is never a failure)."""
    parser = argparse.ArgumentParser(description="Honest profitability progress meter.")
    parser.add_argument("--raw-root", type=Path, default=Path("data/raw"))
    parser.add_argument("--processed-root", type=Path, default=Path("data/processed"))
    args = parser.parse_args(argv)

    progress = load_progress(args.raw_root, args.processed_root)
    print("=" * 78)
    print(f"  PROGRESS TO PROVEN PROFITABLE   {_bar(progress.fraction)} {progress.fraction:6.1%}")
    print(f"  Profitability claim supported by evidence: "
          f"{'YES' if progress.profitable_claim_supported else 'NO'}")
    print("=" * 78)
    print(progress.headline)
    print()
    for gate in progress.gates:
        print(f"[{gate.status.upper():>21}]  {gate.label}")
        print(f"{'':>24}  observed: {gate.observed}")
        print(f"{'':>24}  required: {gate.threshold}")
        if gate.next_action:
            print(f"{'':>24}  next:     {gate.next_action}")
    print()
    if not progress.profitable_claim_supported:
        print("This bot is NOT proven profitable. The gates above are what would have to")
        print("pass on real recorded data before any such claim could be made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
