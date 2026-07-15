"""Tests for the automatic, idempotent post-finalization build orchestrator (STAGE 4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.database.recorder import MarketSessionRecorder
from app.research.build_orchestrator import (
    OUTCOME_BUILT,
    OUTCOME_SKIPPED_ACTIVE,
    OUTCOME_SKIPPED_UP_TO_DATE,
    build_finalized_session,
    needs_build,
    run_pending_builds,
)
from app.research.session_catalog import build_catalog


def _finalized_session(raw_root: Path, minute: int = 22) -> None:
    rec = MarketSessionRecorder(root_dir=raw_root, session_start_utc=datetime(2026, 7, 15, 0, minute, tzinfo=UTC))
    rec.record_control_event({"type": "delayed_mode", "timestamp_ns": 1, "delay_minutes": 15})
    base = 1_752_537_751_000_000_000
    price = Decimal("29500.00")
    for i in range(200):
        price += Decimal("0.25") if i % 2 == 0 else Decimal("-0.25")
        rec.record({"type": "depth_update", "timestamp": base + i * 1_000_000, "symbol": "MNQ",
                    "side": "bid" if i % 2 else "ask", "price": f"{price:.2f}",
                    "previous_size": "0", "new_size": str(i % 40 + 1)})
        rec.record({"timestamp_ns": base + i * 1_000_000 + 1, "price": f"{price:.2f}", "size": "1",
                    "aggressor_side": "buy" if i % 2 else "sell", "instrument": "MNQ", "sequence_id": i + 1})
    rec.finalize(clean_shutdown=True)


def _active_session(raw_root: Path) -> None:
    rec = MarketSessionRecorder(root_dir=raw_root, session_start_utc=datetime(2026, 7, 15, 3, 0, tzinfo=UTC))
    rec.record_control_event({"type": "delayed_mode", "timestamp_ns": 1, "delay_minutes": 15})
    rec.record({"type": "depth_update", "timestamp": 10, "symbol": "MNQ", "side": "bid",
                "price": "29500.00", "previous_size": "0", "new_size": "5"})
    rec.flush()  # streamed, never finalized -> active


def test_first_run_builds_then_second_run_is_idempotent(tmp_path: Path) -> None:
    raw, processed, labels = tmp_path / "raw", tmp_path / "processed", tmp_path / "labels"
    _finalized_session(raw)

    first = run_pending_builds(raw, processed, labels)
    built = [s for s in first if s.outcome == OUTCOME_BUILT]
    assert len(built) == 1
    assert (processed / f"{built[0].session_id}.build.json").exists()

    # Nothing changed -> the second run must skip, not rebuild.
    second = run_pending_builds(raw, processed, labels)
    assert all(s.outcome == OUTCOME_SKIPPED_UP_TO_DATE for s in second if s.session_id == built[0].session_id)


def test_active_session_is_never_built(tmp_path: Path) -> None:
    raw, processed, labels = tmp_path / "raw", tmp_path / "processed", tmp_path / "labels"
    _finalized_session(raw)
    _active_session(raw)
    statuses = run_pending_builds(raw, processed, labels)
    active = [s for s in statuses if s.outcome == OUTCOME_SKIPPED_ACTIVE]
    assert active, "the active recording must be skipped, never opened"


def test_builder_version_change_forces_rebuild(tmp_path: Path) -> None:
    raw, processed, labels = tmp_path / "raw", tmp_path / "processed", tmp_path / "labels"
    _finalized_session(raw)
    run_pending_builds(raw, processed, labels)
    entry = next(e for e in build_catalog(raw) if not e.active and e.eligible_for_analysis)

    # Simulate a stale prior build from an older builder version.
    summary_path = processed / f"{entry.session_id}.build.json"
    prior = json.loads(summary_path.read_text(encoding="utf-8"))
    prior["builder_version"] = "real-episodes-v0"
    summary_path.write_text(json.dumps(prior), encoding="utf-8")

    required, reason = needs_build(entry.session_id, entry.manifest_path.parent, processed)
    assert required and "builder version" in reason
    status = build_finalized_session(entry, processed_root=processed, labels_root=labels)
    assert status.outcome == OUTCOME_BUILT


def test_source_data_change_forces_rebuild(tmp_path: Path) -> None:
    raw, processed, labels = tmp_path / "raw", tmp_path / "processed", tmp_path / "labels"
    _finalized_session(raw)
    run_pending_builds(raw, processed, labels)
    entry = next(e for e in build_catalog(raw) if not e.active and e.eligible_for_analysis)

    summary_path = processed / f"{entry.session_id}.build.json"
    prior = json.loads(summary_path.read_text(encoding="utf-8"))
    prior["source_file_hashes"] = {"depth.parquet": "deadbeef"}
    summary_path.write_text(json.dumps(prior), encoding="utf-8")

    required, reason = needs_build(entry.session_id, entry.manifest_path.parent, processed)
    assert required and "source data" in reason
