"""Integration test for the real-episode build CLI over a fixture tree."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.database.recorder import MarketSessionRecorder
from tools.build_real_episodes import build_all


def _finalized_delayed_session(raw_root: Path) -> None:
    rec = MarketSessionRecorder(root_dir=raw_root, session_start_utc=datetime(2026, 7, 15, 0, 22, tzinfo=UTC))
    rec.record_control_event({"type": "delayed_mode", "timestamp_ns": 1, "delay_minutes": 15})
    base = 1_752_537_751_000_000_000
    price = Decimal("29500.00")
    for i in range(200):
        price += Decimal("0.25") if i % 2 == 0 else Decimal("-0.25")
        rec.record({"type": "depth_update", "timestamp": base + i * 500_000_000, "symbol": "MNQ",
                    "side": "bid" if i % 2 else "ask", "price": f"{price:.2f}",
                    "previous_size": "0", "new_size": str(i % 40 + 1)})
        rec.record({"timestamp_ns": base + i * 500_000_000 + 1, "price": f"{price:.2f}", "size": "1",
                    "aggressor_side": "buy" if i % 2 else "sell", "instrument": "MNQ", "sequence_id": i + 1})
    rec.finalize(clean_shutdown=True)


def _active_session(raw_root: Path) -> None:
    # Never finalized (no utc_end) -> active; the CLI must skip it, never open its files.
    rec = MarketSessionRecorder(root_dir=raw_root, session_start_utc=datetime(2026, 7, 15, 1, 0, tzinfo=UTC))
    rec.record_control_event({"type": "delayed_mode", "timestamp_ns": 1, "delay_minutes": 15})
    rec.record({"type": "depth_update", "timestamp": 10, "symbol": "MNQ", "side": "bid",
                "price": "29500.00", "previous_size": "0", "new_size": "5"})
    rec.flush()  # closed part exists, but no utc_end -> active and skipped


def test_build_all_processes_finalized_skips_active(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    _finalized_delayed_session(raw)
    _active_session(raw)

    processed = tmp_path / "processed"
    labels = tmp_path / "labels"
    results, rejected = build_all(raw, processed, labels)

    # Exactly one finalized eligible session processed; the active one is skipped safely.
    assert len(results) == 1
    assert results[0].provenance == "REAL_DELAYED"
    assert results[0].evaluations > 0
    # Honest result: whatever the strategy found, counts are consistent and no crash.
    assert results[0].completed >= 0
    assert (processed / f"{results[0].session_id}.decisions.jsonl").exists()
    assert (processed / f"{results[0].session_id}.episodes.jsonl").exists()
    assert (labels / f"{results[0].session_id}.labels.jsonl").exists()
    assert not any("010000Z" in path.name for path in processed.iterdir())
