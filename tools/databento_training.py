"""Let machine learning train on Databento days already imported, or stop it; or just show it.

Imported days are for replay only unless training was allowed when they were imported (the
"Let machine learning train on this data" box). This changes that choice afterwards, for days
already imported. The data itself is never touched, and the catalog still decides: a day whose
quality checks failed stays out of training whatever is chosen here.

    py -m tools.databento_training                       # show each imported day, writes nothing
    py -m tools.databento_training --on                  # allow training on every imported day
    py -m tools.databento_training --on --day 2026-11-02 # only that day (repeat --day for more)
    py -m tools.databento_training --off                 # back to replay only
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.importers.databento import set_training_permission  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--on", action="store_true", help="allow machine-learning training")
    choice.add_argument("--off", action="store_true", help="replay only, no training")
    parser.add_argument("--day", action="append", metavar="YYYY-MM-DD", help="only this day (repeatable)")
    parser.add_argument("--raw", default="data/raw", help="the recordings folder (default data/raw)")
    args = parser.parse_args(argv)
    enabled = True if args.on else False if args.off else None
    results = set_training_permission(Path(args.raw), enabled, args.day)
    if not results:
        print("No imported Databento days found" + (f" for {', '.join(args.day)}." if args.day else "."))
        return 1
    for item in results:
        verdict = "TRAINS" if item.trainable else "replay only"
        change = "  (changed)" if item.changed else ""
        print(f"{item.day}  {verdict:<11}{change}")
        for reason in item.reasons:
            print(f"            {reason}")
    trainable = sum(1 for item in results if item.trainable)
    print(f"\n{trainable} of {len(results)} imported day(s) can be used for training.")
    if enabled is None:
        print("Nothing was changed. Use --on or --off to change it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
