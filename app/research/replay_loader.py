"""Streaming, bounded-memory replay of recorded Bookmap sessions.

New recordings contain a receiver-local ``receive_sequence`` on both depth
and trade rows.  That sequence is the causal replay key.  Historical files
without it are merged by exchange timestamp, depth before trade on a tie;
every cross-stream timestamp collision is reported as ambiguous provenance.

The loader reads only PyArrow batches.  It accepts finalized compatibility
files or atomically closed part directories and never materializes a whole
session in memory.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

DEFAULT_BATCH_ROWS = 8192


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    """One replayed market event with its causal metadata."""

    timestamp_ns: int
    kind: str
    payload: dict[str, object]
    receive_sequence: int | None = None


@dataclass(slots=True)
class ReplayStats:
    """Data-quality and ordering counters populated as replay is consumed."""

    depth_events: int = 0
    trade_events: int = 0
    same_timestamp_collisions: int = 0
    timestamp_regressions: int = 0
    trade_sequence_gaps: int = 0
    missed_trade_events: int = 0
    nonmonotonic_trade_sequences: int = 0
    receive_sequence_gaps: int = 0
    missed_receive_events: int = 0
    nonmonotonic_receive_sequences: int = 0
    ordering_mode: str = "timestamp_fallback"
    receive_order_available: bool = False

    @property
    def ordering_ambiguous(self) -> bool:
        """Return whether old timestamp-only data has cross-stream ties."""
        return not self.receive_order_available and self.same_timestamp_collisions > 0

    @property
    def continuity_ok(self) -> bool:
        """Return whether replay detected no missing or regressing event IDs."""
        return (
            self.timestamp_regressions == 0
            and self.trade_sequence_gaps == 0
            and self.nonmonotonic_trade_sequences == 0
            and self.receive_sequence_gaps == 0
            and self.nonmonotonic_receive_sequences == 0
        )


def _stream_paths(session_dir: Path, stem: str) -> tuple[Path, ...]:
    finalized = session_dir / f"{stem}.parquet"
    if finalized.is_file():
        return (finalized,)
    parts_name = "trade_parts" if stem == "trades" else "depth_parts"
    parts_dir = session_dir / parts_name
    if not parts_dir.is_dir():
        return ()
    return tuple(sorted(parts_dir.glob("part-*.parquet")))


def _has_receive_order(paths: Sequence[Path]) -> bool:
    return bool(paths) and all(
        "receive_sequence" in pq.ParquetFile(path).schema_arrow.names
        for path in paths
    )


def _iter_rows(
    paths: Sequence[Path],
    timestamp_key: str,
    kind: str,
    batch_rows: int,
) -> Iterator[ReplayEvent]:
    for path in paths:
        parquet = pq.ParquetFile(path)
        for batch in parquet.iter_batches(batch_size=batch_rows):
            for raw_row in batch.to_pylist():
                row = dict(raw_row)
                receive_value = row.get("receive_sequence")
                yield ReplayEvent(
                    timestamp_ns=int(row[timestamp_key]),
                    kind=kind,
                    payload=row,
                    receive_sequence=(int(receive_value) if receive_value is not None else None),
                )


def _event_key(event: ReplayEvent, use_receive_order: bool) -> tuple[int, int]:
    if use_receive_order and event.receive_sequence is not None:
        return event.receive_sequence, 0 if event.kind == "depth" else 1
    return event.timestamp_ns, 0 if event.kind == "depth" else 1


def stream_session_events(
    session_dir: Path,
    *,
    batch_rows: int = DEFAULT_BATCH_ROWS,
) -> Iterator[ReplayEvent]:
    """Yield one session in causal order using at most one batch per stream."""
    if batch_rows <= 0:
        raise ValueError("batch_rows must be positive")
    depth_paths = _stream_paths(session_dir, "depth")
    trade_paths = _stream_paths(session_dir, "trades")
    use_receive_order = _has_receive_order(depth_paths) and _has_receive_order(trade_paths)

    depth = _iter_rows(depth_paths, "timestamp", "depth", batch_rows)
    trades = _iter_rows(trade_paths, "timestamp_ns", "trade", batch_rows)
    depth_next = next(depth, None)
    trade_next = next(trades, None)
    while depth_next is not None or trade_next is not None:
        if trade_next is None:
            assert depth_next is not None
            yield depth_next
            depth_next = next(depth, None)
        elif depth_next is None:
            yield trade_next
            trade_next = next(trades, None)
        elif _event_key(depth_next, use_receive_order) <= _event_key(trade_next, use_receive_order):
            yield depth_next
            depth_next = next(depth, None)
        else:
            yield trade_next
            trade_next = next(trades, None)


def stream_session_events_with_stats(
    session_dir: Path,
    *,
    batch_rows: int = DEFAULT_BATCH_ROWS,
) -> tuple[Iterator[ReplayEvent], ReplayStats]:
    """Return a replay iterator plus counters completed when it is consumed."""
    depth_paths = _stream_paths(session_dir, "depth")
    trade_paths = _stream_paths(session_dir, "trades")
    has_receive_order = _has_receive_order(depth_paths) and _has_receive_order(trade_paths)
    stats = ReplayStats(
        ordering_mode="receive_sequence" if has_receive_order else "timestamp_fallback",
        receive_order_available=has_receive_order,
    )

    def _counting() -> Iterator[ReplayEvent]:
        last_timestamp: int | None = None
        last_trade_sequence: int | None = None
        last_receive_sequence: int | None = None
        collision_timestamp: int | None = None
        collision_kinds: set[str] = set()

        for event in stream_session_events(session_dir, batch_rows=batch_rows):
            if collision_timestamp is not None and event.timestamp_ns != collision_timestamp:
                if collision_kinds == {"depth", "trade"}:
                    stats.same_timestamp_collisions += 1
                collision_kinds.clear()
            collision_timestamp = event.timestamp_ns
            collision_kinds.add(event.kind)

            if last_timestamp is not None and event.timestamp_ns < last_timestamp:
                stats.timestamp_regressions += 1
            last_timestamp = event.timestamp_ns

            if event.kind == "depth":
                stats.depth_events += 1
            else:
                stats.trade_events += 1
                sequence_value = event.payload.get("sequence_id")
                if sequence_value is not None:
                    sequence_id = int(sequence_value)
                    if last_trade_sequence is not None:
                        if sequence_id > last_trade_sequence + 1:
                            stats.trade_sequence_gaps += 1
                            stats.missed_trade_events += sequence_id - last_trade_sequence - 1
                        elif sequence_id <= last_trade_sequence:
                            stats.nonmonotonic_trade_sequences += 1
                    last_trade_sequence = sequence_id

            if has_receive_order and event.receive_sequence is not None:
                if last_receive_sequence is not None:
                    if event.receive_sequence > last_receive_sequence + 1:
                        stats.receive_sequence_gaps += 1
                        stats.missed_receive_events += event.receive_sequence - last_receive_sequence - 1
                    elif event.receive_sequence <= last_receive_sequence:
                        stats.nonmonotonic_receive_sequences += 1
                last_receive_sequence = event.receive_sequence
            yield event

        if collision_timestamp is not None and collision_kinds == {"depth", "trade"}:
            stats.same_timestamp_collisions += 1

    return _counting(), stats


def session_stream_paths(session_dir: Path) -> tuple[Path, ...]:
    """Return the exact Parquet inputs selected for a session replay."""
    return _stream_paths(session_dir, "depth") + _stream_paths(session_dir, "trades")
