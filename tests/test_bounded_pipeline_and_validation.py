"""Tests: bounded capture pipeline (measured, no silent loss) and walk-forward validation."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.database.recorder import MarketSessionRecorder
from app.market.bounded_pipeline import BoundedIntakeBuffer, PipelineStateHolder, RecorderPipeline
from app.research.validation import evaluate_walk_forward, split_days, walk_forward_folds


class _FakeConnection:
    def __init__(self, frames: list[str]) -> None:
        self.frames = frames

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for frame in self.frames:
            yield frame


def test_intake_buffer_overflow_is_loud_never_silent() -> None:
    async def run() -> tuple[list[str], BoundedIntakeBuffer]:
        frames = [json.dumps({"n": i}) for i in range(10)]
        buffer = BoundedIntakeBuffer(_FakeConnection(frames), capacity=4)
        await buffer.pump()  # fill without a consumer -> overflow must be explicit
        out: list[str] = []
        async for item in buffer:
            out.append(str(item))
        return out, buffer

    out, buffer = asyncio.run(run())
    assert buffer.metrics.ingress == 10
    assert buffer.metrics.overflow > 0
    gap_frames = [o for o in out if "data_gap" in o and "intake queue overflow" in o]
    assert gap_frames, "overflow must inject an explicit data_gap frame"
    assert buffer.metrics.high_water <= 4  # capacity respected


def test_intake_buffer_passthrough_when_consumer_keeps_up() -> None:
    async def run() -> list[str]:
        frames = [json.dumps({"n": i}) for i in range(50)]
        buffer = BoundedIntakeBuffer(_FakeConnection(frames), capacity=100)
        task = asyncio.get_running_loop().create_task(buffer.pump())
        out = [str(item) async for item in buffer]
        await task
        return out

    out = asyncio.run(run())
    assert len(out) == 50  # nothing lost, no gaps


def _recorder(tmp_path: Path) -> MarketSessionRecorder:
    rec = MarketSessionRecorder(root_dir=tmp_path, session_start_utc=datetime(2026, 7, 15, 0, 22, tzinfo=UTC))
    rec.record_control_event({"type": "delayed_mode", "timestamp_ns": 1, "delay_minutes": 15})
    return rec


def test_recorder_pipeline_persists_everything_and_measures(tmp_path: Path) -> None:
    pipeline = RecorderPipeline(_recorder(tmp_path), capacity=10_000, batch_size=100)
    base = 1_752_537_751_000_000_000
    for i in range(2_000):
        pipeline.record({"type": "depth_update", "timestamp": base + i, "symbol": "MNQ", "side": "bid",
                         "price": "29500.00", "previous_size": "0", "new_size": "5"})
    pipeline.finalize(clean_shutdown=True)  # drains first
    import pyarrow.parquet as pq

    assert pq.ParquetFile(pipeline.depth_path).metadata.num_rows == 2_000  # no loss on drain
    snap = pipeline.metrics_snapshot()
    assert snap.recorder["ingress"] == 2_000 and snap.recorder["egress"] == 2_000
    assert snap.recorder["overflow"] == 0
    assert snap.last_batch_size > 0
    assert snap.last_flush_duration_ms >= 0.0
    assert snap.flush_failures == 0
    assert snap.recorder["high_water"] > 0


def test_recorder_pipeline_overflow_counts_on_manifest(tmp_path: Path) -> None:
    real = _recorder(tmp_path)
    pipeline = RecorderPipeline(real, capacity=1, batch_size=1)
    pipeline._stop.set()  # freeze the writer so the queue can only overflow
    pipeline._thread.join(timeout=5)
    pipeline.record({"type": "depth_update", "timestamp": 1, "symbol": "MNQ", "side": "bid",
                     "price": "29500.00", "previous_size": "0", "new_size": "5"})
    pipeline.record({"type": "depth_update", "timestamp": 2, "symbol": "MNQ", "side": "bid",
                     "price": "29500.00", "previous_size": "0", "new_size": "5"})
    assert pipeline.metrics.overflow == 1
    assert real.rejected_event_count == 1  # loss reaches the session manifest, never silent


def test_pipeline_state_holder_feeds_research_throttling(tmp_path: Path) -> None:
    holder = PipelineStateHolder()
    assert holder.worst_queue_occupancy_fraction() == 0.0  # no connection yet
    pipeline = RecorderPipeline(_recorder(tmp_path), capacity=10, batch_size=100)
    pipeline._stop.set()
    pipeline._thread.join(timeout=5)
    for i in range(9):
        pipeline.record({"type": "depth_update", "timestamp": i + 1, "symbol": "MNQ", "side": "bid",
                         "price": "29500.00", "previous_size": "0", "new_size": "5"})
    holder.attach(None, pipeline)
    assert holder.worst_queue_occupancy_fraction() >= 0.9  # real measured pressure


# --- validation ---------------------------------------------------------------


def test_split_days_never_splits_a_day_and_is_chronological() -> None:
    days = [f"2026-07-{d:02d}" for d in range(1, 11)]
    split = split_days(days)
    assert len(split.train_days) == 6 and len(split.validation_days) == 2 and len(split.test_days) == 2
    assert max(split.train_days) < min(split.validation_days) < min(split.test_days)
    all_days = set(split.train_days) | set(split.validation_days) | set(split.test_days)
    assert all_days == set(days)  # every day lands in exactly one partition


def test_walk_forward_folds_are_expanding_and_test_only_later_days() -> None:
    days = [f"2026-07-{d:02d}" for d in range(1, 11)]
    folds = walk_forward_folds(days, folds=3)
    assert len(folds) == 3
    for fold in folds:
        assert fold.train_days and fold.test_days
        assert max(fold.train_days) < min(fold.test_days)  # strictly out-of-sample
    assert len(folds[2].train_days) > len(folds[0].train_days)  # expanding window


def test_walk_forward_with_zero_outcomes_is_insufficient_never_a_pass() -> None:
    report = evaluate_walk_forward([])
    assert report.sufficient is False
    assert report.all_folds_positive is False
    assert report.total_test_trades == 0


def test_walk_forward_evaluates_costs_included_expectancy(tmp_path: Path) -> None:
    from tests.test_profitability_progress import _outcome

    outcomes = [
        _outcome(i, day=f"2026-07-{1 + (i % 10):02d}", direction="long" if i % 2 else "short", won=(i % 4) != 0)
        for i in range(60)
    ]
    report = evaluate_walk_forward(outcomes, folds=3)
    assert report.sufficient is True
    assert report.total_test_trades > 0
    for fold in report.folds:
        assert isinstance(fold.net_pnl_per_contract, Decimal)  # Decimal money math


def test_progress_ladder_includes_walk_forward_and_demo_soak_gates() -> None:
    from app.research.profitability_progress import compute_progress

    progress = compute_progress(catalog=[], outcomes=[], ledger=None)
    ids = [g.gate_id for g in progress.gates]
    assert "walk_forward" in ids and "demo_soak" in ids
    by_id = {g.gate_id: g for g in progress.gates}
    assert by_id["walk_forward"].status == "insufficient_evidence"  # zero outcomes never pass
    assert by_id["demo_soak"].status == "insufficient_evidence"
