"""The salvage audit must sort recorded sessions into honest dispositions."""

from __future__ import annotations

from pathlib import Path

from app.research.session_catalog import SessionEntry
from tools.salvage_audit import (
    ACTIVE,
    DEAD_DEPTH_ONLY,
    DEAD_NO_DEPTH,
    ELIGIBLE,
    REPROCESS,
    RERECORD_BRIDGE,
    RERECORD_OVERFLOW,
    RERECORD_PROVENANCE,
    RERECORD_UNCLEAN,
    audit,
    categorize_entry,
)


def _entry(**over: object) -> SessionEntry:
    """A finalized, real, analysis-eligible session; override to shape each case."""
    base: dict[str, object] = dict(
        session_id="s", manifest_path=Path("m"), provenance="REAL_DELAYED",
        is_delayed=True, finalized=True, active=False, continuity_status="continuous",
        depth_updates=1000, trades=100, dropped_message_count=0, malformed_event_count=0,
        rejected_event_count=0, missed_trade_event_count=0, utc_start="2026-08-01T00:00:00",
        utc_end="2026-08-01T01:00:00", eligible_for_analysis=True,
        eligible_for_order_flow_replay=False, eligible_for_model_training=False,
        valid_for_live_decisions=False, reasons=(), model_training_reasons=(),
    )
    base.update(over)
    return SessionEntry(**base)  # type: ignore[arg-type]


def test_eligible_and_dead_and_active_dispositions() -> None:
    assert categorize_entry(_entry(eligible_for_model_training=True)) == ELIGIBLE
    assert categorize_entry(_entry(finalized=False, active=True)) == ACTIVE
    assert categorize_entry(_entry(depth_updates=0)) == DEAD_NO_DEPTH
    assert categorize_entry(_entry(trades=0)) == DEAD_DEPTH_ONLY


def test_zero_size_artifact_is_a_reprocess_candidate() -> None:
    """Clean + continuous + has trades, blocked only by zero-size trades -> REPROCESS."""
    entry = _entry(
        eligible_for_analysis=True,
        eligible_for_order_flow_replay=False,
        malformed_event_count=393_265,
        missed_trade_event_count=17_000,
        reasons=("393265 market event(s) could not be parsed", "12 trade-sequence gap(s)"),
    )
    assert categorize_entry(entry) == REPROCESS


def test_real_loss_is_not_a_reprocess_candidate() -> None:
    """Bounded-queue overflow / drops are real loss, not a benign artifact."""
    entry = _entry(
        eligible_for_order_flow_replay=False,
        dropped_message_count=5_000,
        reasons=("bounded queue overflow dropped 5000",),
    )
    assert categorize_entry(entry) == RERECORD_OVERFLOW


def test_unclean_and_provenance_and_bridge_need_rerecord() -> None:
    assert categorize_entry(_entry(
        eligible_for_analysis=False, continuity_status="receiver_error",
        reasons=("unclean shutdown",))) == RERECORD_UNCLEAN
    assert categorize_entry(_entry(
        eligible_for_analysis=False,
        reasons=("provenance UNKNOWN: source mode was never declared",))) == RERECORD_PROVENANCE
    # order-flow clean but the model gate is blocked by an old bridge -> re-record
    assert categorize_entry(_entry(
        eligible_for_order_flow_replay=True,
        model_training_reasons=("model training requires accepted bridge protocol 1.2 or newer",),
    )) == RERECORD_BRIDGE


def test_audit_counts_and_ranks_reprocess_candidates() -> None:
    entries = [
        _entry(session_id="eligible", eligible_for_model_training=True),
        _entry(session_id="rich", eligible_for_order_flow_replay=False,
               trades=123_000, malformed_event_count=9, reasons=("malformed",)),
        _entry(session_id="small", eligible_for_order_flow_replay=False,
               trades=1_200, malformed_event_count=3, reasons=("malformed",)),
        _entry(session_id="depth_only", trades=0),
    ]
    report = audit(entries)
    assert report.total == 4
    assert report.eligible_now == 1
    assert report.reprocessable == 2
    assert report.dead == 1
    assert report.reprocess_trades == 124_200
    # richest candidate is listed first
    assert report.reprocess_examples[0] == ("rich", 123_000)
