"""Generate the paper-trading daily report from the real ledger, on demand.

    .venv\\Scripts\\python.exe -m tools.paper_daily_report --date 2026-07-17

Read-only over the ledger; writes only the report. Synthetic fixture trades are
excluded from every statistic, and zero real trades is reported honestly rather
than as a blank or an implied record.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from app.paper.daily_report import render_paper_daily_markdown, write_paper_daily_report
from app.paper.ledger import PaperLedger
from app.paper.daily_report import build_paper_daily_report
from app.risk.account_profile import load_selected_profile


def main(argv: Sequence[str] | None = None) -> int:
    """Write the report and echo it. Returns 0 (reporting is never a failure)."""
    parser = argparse.ArgumentParser(description="Paper-trading daily report.")
    parser.add_argument("--date", default=datetime.now(UTC).date().isoformat(),
                        help="report date (YYYY-MM-DD); defaults to today (UTC)")
    parser.add_argument("--ledger", type=Path, default=Path("data/paper/paper_trades.jsonl"))
    parser.add_argument("--report-root", type=Path, default=Path("data/reports/paper"))
    args = parser.parse_args(argv)

    starting_balance = load_selected_profile().account_size
    if not args.ledger.is_file():
        report = build_paper_daily_report(args.date, PaperLedger.recover(args.ledger),
                                          starting_balance=starting_balance)
        print(render_paper_daily_markdown(report))
        print(f"(no ledger at {args.ledger}; nothing persisted)")
        return 0

    markdown_path = write_paper_daily_report(
        args.report_root, args.date, args.ledger, starting_balance=starting_balance,
    )
    print(markdown_path.read_text(encoding="utf-8"))
    print(f"Written: {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
