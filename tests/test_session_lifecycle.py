"""One coordinated lifecycle: finalize -> build -> research -> ledgers -> restart."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.database.recorder import MarketSessionRecorder
from app.research.auto_research import canonical_candidate
from app.research.build_orchestrator import run_pending_builds
from app.research.research_service import ResearchService


def _record_and_finalize(raw_root: Path) -> str:
    rec = MarketSessionRecorder(root_dir=raw_root, session_start_utc=datetime(2026, 7, 15, 0, 22, tzinfo=UTC))
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
    return rec.session_dir.name


def test_finalize_build_research_ledger_restart_is_one_lifecycle(tmp_path: Path) -> None:
    """The full chain runs once, coordinates, and survives a restart intact."""
    raw = tmp_path / "raw"
    processed = tmp_path / "processed"
    labels = tmp_path / "labels"
    state = tmp_path / "state"
    session_id = _record_and_finalize(raw)

    # 7-9: finalized session -> idempotent build writes decisions/episodes/labels/status.
    statuses = run_pending_builds(raw, processed, labels)
    assert any(s.outcome == "built" and s.session_id == session_id for s in statuses)
    assert (processed / f"{session_id}.build.json").exists()
    assert (processed / f"{session_id}.decisions.jsonl").exists()
    assert (labels / f"{session_id}.labels.jsonl").exists()
    # Re-running the lifecycle does NOT rebuild (no duplicate builders on one session).
    second = run_pending_builds(raw, processed, labels)
    assert all(s.outcome != "built" for s in second if s.session_id == session_id)

    # 11-12: research discovers the finalized session and persists ledgers.
    service = ResearchService(raw, processed, state_dir=state, candidates=[canonical_candidate()])
    service.run_batch(use_processes=False)
    ledgers = state / "ledgers"
    assert (ledgers / "canonical.json").exists()
    checkpoint = json.loads((state / "research_checkpoint.json").read_text(encoding="utf-8"))
    assert len(checkpoint["completed"]) == 1

    # 14: restart restores the complete state - nothing lost, nothing duplicated.
    restarted = ResearchService(raw, processed, state_dir=state, candidates=[canonical_candidate()])
    result = restarted.run_batch(use_processes=False)
    checkpoint_after = json.loads((state / "research_checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint_after["completed"] == checkpoint["completed"]
    assert result.raw_candidate_trades == 0  # honest zero on this fixture, preserved not zeroed-by-loss
    assert restarted.status().state == "idle"
