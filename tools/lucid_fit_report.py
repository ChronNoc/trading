"""Print how a Lucid evaluation ends under the governor and under the old production rules.

    py -m tools.lucid_fit_report [--runs 200] [--edges=-0.15,0,0.1,0.2] [--stop 40] [--target 80]

``edge`` is the strategy's TRUE net expectancy per trade in R (an assumption you choose, not a
prediction). The table shows what edge an account needs before it has a fair chance, and that
the governor turns "busted" into "retired". Deterministic for a given ``--seed``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

if __package__ in (None, ""):  # `py tools/lucid_fit_report.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.risk.account_profile import lucid_flex_25k  # noqa: E402
from app.risk.lucid_fit import FitScenario, estimate  # noqa: E402
from app.risk.lucid_governor import load_governor_policy  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, default=200, help="simulated evaluations per row (default 200)")
    parser.add_argument("--edges", default="-0.15,-0.05,0,0.05,0.10,0.20,0.35",
                        help="comma-separated true expectancies per trade, in R, net of costs")
    parser.add_argument("--stop", type=int, default=40, help="stop distance in ticks (default 40)")
    parser.add_argument("--target", type=int, default=80, help="target distance in ticks (default 80)")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--governor-config", type=Path, default=Path("config/risk_governor.yaml"))
    args = parser.parse_args(argv)

    profile = lucid_flex_25k()
    policy = load_governor_policy(args.governor_config)
    print(f"{profile.display_name}: target ${profile.profit_target}, max loss ${profile.max_loss_limit}, "
          f"stop {args.stop} / target {args.target} ticks, {args.runs} runs per row, 250-day horizon\n")
    header = f"{'edge (R/trade)':>14} | {'rules':9} | {'pass':>5} {'retired':>7} {'failed':>6} {'other':>5} | {'median days to pass':>19}"
    print(header)
    print("-" * len(header))
    for raw in args.edges.split(","):
        scenario = FitScenario(edge_r=Decimal(raw.strip()), stop_ticks=args.stop, target_ticks=args.target)
        for governed in (False, True):
            summary = estimate(profile, scenario, runs=args.runs, seed=args.seed, governed=governed, policy=policy)
            other = summary.stalled + summary.active
            days = "-" if summary.median_days_to_pass is None else str(summary.median_days_to_pass)
            print(f"{raw.strip():>14} | {'governed' if governed else 'old rules':9} | "
                  f"{summary.rate(summary.passed):>5.0%} {summary.rate(summary.retired):>7.0%} "
                  f"{summary.rate(summary.failed):>6.0%} {summary.rate(other):>5.0%} | {days:>19}")
    print("\nretired = the governor stopped the account with room left; failed = the floor was touched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
