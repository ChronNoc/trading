"""CLI for writing observe-only daily market learning reports."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from app.machine_learning.daily_learning import analyze_and_write_daily_learning_report


@dataclass(frozen=True, slots=True)
class DailyLearningSummaryConfig:
    """Configuration for the daily learning summary CLI."""

    raw_root: Path = Path("data/raw")
    report_root: Path = Path("data/reports")
    processed_root: Path = Path("data/processed")
    trading_date: date = datetime.now(UTC).date()


def parse_args(argv: Sequence[str] | None = None) -> DailyLearningSummaryConfig:
    """Parse CLI arguments for a daily learning report run."""
    parser = argparse.ArgumentParser(description="Write an observe-only daily market learning summary.")
    parser.add_argument("--raw-root", type=Path, default=Path("data/raw"))
    parser.add_argument("--report-root", type=Path, default=Path("data/reports"))
    parser.add_argument("--processed-root", type=Path, default=Path("data/processed"))
    parser.add_argument(
        "--date",
        dest="trading_date",
        type=_parse_date,
        default=datetime.now(UTC).date(),
        help="UTC recording date to analyze, formatted YYYY-MM-DD.",
    )
    args = parser.parse_args(argv)
    return DailyLearningSummaryConfig(
        raw_root=Path(args.raw_root),
        report_root=Path(args.report_root),
        processed_root=Path(args.processed_root),
        trading_date=args.trading_date,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the daily learning summary CLI."""
    try:
        config = parse_args(argv)
        paths = analyze_and_write_daily_learning_report(
            config.raw_root,
            config.report_root,
            config.trading_date,
            processed_root=config.processed_root,
        )
    except Exception as error:
        print(f"Daily learning summary failed: {error}", file=sys.stderr)
        return 2
    print(f"Daily learning summary written: {paths.markdown_path}", flush=True)
    print(f"Machine-readable summary written: {paths.json_path}", flush=True)
    return 0


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("--date must be formatted YYYY-MM-DD") from error


if __name__ == "__main__":
    raise SystemExit(main())
