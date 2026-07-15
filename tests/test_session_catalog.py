"""Tests for the read-only session catalog over data/raw."""

from __future__ import annotations

import json
from pathlib import Path

from app.research.session_catalog import build_catalog, classify_manifest, eligibility_report


def _write(session_dir: Path, manifest: dict) -> Path:
    session_dir.mkdir(parents=True, exist_ok=True)
    p = session_dir / "session_manifest.json"
    p.write_text(json.dumps(manifest), encoding="utf-8")
    return p


def test_delayed_session_mislabeled_live_is_reclassified_offline_only(tmp_path: Path) -> None:
    """A delayed session stored as source_mode=live is corrected at read time."""
    manifest = {
        "session_id": "session_20260715T002231Z",
        "source_mode": "live",  # the historical bug
        "data_delay_minutes": 15,
        "synthetic": False,
        "clean_shutdown": True,
        "utc_end": "2026-07-15T00:22:49Z",
        "continuity_status": "continuous",
        "event_counts": {"depth_updates": 11418, "trades": 266},
        "dropped_message_count": 0,
    }
    entry = classify_manifest(manifest, tmp_path / "m.json")

    assert entry.provenance == "REAL_DELAYED"
    assert entry.is_delayed is True
    assert entry.valid_for_live_decisions is False  # never live-ready
    assert entry.eligible_for_analysis is True  # but valid offline
    assert entry.eligible_for_order_flow_replay is True


def test_synthetic_session_excluded_from_real_metrics(tmp_path: Path) -> None:
    """Synthetic sessions are classified SYNTHETIC and never eligible."""
    manifest = {
        "source_mode": "prototype", "synthetic": True, "clean_shutdown": True,
        "utc_end": "x", "continuity_status": "continuous",
        "event_counts": {"depth_updates": 100, "trades": 10}, "dropped_message_count": 0,
    }
    entry = classify_manifest(manifest, tmp_path / "m.json")
    assert entry.provenance == "SYNTHETIC"
    assert entry.eligible_for_analysis is False


def test_active_session_is_skipped_never_opened(tmp_path: Path) -> None:
    """A session with no utc_end is active: skipped, never opened."""
    manifest = {
        "source_mode": "delayed", "data_delay_minutes": 15, "synthetic": False,
        "clean_shutdown": False, "utc_end": None, "continuity_status": "continuous",
        "event_counts": {"depth_updates": 5000, "trades": 100}, "dropped_message_count": 0,
    }
    entry = classify_manifest(manifest, tmp_path / "m.json")
    assert entry.active is True
    assert entry.finalized is False
    assert entry.eligible_for_analysis is False
    assert any("active recording" in r for r in entry.reasons)


def test_build_catalog_and_report_over_a_tree(tmp_path: Path) -> None:
    """build_catalog scans manifests and the report summarizes eligibility."""
    _write(tmp_path / "2026-07-15" / "session_a", {
        "source_mode": "delayed", "data_delay_minutes": 15, "synthetic": False,
        "clean_shutdown": True, "utc_end": "x", "continuity_status": "continuous",
        "event_counts": {"depth_updates": 9000, "trades": 200}, "dropped_message_count": 0,
    })
    _write(tmp_path / "2026-07-15" / "session_b", {
        "source_mode": "prototype", "synthetic": True, "clean_shutdown": True,
        "utc_end": "x", "continuity_status": "continuous",
        "event_counts": {"depth_updates": 10, "trades": 1}, "dropped_message_count": 0,
    })
    catalog = build_catalog(tmp_path)
    report = eligibility_report(catalog)

    assert report["total_sessions"] == 2
    assert report["real_delayed"] == 1
    assert report["synthetic"] == 1
    assert report["eligible_for_analysis"] == 1
    assert report["valid_for_live_decisions"] == 0


def test_feed_quality_gap_blocks_strategy_replay_but_remains_catalogued(tmp_path: Path) -> None:
    manifest = {
        "source_mode": "delayed", "data_delay_minutes": 15, "synthetic": False,
        "clean_shutdown": True, "utc_end": "x", "continuity_status": "continuous",
        "event_counts": {"depth_updates": 100, "trades": 10}, "dropped_message_count": 0,
        "data_quality": {
            "malformed_events": 0, "rejected_events": 0, "missed_trade_events": 4,
        },
    }
    entry = classify_manifest(manifest, tmp_path / "m.json")
    assert entry.eligible_for_analysis is True
    assert entry.eligible_for_order_flow_replay is False
    assert any("missing events" in reason for reason in entry.reasons)
