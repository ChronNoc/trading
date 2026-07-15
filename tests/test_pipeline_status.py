"""Tests for the nine-stage pipeline status backend (STAGE 3)."""

from __future__ import annotations

import json
from pathlib import Path

from app.research.pipeline_status import (
    STATUS_BLOCKED,
    STATUS_LOCKED,
    STATUS_NOT_STARTED,
    STATUS_READY,
    STATUS_RUNNING,
    PipelineInputs,
    compute_pipeline,
    load_build_aggregate,
)


def test_cold_start_shows_receiver_not_started_and_gates_locked() -> None:
    """Nothing running: stage 1 not started, demo/live gates always locked."""
    status = compute_pipeline(PipelineInputs())
    assert status.by_key("receiver_listening").status == STATUS_NOT_STARTED
    assert status.by_key("demo_gate").status == STATUS_LOCKED
    assert status.by_key("live_gate").status == STATUS_LOCKED
    assert "no orders are submitted" in status.by_key("demo_gate").detail.lower()


def test_listening_but_no_addon_blocks_connection_stage_with_action() -> None:
    """Receiver up, no add-on: connection blocked with a real Bookmap next-action."""
    status = compute_pipeline(PipelineInputs(receiver_listening=True))
    connected = status.by_key("bookmap_connected")
    assert connected.status == STATUS_BLOCKED
    assert "MNQ WebSocket Forwarder" in connected.next_action


def test_recording_healthy_when_connected_fresh_and_writing() -> None:
    """A connected, fresh, writing feed makes recording ready."""
    status = compute_pipeline(
        PipelineInputs(receiver_listening=True, bookmap_connected=True, recording=True, data_stale=False),
    )
    assert status.by_key("bookmap_connected").status == STATUS_RUNNING
    assert status.by_key("recording_healthy").status == STATUS_READY


def test_zero_completed_outcomes_is_honest_blocked_not_failed() -> None:
    """Zero completed setups reads as a valid blocked state with rejection reasons, not a claim."""
    status = compute_pipeline(
        PipelineInputs(
            receiver_listening=True, bookmap_connected=True, recording=True, data_stale=False,
            finalized_sessions=162, eligible_sessions=1, sessions_built=1, evaluations=20,
            accepted_setups=0, completed_outcomes=0,
            excluded_by_reason={"durable_defending_block": 20, "controlling_side_known": 20},
        ),
    )
    completed = status.by_key("completed_outcomes")
    assert completed.status == STATUS_BLOCKED
    assert "honest" in completed.blocker.lower()
    assert "durable_defending_block" in completed.detail
    # Validation gate must not be ready without evidence.
    assert status.by_key("validation_gate").status == STATUS_BLOCKED


def test_validation_ready_only_when_evidence_passes() -> None:
    """The validation gate is ready only when the evidence ladder passed."""
    status = compute_pipeline(PipelineInputs(completed_outcomes=150, validation_passed=True))
    assert status.by_key("validation_gate").status == STATUS_READY


def test_load_build_aggregate_sums_real_summaries(tmp_path: Path) -> None:
    """Aggregation sums evaluations/accepted/completed and merges rejection tallies."""
    (tmp_path / "a.build.json").write_text(json.dumps({
        "evaluations": 20, "accepted_candidates": 0, "completed": 0, "ledger_eligible": 0,
        "replay_quality": {"continuity_ok": False},
        "rejected_condition_tally": {"durable_defending_block": 20, "valid_stop_location": 20},
    }), encoding="utf-8")
    (tmp_path / "b.build.json").write_text(json.dumps({
        "evaluations": 12, "accepted_candidates": 2, "completed": 1, "ledger_eligible": 1,
        "replay_quality": {"continuity_ok": True},
        "rejected_condition_tally": {"durable_defending_block": 5},
    }), encoding="utf-8")
    agg = load_build_aggregate(tmp_path)
    assert agg.sessions_built == 2
    assert agg.evaluations == 32
    assert agg.accepted_setups == 2
    assert agg.completed_outcomes == 1
    assert agg.sessions_failing_continuity == 1
    assert agg.excluded_by_reason["durable_defending_block"] == 25
