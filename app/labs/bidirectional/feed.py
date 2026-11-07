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


def first_trade_prices(
    session_dir: Path, *, limit: int = 600, max_files: int = 4,
) -> list[Decimal]:
    """Return up to ``limit`` of a session's earliest trade prices (read-only, cheap).

    Opens at most ``max_files`` trade files however large the session is. That
    bound is the point: a session that has been recording for hours holds tens of
    thousands of part files, and the full replay path (``stream_session_events``)
    opens every one of their footers just to check the schema - minutes of work.
    A starting-price sample only needs the first few hundred trades, so read just
    those, never the depth stream, and never walk the whole session.
    """
    import pyarrow.parquet as pq

    from app.research.replay_loader import _stream_paths

    prices: list[Decimal] = []
    for path in _stream_paths(session_dir, "trades")[:max_files]:
        for batch in pq.ParquetFile(path).iter_batches(batch_size=limit, columns=["price"]):
            for value in batch.column("price").to_pylist():
                try:
                    prices.append(Decimal(str(value)))
                except (InvalidOperation, TypeError):
                    continue  # a malformed recorded row is skipped, never fatal
                if len(prices) >= limit:
                    return prices
    return prices


def recorded_session_dirs(raw_root: Path) -> list[Path]:
    """Return recorded session directories under ``raw_root`` (read-only listing)."""
    if not raw_root.is_dir():
        return []
    return sorted(p.parent for p in raw_root.glob("*/*/session_manifest.json"))
