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


# --- named defect regressions -------------------------------------------------


def test_gap_marker_is_never_counted_as_lost_market_data() -> None:
    """A synthetic gap marker displaced from the queue is not market-data loss."""
    async def run() -> BoundedIntakeBuffer:
        frames = [json.dumps({"n": i}) for i in range(30)]
        buffer = BoundedIntakeBuffer(_FakeConnection(frames), capacity=4)
        await buffer.pump()
        out = [str(item) async for item in buffer]
        # Real frames delivered + real frames lost must equal frames sent. If a
        # marker were miscounted as lost, this identity would break.
        real_out = [o for o in out if "data_gap" not in o]
        assert len(real_out) + buffer.metrics.overflow == 30
        return buffer

    buffer = asyncio.run(run())
    assert buffer.metrics.overflow > 0  # loss really happened and was counted


def test_one_incoming_frame_evicts_at_most_one_queued_frame() -> None:
    """The gap marker must not cost a second real event per overflow."""
    async def run() -> tuple[int, int]:
        frames = [json.dumps({"n": i}) for i in range(20)]
        buffer = BoundedIntakeBuffer(_FakeConnection(frames), capacity=5)
        await buffer.pump()
        out = [str(item) async for item in buffer]
        return len([o for o in out if "data_gap" not in o]), buffer.metrics.overflow

    delivered, lost = asyncio.run(run())
    # 20 frames into a capacity-5 queue: exactly 15 displaced, one per overflow.
    assert delivered == 5
    assert lost == 15
    assert delivered + lost == 20


def test_overflow_emits_a_coalesced_gap_marker_with_running_total() -> None:
    """Loss stays loud: a marker carrying the running lost count is delivered."""
    async def run() -> list[str]:
        frames = [json.dumps({"n": i}) for i in range(12)]
        buffer = BoundedIntakeBuffer(_FakeConnection(frames), capacity=3)
        await buffer.pump()
        return [str(item) async for item in buffer]

    out = asyncio.run(run())
    gaps = [json.loads(o) for o in out if "data_gap" in o]
    assert gaps, "overflow must still announce an explicit data_gap"
    assert gaps[-1]["receiver_intake_lost"] > 0
    assert gaps[-1]["reason"] == "receiver intake queue overflow"


class _FlakyRecorder:
    """Recorder whose write fails for one specific event."""

    def __init__(self, real, bad_timestamp: int) -> None:
        self._real = real
        self._bad = bad_timestamp
        self.rejected: list[str] = []

    def record(self, event):
        if int(event.get("timestamp", 0)) == self._bad:
            raise OSError("disk write failed")
        return self._real.record(event)

    def note_rejected_event(self, reason: str) -> None:
        self.rejected.append(reason)

    def finalize(self, *, clean_shutdown: bool, reason=None):
        self.clean_shutdown = clean_shutdown
        self.finalize_reason = reason
        return self._real.finalize(clean_shutdown=clean_shutdown, reason=reason)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_one_bad_event_does_not_discard_the_whole_batch(tmp_path: Path) -> None:
    """The old code lost every queued event on one exception; now only the bad one."""
    flaky = _FlakyRecorder(_recorder(tmp_path), bad_timestamp=5)
    pipeline = RecorderPipeline(flaky, capacity=1000, batch_size=100)
    for i in range(10):
        pipeline.record({"type": "depth_update", "timestamp": i, "symbol": "MNQ", "side": "bid",
                         "price": "29500.00", "previous_size": "0", "new_size": "5"})
    pipeline.finalize(clean_shutdown=True)
    import pyarrow.parquet as pq

    # 9 of 10 persisted; only the poisoned event was lost.
    assert pq.ParquetFile(flaky.depth_path).metadata.num_rows == 9
    assert pipeline.lost_events == 1
    assert pipeline.flush_failures >= 1


def test_write_failure_propagates_to_manifest_and_fails_closed(tmp_path: Path) -> None:
    """A lost event must reach the manifest and force an UNCLEAN finalize."""
    flaky = _FlakyRecorder(_recorder(tmp_path), bad_timestamp=3)
    pipeline = RecorderPipeline(flaky, capacity=1000, batch_size=10)
    for i in range(6):
        pipeline.record({"type": "depth_update", "timestamp": i, "symbol": "MNQ", "side": "bid",
                         "price": "29500.00", "previous_size": "0", "new_size": "5"})
    pipeline.finalize(clean_shutdown=True)  # caller asks for clean...
    assert pipeline.fail_closed is True
    assert flaky.clean_shutdown is False, "a segment that lost events can never be clean"
    assert "lost" in (flaky.finalize_reason or "")
    assert any("write failed" in r for r in flaky.rejected), "loss must reach the manifest"


def test_session_ended_drains_the_pipeline_before_finalizing(tmp_path) -> None:
    """The session tail must not die when session_ended arrives.

    Real bug: session_ended finalized the underlying recorder while the
    pipeline queue still held the session's last events; the writer then
    failed every one with "cannot record after finalization". At the real
    ~1,331 ev/s this silently cost the final seconds of every session.
    """
    from app.database.recorder import MarketSessionRecorder
    from app.market.bounded_pipeline import RecorderPipeline

    recorder = MarketSessionRecorder(root_dir=tmp_path)
    pipeline = RecorderPipeline(recorder, capacity=10_000, batch_size=50)
    base = 1_752_537_751_000_000_000
    for i in range(1_200):  # enough to guarantee a queued backlog
        pipeline.record({"type": "depth_update", "timestamp": base + i * 1_000_000,
                         "symbol": "MNQ", "side": "bid", "price": "29500.00",
                         "previous_size": "0", "new_size": "1"})
    # The finalizing control event arrives while the queue is still busy.
    pipeline.record_control_event({"type": "session_ended", "timestamp_ns": base + 2_000_000_000,
                                   "source_mode": "delayed", "reason": "clean shutdown"})
    assert recorder.depth_updates == 1_200, "every queued event must persist BEFORE finalization"
    assert recorder.finalized is True
    assert pipeline.metrics.overflow == 0
    assert pipeline.lost_events == 0
