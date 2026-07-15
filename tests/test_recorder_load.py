"""STAGE 2 transport/load tests: recorder reliability under high event rate.

These assert the recorder never silently drops events under a burst well above a
realistic MNQ depth+trade rate, keeps closed parts readable mid-recording, and
that clean shutdown is idempotent. Throughput is measured and asserted to clear a
conservative floor so a regression that reintroduces the old O(n^2) slowdown
fails loudly.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq

from app.database.recorder import MarketSessionRecorder

_EVENTS_PER_STREAM = 20_000  # 40k total; above typical sustained MNQ depth+trade rates


def _row_count(path: Path) -> int:
    return pq.ParquetFile(path).metadata.num_rows if path.is_file() else 0


def test_recorder_takes_high_burst_without_silent_drops(tmp_path: Path) -> None:
    """A 40k-event burst is fully persisted; nothing is silently discarded."""
    rec = MarketSessionRecorder(root_dir=tmp_path, session_start_utc=datetime(2026, 7, 15, 0, 22, tzinfo=UTC))
    rec.record_control_event({"type": "delayed_mode", "timestamp_ns": 1, "delay_minutes": 15})
    base = 1_752_537_751_000_000_000
    price = Decimal("29500.00")

    start = time.perf_counter()
    for i in range(_EVENTS_PER_STREAM):
        price += Decimal("0.25") if i % 2 == 0 else Decimal("-0.25")
        rec.record({"type": "depth_update", "timestamp": base + i * 1000, "symbol": "MNQ",
                    "side": "bid" if i % 2 else "ask", "price": f"{price:.2f}",
                    "previous_size": "0", "new_size": str(i % 50 + 1)})
        rec.record({"timestamp_ns": base + i * 1000 + 1, "price": f"{price:.2f}", "size": "1",
                    "aggressor_side": "buy" if i % 2 else "sell", "instrument": "MNQ", "sequence_id": i + 1})
    elapsed = time.perf_counter() - start
    throughput = (2 * _EVENTS_PER_STREAM) / elapsed if elapsed else float("inf")

    # Closed parts must be readable while recording continues (bounded-memory rotation).
    depth_parts = list(rec.depth_parts_dir.glob("part-*.parquet"))
    assert depth_parts, "expected readable closed depth parts during recording"
    assert _row_count(depth_parts[0]) > 0  # a mid-recording part is a valid parquet

    rec.finalize(clean_shutdown=True)
    assert _row_count(rec.depth_path) == _EVENTS_PER_STREAM
    assert _row_count(rec.trades_path) == _EVENTS_PER_STREAM
    # Conservative throughput floor - the old O(n^2) recorder fell far below this.
    assert throughput > 5_000, f"throughput too low: {throughput:.0f} events/s"


def test_finalize_is_idempotent(tmp_path: Path) -> None:
    """Calling finalize twice does not corrupt or re-merge the session."""
    rec = MarketSessionRecorder(root_dir=tmp_path, session_start_utc=datetime(2026, 7, 15, 0, 22, tzinfo=UTC))
    rec.record_control_event({"type": "delayed_mode", "timestamp_ns": 1, "delay_minutes": 15})
    base = 1_752_537_751_000_000_000
    for i in range(1000):
        rec.record({"type": "depth_update", "timestamp": base + i * 1000, "symbol": "MNQ", "side": "bid",
                    "price": "29500.00", "previous_size": "0", "new_size": "5"})
    rec.finalize(clean_shutdown=True)
    first = _row_count(rec.depth_path)
    rec.finalize(clean_shutdown=True)  # second call must be a no-op
    assert _row_count(rec.depth_path) == first == 1000


def test_data_gap_is_reported_not_swallowed(tmp_path: Path) -> None:
    """A bridge data_gap control event is recorded in continuity status, never hidden."""
    rec = MarketSessionRecorder(root_dir=tmp_path, session_start_utc=datetime(2026, 7, 15, 0, 22, tzinfo=UTC))
    rec.record_control_event({"type": "delayed_mode", "timestamp_ns": 1, "delay_minutes": 15})
    rec.record_control_event({"type": "data_gap", "timestamp_ns": 2, "reason": "bounded queue overflow",
                              "dropped_message_count": 7})
    rec.finalize(clean_shutdown=False, reason="ended with a reported gap")
    assert rec.dropped_message_count == 7
    assert "gap" in rec.continuity_status.lower() or rec.continuity_status == "ended with a reported gap"
