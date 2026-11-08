#!/usr/bin/env python3
"""Import Databento MBP-10 CSV files as replay sessions (command line).

This is the same engine the drop-in window uses (``app/importers/databento.py``); see
``docs/DATABENTO_IMPORT.md`` for how to download the data.

    py -m tools.databento_to_session  FILE_OR_FOLDER [FILE_OR_FOLDER ...]
        [--out data/raw] [--allow-model-training] [--instrument-id N]

A file may be a ``.csv``, ``.csv.zst``, ``.csv.gz`` or a ``.zip`` of them, and may hold one
day or a month: every UTC day becomes its own session. Folders are searched, and a trades
file or Databento's ``.json`` files in the same folder are skipped with a note.

Prove the whole path first, with no purchase and no download:

    py -m tools.databento_to_session --selftest

The older ``--depth FILE`` / ``--batch FOLDER`` forms still work; ``--trades`` is accepted
and ignored, because the order-book file already contains every trade.
"""

from __future__ import annotations

import argparse
import csv
import sys
import tempfile
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path

if __package__ in (None, ""):  # `py tools/databento_to_session.py` as a script
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.importers.databento import (  # noqa: E402
    ImportOptions,
    ImportResult,
    ProgressEvent,
    import_many,
)

_TAGS = {
    "imported": "[ok]     ", "duplicate": "[skip]   ", "skipped": "[skip]   ",
    "refused": "[refused]", "failed": "[FAILED] ", "cancelled": "[stopped]",
}


def _print_result(result: ImportResult) -> None:
    print(f"  {_TAGS.get(result.status, '[?]      ')} {result.headline}")
    if result.status == "imported":
        replay = "usable for replay" if result.replay_ok else "NOT usable for replay"
        training = "model training allowed" if result.training_ok else "model training off"
        print(f"             {replay}; {training}")
        for problem in result.problems:
            print(f"             problem: {problem}")
        if not result.replay_ok:
            for reason in result.replay_reasons:
                print(f"             why: {reason}")
        for note in result.notes[:2]:
            print(f"             note: {note}")
    for problem in result.problems if result.status == "failed" else ():
        print(f"             detail: {problem}")


def _progress_printer() -> Callable[[ProgressEvent], None]:
    last = {"line": ""}

    def show(event: ProgressEvent) -> None:
        if event.stage not in ("scanning", "converting"):
            return
        where = f"file {event.file_index}/{event.file_count} " if event.file_count > 1 else ""
        percent = f" {100 * event.done // event.total}%" if event.total else ""
        line = f"  {where}{event.stage}: {event.done:,} rows{percent}"
        if line != last["line"] and sys.stderr.isatty():
            print("\r" + line.ljust(70), end="", file=sys.stderr, flush=True)
            last["line"] = line

    return show


def run(paths: Sequence[Path], options: ImportOptions, *, quiet: bool = False) -> int:
    if not quiet:
        print(f"importing {len(paths)} item(s) into {options.out_root} ...")
    results = import_many(paths, options, progress=None if quiet else _progress_printer(), on_result=None)
    if not quiet and sys.stderr.isatty():
        print("\r" + " " * 70 + "\r", end="", file=sys.stderr)
    for result in results:
        _print_result(result)
    counts = {status: sum(r.status == status for r in results) for status in _TAGS}
    print(
        f"done: {counts['imported']} session(s) imported, {counts['duplicate']} already there, "
        f"{counts['skipped']} skipped, {counts['refused']} refused, {counts['failed']} failed."
    )
    if counts["failed"] or (counts["refused"] and not counts["imported"] and not counts["duplicate"]):
        return 1
    return 0


# --- self test ---------------------------------------------------------------------------


def _write_sample(path: Path) -> Path:
    """A tiny but correctly-shaped Databento mbp-10 CSV: a book that moves, and three trades."""
    undef = "9223372036854775807"
    header = ["ts_recv", "ts_event", "rtype", "publisher_id", "instrument_id", "action", "side", "depth",
              "price", "size", "flags", "ts_in_delta", "sequence"]
    for level in range(10):
        header += [f"bid_px_{level:02d}", f"ask_px_{level:02d}", f"bid_sz_{level:02d}", f"ask_sz_{level:02d}",
                   f"bid_ct_{level:02d}", f"ask_ct_{level:02d}"]
    header.append("symbol")

    def fp(price: str) -> str:
        return str(int(Decimal(price) * 1_000_000_000))

    base = 1_752_537_751_000_000_000
    book = {"bid": [("29500.00", 10), ("29499.75", 5)], "ask": [("29500.25", 8), ("29500.50", 6)]}

    def row(step: int, action: str, side: str, price: str, size: int) -> list[str]:
        cells = [str(base + step * 1_000_000), str(base + step * 1_000_000 - 100_000), "10", "1", "42",
                 action, side, "0", fp(price), str(size), "130", "50000", str(step)]
        for level in range(10):
            bid = book["bid"][level] if level < len(book["bid"]) else None
            ask = book["ask"][level] if level < len(book["ask"]) else None
            cells += [fp(bid[0]) if bid else undef, fp(ask[0]) if ask else undef,
                      str(bid[1]) if bid else "0", str(ask[1]) if ask else "0", "1", "1"]
        return cells + ["MNQU5"]

    rows = [row(1, "M", "B", "29500.00", 10)]
    rows.append(row(2, "T", "B", "29500.25", 2))                       # a buyer lifts the ask
    book["ask"][0] = ("29500.25", 6)
    rows.append(row(3, "M", "A", "29500.25", 2))
    rows.append(row(4, "T", "A", "29500.00", 3))                       # a seller hits the bid
    book["bid"][0] = ("29500.00", 7)
    rows.append(row(5, "M", "B", "29500.00", 3))
    rows.append(row(6, "T", "B", "29500.25", 6))                       # the ask level is eaten
    book["ask"] = [("29500.50", 6)]
    rows.append(row(7, "C", "A", "29500.25", 6))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def _selftest() -> int:
    from app.market.state import MarketState
    from app.research.replay_loader import stream_session_events_with_stats

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        sample = _write_sample(root / "sample.mbp-10.csv")
        results = import_many([sample], ImportOptions(out_root=root / "raw"))
        for result in results:
            _print_result(result)
        imported = [r for r in results if r.status == "imported"]
        if len(imported) != 1 or imported[0].session_dir is None:
            print("RESULT: FAIL - the sample did not import.")
            return 1
        events, stats = stream_session_events_with_stats(imported[0].session_dir)
        state, trades = MarketState(), 0
        for event in events:
            state = state.update(event.payload)
            trades += event.kind == "trade"
        print(f"replayed: {stats.ordering_mode} ordering, continuity_ok={stats.continuity_ok}, "
              f"{trades} trades, best bid/ask {state.best_bid}/{state.best_ask}, CVD {state.cumulative_volume_delta}")
        ok = (
            trades == 3 and stats.continuity_ok and stats.ordering_mode == "receive_sequence"
            and imported[0].replay_ok is True and imported[0].training_ok is False
            and str(state.best_ask) == "29500.50"
        )
        print("RESULT: PASS - the importer reads, replays and gates a Databento file correctly."
              if ok else "RESULT: FAIL - see above.")
        return 0 if ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="files, folders or .zip files to import")
    parser.add_argument("--out", type=Path, default=Path("data/raw"), help="recordings folder (default data/raw)")
    parser.add_argument("--allow-model-training", action="store_true",
                        help="let machine learning train on these sessions (default: replay and analysis only)")
    parser.add_argument("--instrument-id", type=int, help="import this instrument instead of the busiest contract")
    parser.add_argument("--symbol", default="MNQ", help="product family to accept (default MNQ)")
    parser.add_argument("--selftest", action="store_true", help="prove the path with a synthetic sample")
    parser.add_argument("--quiet", action="store_true", help="print results only")
    legacy = parser.add_argument_group("older options, still accepted")
    legacy.add_argument("--depth", type=Path, help="same as passing the file")
    legacy.add_argument("--trades", type=Path, help="ignored: the order-book file already contains every trade")
    legacy.add_argument("--batch", type=Path, help="same as passing the folder")
    args = parser.parse_args(argv)

    if args.selftest:
        return _selftest()
    paths = list(args.paths) + [p for p in (args.depth, args.batch) if p]
    if args.trades:
        print("note: --trades is ignored; the order-book (MBP) file already contains every trade.")
    if not paths:
        parser.error("give at least one file or folder (or use --selftest)")
    options = ImportOptions(
        out_root=args.out, symbol_family=args.symbol, instrument_id=args.instrument_id,
        allow_model_training=args.allow_model_training,
    )
    return run(paths, options, quiet=args.quiet)


if __name__ == "__main__":
    raise SystemExit(main())
