"""CLI for one full strategy-discovery run ending at the validation gate.

    .venv\\Scripts\\python.exe -m tools.run_discovery --output-root data/discovery

Produces candidates.jsonl, per-worker traces, and (when a candidate clears
every gate) recommendation.md. Then it stops - see Part 7 / AGENTS.md.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from app.discovery.episodes import generate_synthetic_episodes
from app.discovery.search import SearchLimits, SearchRateLimiter, run_search
from app.discovery.splits import split_holdout
from app.discovery.supervisor import ModeSupervisor

DEFAULT_OUTPUT_ROOT = Path("data/discovery")


def main(argv: Sequence[str] | None = None) -> int:
    """Run one rate-limited strategy-discovery search.

    Defaults to real recorded episodes. The synthetic generator (a planted
    edge) is a developer/demo path only, reachable exclusively via
    ``--data-source synthetic`` and written under a clearly-separated output
    root so synthetic candidates can never be mistaken for real research.
    """
    parser = argparse.ArgumentParser(description="Run one strategy-discovery search.")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--data-source",
        choices=("real", "synthetic"),
        default="real",
        help="'real' (default) uses recorded episodes; 'synthetic' is a dev/demo edge.",
    )
    parser.add_argument("--seed", type=int, default=20260713)
    parser.add_argument("--trading-days", type=int, default=120)
    parser.add_argument("--minimum-trades", type=int, default=100)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--min-run-interval-seconds", type=float, default=3600.0)
    parser.add_argument("--skip-rate-limit", action="store_true", help="Testing only.")
    args = parser.parse_args(argv)

    mode = ModeSupervisor().view()
    print(f"Mode: {mode.mode} (LIVE armed: {mode.live_armed}) - discovery never changes this.")

    if args.data_source == "real":
        # Real episodes come from replaying recorded sessions through the
        # deterministic strategy with no-lookahead labeling. Until that engine
        # has produced episodes, there is nothing to search - and no
        # performance claim is generated.
        print(
            "data-source=real: no eligible real episodes are available yet "
            "(build them with 'python -m tools.build_real_episodes'). "
            "No candidates and no performance claim were generated.",
        )
        print("Synthetic search is a dev/demo path only: pass --data-source synthetic explicitly.")
        return 0

    print(
        "WARNING: --data-source synthetic uses a PLANTED synthetic edge. Output is a "
        "developer/demo artifact and must never be presented as real performance.",
    )
    synthetic_root = args.output_root / "synthetic"
    episodes = generate_synthetic_episodes(
        seed=args.seed,
        start_date=date(2026, 1, 5),
        trading_days=args.trading_days,
    )
    search_episodes, holdout = split_holdout(episodes)
    limiter = None
    if not args.skip_rate_limit:
        limiter = SearchRateLimiter(
            synthetic_root / ".last_search_run",
            min_seconds_between_runs=args.min_run_interval_seconds,
        )
    result = run_search(
        search_episodes,
        holdout,
        base_seed=args.seed,
        output_root=synthetic_root,
        limits=SearchLimits(max_workers=args.max_workers),
        minimum_trades=args.minimum_trades,
        rate_limiter=limiter,
    )

    print(f"[SYNTHETIC] Candidates evaluated: {len(result.candidates)}")
    print(f"[SYNTHETIC] Candidates log: {result.candidates_log_path}")
    if result.leader is None:
        print("[SYNTHETIC] No candidate cleared every gate. See the candidates log.")
        return 0
    print(f"[SYNTHETIC] Leader: {result.leader.parameters.key()}")
    print("[SYNTHETIC] Developer/demo only - NOT real performance, never for the paper ledger.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
