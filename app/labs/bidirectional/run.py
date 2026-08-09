"""CLI runner for the Bidirectional Paper Trading Lab (paper-only, read-only data).

Replay a recorded session through the lab with one or more paired activation
levels and print honest statistics. Nothing here can place a real order.

    python -m app.labs.bidirectional.run data/raw/2026-08-05/session_20260805T154846Z \\
        --level 20000 --long 100 --short 100 --stop-ticks 2 \\
        --be-trigger 3 --trail-act 5 --trail-dist 2 --max-events 500000
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app.labs.bidirectional.config import (  # noqa: E402
    AccountConfig,
    ActivationSpec,
    BreakEvenConfig,
    TrailConfig,
)
from app.labs.bidirectional.engine import LabEngine  # noqa: E402
from app.labs.bidirectional.feed import replay_market_events  # noqa: E402
from app.labs.bidirectional.statistics import compute_statistics  # noqa: E402


def _fmt(value: object) -> str:
    if isinstance(value, Decimal):
        return f"{value:,.2f}"
    return str(value)


def main(argv: list[str] | None = None) -> int:
    """Replay one recorded session through the lab and print statistics."""
    parser = argparse.ArgumentParser(description="Bidirectional Paper Trading Lab replay (paper-only).")
    parser.add_argument("session_dir", type=Path, help="a recorded session directory under data/raw/")
    parser.add_argument("--level", type=Decimal, action="append", default=[],
                        help="paired activation price (repeatable)")
    parser.add_argument("--long", type=int, default=100)
    parser.add_argument("--short", type=int, default=100)
    parser.add_argument("--stop-ticks", type=Decimal, default=Decimal("2"))
    parser.add_argument("--be-trigger", type=Decimal, default=Decimal("0"))
    parser.add_argument("--be-offset", type=Decimal, default=Decimal("0"))
    parser.add_argument("--trail-act", type=Decimal, default=Decimal("0"))
    parser.add_argument("--trail-dist", type=Decimal, default=Decimal("0"))
    parser.add_argument("--tick-size", type=Decimal, default=Decimal("0.25"))
    parser.add_argument("--tick-value", type=Decimal, default=Decimal("0.50"))
    parser.add_argument("--commission", type=Decimal, default=Decimal("0.62"))
    parser.add_argument("--stop-slippage-ticks", type=Decimal, default=Decimal("1"))
    parser.add_argument("--max-events", type=int, default=None)
    parser.add_argument("--one-shot", action="store_true", help="each level fires once (default: repeats)")
    parser.add_argument("--log-tail", type=int, default=20)
    args = parser.parse_args(argv)

    account = AccountConfig(
        tick_size=args.tick_size, tick_value=args.tick_value,
        commission_per_contract=args.commission, stop_slippage_ticks=args.stop_slippage_ticks,
        max_gross_contracts=max(1, args.long + args.short) * max(1, len(args.level) or 1) * 4,
    )
    engine = LabEngine(account)
    be = BreakEvenConfig(enabled=args.be_trigger > 0, trigger_ticks=args.be_trigger, offset_ticks=args.be_offset)
    tr = TrailConfig(enabled=args.trail_dist > 0, activation_ticks=args.trail_act, distance_ticks=args.trail_dist)

    # Arm the levels once the first event has primed the market (so auto-direction
    # resolves correctly). We prime with the first event, then arm, then replay on.
    events = replay_market_events(args.session_dir, max_events=args.max_events)
    first = next(events, None)
    if first is None:
        print(f"No market events found in {args.session_dir}", file=sys.stderr)
        return 2
    engine.on_event(first)
    for i, price in enumerate(args.level):
        engine.arm(ActivationSpec(
            activation_id=str(i + 1), price=price, long_qty=args.long, short_qty=args.short,
            stop_ticks=args.stop_ticks, break_even=be, trailing=tr,
            one_shot=args.one_shot, max_activations=1 if args.one_shot else 1_000_000,
            require_leave_reenter=True, leave_distance_ticks=args.stop_ticks * 2,
        ))
    saw_book = first.bid is not None and first.ask is not None
    for ev in events:
        engine.on_event(ev)
        saw_book = saw_book or (ev.bid is not None and ev.ask is not None)

    if not saw_book:
        print("WARNING: no bid/ask in this data; tight-stop and activation simulation "
              "may be unreliable because the exact intrabar price sequence is unavailable.\n")

    stats = compute_statistics(engine)
    print(f"Bidirectional Paper Trading Lab  -  {args.session_dir.name}")
    print(f"events: {stats['account']['events_seen']:,}   levels: {[str(p) for p in args.level]}\n")
    for section in ("account", "general", "paired", "activation"):
        print(f"[{section}]")
        for key, value in stats[section].items():  # type: ignore[index]
            print(f"  {key:<26} {_fmt(value)}")
        print()
    if args.log_tail:
        print(f"[event log · last {args.log_tail}]")
        for entry in engine.log[-args.log_tail:]:
            print(f"  {entry.ts_ns}  {entry.text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
