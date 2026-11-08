"""CLI that fills the walk-forward volatility cache from recorded sessions (offline).

    .venv\\Scripts\\python.exe -m tools.session_volatility

It reads only the first New York trading hour (09:30-10:30) of each FINISHED real
session and writes one small JSON file (default ``data/processed/session_volatility.json``).
The Sessions & Replay page reads that file; nothing heavy ever runs inside the live
backend. Safe to re-run: sessions that are already analysed are skipped. Run it while
the backend is idle, because it reads parquet from disk.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.research.causal_context import trading_day_for_timestamp
from app.research.session_catalog import build_catalog
from app.research.session_volatility import (
    LABELS,
    default_cache_path,
    label_for_session_start,
    load_regime_table,
    update_cache,
)


@dataclass(frozen=True, slots=True)
class SessionVolatilityConfig:
    """Configuration for the volatility cache CLI."""

    raw_root: Path = Path("data/raw")
    cache_path: Path | None = None
    quiet: bool = False


def parse_args(argv: Sequence[str] | None = None) -> SessionVolatilityConfig:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Fill the walk-forward volatility cache (first-hour range per trading day).",
    )
    parser.add_argument("--raw-root", type=Path, default=Path("data/raw"))
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="cache file (default: <data>/processed/session_volatility.json next to --raw-root)",
    )
    parser.add_argument("--quiet", action="store_true", help="print only the final summary")
    args = parser.parse_args(argv)
    return SessionVolatilityConfig(
        raw_root=Path(args.raw_root),
        cache_path=Path(args.cache) if args.cache else None,
        quiet=bool(args.quiet),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Refresh the cache and print what the walk-forward regime can and cannot say yet."""
    config = parse_args(argv)
    cache_path = config.cache_path or default_cache_path(config.raw_root)
    summary = update_cache(
        config.raw_root,
        cache_path,
        progress=None if config.quiet else print,
    )
    table = load_regime_table(cache_path)
    print(f"\ncache: {cache_path}")
    print(
        f"sessions analysed now: {summary.analysed}   already cached: {summary.already_cached}   "
        f"without a full first hour: {summary.no_first_hour}   unfinished/synthetic skipped: {summary.skipped_unfinished}",
    )
    for error in summary.errors:
        print(f"  ERROR (will retry next run): {error}")
    print(f"trading days with a first-hour range: {table.observed_days}")

    today = date.fromisoformat(trading_day_for_timestamp(time.time_ns()))
    current = table.label_for(today)
    print(f"regime going into today ({today}): {current.label} - {current.reason}")

    counts: Counter[str] = Counter(
        label_for_session_start(table, entry.utc_start).label
        for entry in build_catalog(config.raw_root)
    )
    print("recorded sessions by regime label: " + "  ".join(f"{label}: {counts.get(label, 0)}" for label in LABELS))
    return 1 if summary.errors else 0


if __name__ == "__main__":
    sys.exit(main())
