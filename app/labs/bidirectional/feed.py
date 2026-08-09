"""Read-only market feed for the lab: recorded session ticks -> MarketEvent.

The lab never touches the live feed. This adapter REPLAYS an already-recorded
session (the highest-resolution data available) purely by reading its Parquet
via the existing replay loader and reconstructing the top of book with the
existing immutable market-state helper. It only reads; it can never publish
back, change a subscription, or alter any runtime state. Kept out of the lab's
package ``__init__`` so the core engine stays a light, dependency-free import.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.labs.bidirectional.market import MarketEvent


def replay_market_events(session_dir: Path, *, max_events: int | None = None) -> Iterator[MarketEvent]:
    """Yield tick-resolution :class:`MarketEvent`s from a recorded session (read-only).

    ``bid``/``ask`` come from the reconstructed top of book; ``last`` is the trade
    price on trade events. Depth-only events carry the current book with no
    ``last``. Nothing here writes, and the source files are never modified.
    """
    from app.market.state import MarketState
    from app.research.replay_loader import stream_session_events

    state = MarketState()
    count = 0
    for event in stream_session_events(session_dir):
        if max_events is not None and count >= max_events:
            return
        try:
            state = state.update(event.payload)
        except Exception:  # noqa: BLE001 - a malformed recorded row is skipped, never fatal
            continue
        last: Decimal | None = None
        if event.kind == "trade":
            try:
                last = Decimal(str(event.payload.get("price")))
            except (InvalidOperation, TypeError):
                last = None
        yield MarketEvent(
            ts_ns=int(event.timestamp_ns),
            last=last,
            bid=state.best_bid,
            ask=state.best_ask,
            kind=event.kind,
        )
        count += 1


def recorded_session_dirs(raw_root: Path) -> list[Path]:
    """Return recorded session directories under ``raw_root`` (read-only listing)."""
    if not raw_root.is_dir():
        return []
    return sorted(p.parent for p in raw_root.glob("*/*/session_manifest.json"))
