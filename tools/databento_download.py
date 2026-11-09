"""Buy MNQ order-book history from Databento and import it (the command-line twin of the window).

    py -m tools.databento_download --days 30                     # price only: buys nothing
    py -m tools.databento_download --start 2026-08-01 --end 2026-08-31
    py -m tools.databento_download --days 30 --buy --max-cost 25  # buy, but only if it costs <= $25

The key is your own: ``DATABENTO_API_KEY`` in the environment, or the one saved from the import
window. Without ``--buy`` nothing is ever bought; with it, ``--max-cost`` is your confirmation and
nothing is bought if the price is above it. Days already imported are never bought again, and a
stopped run continues from what it already bought. Historical only: the live feed stays Bookmap.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

if __package__ in (None, ""):  # `py tools/databento_download.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.importers.databento import ImportOptions, import_many  # noqa: E402
from app.importers.databento_api import DatabentoError, HistoricalClient  # noqa: E402
from app.importers.databento_download import (  # noqa: E402
    DownloadEvent,
    download_plan,
    latest_available_day,
    plan_download,
    remove_downloads,
)
from app.importers.databento_key import ENV_VAR, load_api_key  # noqa: E402


def _money(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation as error:
        raise argparse.ArgumentTypeError(f"not an amount: {text!r}") from error
    if not value.is_finite() or value < 0:
        raise argparse.ArgumentTypeError("the amount must be zero or more")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    span = parser.add_mutually_exclusive_group()
    span.add_argument("--days", type=int, help="the last N UTC days up to yesterday (default 30)")
    span.add_argument("--start", type=date.fromisoformat, help="first UTC day, YYYY-MM-DD")
    parser.add_argument("--end", type=date.fromisoformat, help="last UTC day, YYYY-MM-DD (with --start)")
    parser.add_argument("--buy", action="store_true", help="buy and import (without it: price only)")
    parser.add_argument("--max-cost", type=_money, help="with --buy: the most you accept to pay, in US dollars")
    parser.add_argument("--allow-training", action="store_true", help="let machine learning train on the data")
    parser.add_argument("--out", type=Path, default=Path("data/raw"), help="recordings folder (default data/raw)")
    args = parser.parse_args(argv)
    if args.buy and args.max_cost is None:
        parser.error("--buy needs --max-cost: the most you accept to pay")
    if args.start is not None and args.end is None:
        parser.error("--start needs --end")
    if args.days is not None and args.days < 1:
        parser.error("--days must be at least 1")
    return args


def _say(event: DownloadEvent) -> None:
    if event.stage == "downloading" and event.total and event.done not in (0, event.total):
        return
    print(f"  {event.message}", flush=True)


def main(argv: Sequence[str] | None = None, *, client: HistoricalClient | None = None) -> int:
    args = parse_args(argv)
    if client is None:
        key = load_api_key()
        if not key:
            print(f"No Databento API key: set {ENV_VAR}, or save it in the Databento Import window.",
                  file=sys.stderr)
            return 2
        client = HistoricalClient(key)
    if args.start is not None:
        first, last = args.start, args.end
    else:
        last = latest_available_day()
        first = last - timedelta(days=(args.days or 30) - 1)
    try:
        plan = plan_download(client, args.out, first, last, progress=_say)
    except (DatabentoError, ValueError) as error:
        print(f"No price: {error}", file=sys.stderr)
        return 1
    print(f"{first} to {last}: {plan.days_to_buy} day(s) to buy for ${plan.total_cost:.2f}; "
          f"{len(plan.paid_days)} already paid for; {len(plan.already_imported)} already imported.")
    if plan.nothing_to_do:
        print("Nothing to do.")
        return 0
    if not args.buy:
        print("Price only: nothing was bought. Add --buy --max-cost <dollars> to buy.")
        return 0
    if plan.total_cost > args.max_cost:
        print(f"Not bought: ${plan.total_cost:.2f} is above your --max-cost ${args.max_cost:.2f}.", file=sys.stderr)
        return 1
    try:
        files = download_plan(client, plan, args.out, confirmed_cost=min(plan.total_cost, args.max_cost),
                              progress=_say)
    except DatabentoError as error:
        print(f"Stopped: {error}", file=sys.stderr)
        return 1
    results = import_many(files, ImportOptions(out_root=args.out, allow_model_training=args.allow_training))
    done = {Path(r.source) for r in results if r.status in ("imported", "duplicate")}
    kept = {Path(r.source) for r in results if r.status not in ("imported", "duplicate")}
    remove_downloads(path for path in files if path in done and path not in kept)
    for result in results:
        print(f"  {result.day or '-'}  {result.status:9}  {result.headline}")
    imported = sum(r.status == "imported" for r in results)
    print(f"Imported {imported} session(s).")
    return 0 if not kept else 1


if __name__ == "__main__":
    raise SystemExit(main())
