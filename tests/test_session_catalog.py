"""Tests for the read-only session catalog over data/raw."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def test_malformed_and_non_object_manifests_remain_visible_and_fail_closed(tmp_path: Path) -> None:
    """Every discovered manifest is counted even when its JSON is unusable."""
    malformed = tmp_path / "2026-07-15" / "session_malformed" / "session_manifest.json"
    non_object = tmp_path / "2026-07-15" / "session_list" / "session_manifest.json"
    malformed.parent.mkdir(parents=True)
    non_object.parent.mkdir(parents=True)
    malformed.write_text("{truncated", encoding="utf-8")
    non_object.write_text("[]", encoding="utf-8")

    entries = build_catalog(tmp_path)

    assert len(entries) == 2
    by_id = {entry.session_id: entry for entry in entries}
    assert by_id["session_malformed"].model_training_reasons == (
        "manifest unreadable: active or truncated",
    )
    assert by_id["session_list"].model_training_reasons == (
        "manifest invalid: top-level JSON value must be an object",
    )
    assert all(not entry.eligible_for_analysis for entry in entries)
    assert all(not entry.eligible_for_model_training for entry in entries)


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


def test_eligibility_report_includes_deterministic_model_training_exclusions() -> None:
    """Model-training observability is stable and does not weaken eligibility."""
    clean = classify_manifest(
        _manifest(session_id="session_clean"),
        Path("session_clean/session_manifest.json"),
    )
    legacy = classify_manifest(
        _manifest(session_id="session_legacy", bridge_provenance={}, storage={}),
        Path("session_legacy/session_manifest.json"),
    )
    active = classify_manifest(
        _manifest(session_id="session_active", utc_end=None),
        Path("session_active/session_manifest.json"),
    )

    forward = eligibility_report((clean, legacy, active))
    reverse = eligibility_report((active, legacy, clean))

    assert forward == reverse
    assert forward["eligible_for_model_training"] == 1
    assert forward["model_training_exclusion_reasons"] == dict(sorted(
        forward["model_training_exclusion_reasons"].items(),
    ))
    exclusions = forward["model_training_exclusion_reasons"]
    assert exclusions["accepted bridge handshake provenance is missing"] == 1
    assert exclusions["active recording: skip until finalized"] == 1
    assert exclusions["closed storage provenance is missing"] == 2
    assert legacy.eligible_for_model_training is False
    assert active.eligible_for_model_training is False


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


def _manifest(**overrides: object) -> dict:
    """A clean, order-flow-eligible delayed manifest; override to break one rule."""
    base = {
        "session_id": "session_test", "source_mode": "delayed", "data_delay_minutes": 15,
        "synthetic": False, "clean_shutdown": True, "utc_end": "2026-07-15T01:00:00+00:00",
        "utc_start": "2026-07-15T00:00:00+00:00", "continuity_status": "continuous",
        "dropped_message_count": 0,
        "event_counts": {"depth_updates": 10_000, "trades": 2_000},
        "storage": {"format": "closed_parquet_parts_v1", "depth_parts": 1, "trade_parts": 1},
        "bridge_provenance": {
            "protocol_version": "1.2", "provider": "pytest", "handshake_accepted": True,
            "declared_capabilities": ["aggregated_depth", "trades", "aggressor_side", "source_timestamps"],
            "observed": {"depth_updates": 10_000, "trades": 2_000, "trades_with_aggressor_side": 2_000},
        },
        "data_quality": {
            "session_dropped_messages": 0, "receiver_queue_overflow": 0,
            "recorder_queue_overflow": 0, "receiver_intake_lost": 0,
            "malformed_events": 0, "rejected_events": 0, "out_of_order_events": 0,
            "duplicate_stream_events": 0, "missed_trade_events": 0,
            "trade_sequence_gaps": 0,
        },
    }
    for key, value in overrides.items():
        if key in base["data_quality"]:
            base["data_quality"][key] = value  # type: ignore[index]
        elif key in base["event_counts"]:
            base["event_counts"][key] = value  # type: ignore[index]
        else:
            base[key] = value
    return base


def test_clean_manifest_is_order_flow_eligible() -> None:
    entry = classify_manifest(_manifest(), Path("m.json"))
    assert entry.eligible_for_order_flow_replay is True


def test_clean_protocol_12_manifest_is_model_training_eligible(tmp_path: Path) -> None:
    entry = classify_manifest(_manifest(), tmp_path / "session_manifest.json")
    assert entry.eligible_for_model_training is True
    assert entry.model_training_reasons == ()


def test_legacy_manifest_stays_model_training_ineligible_after_reporting_upgrade() -> None:
    """Observability cannot retrofit missing historical provenance."""
    entry = classify_manifest(
        _manifest(bridge_provenance={}, storage={}),
        Path("legacy/session_manifest.json"),
    )

    report = eligibility_report((entry,))

    assert entry.eligible_for_model_training is False
    assert report["eligible_for_model_training"] == 0
    assert report["model_training_exclusion_reasons"][
        "accepted bridge handshake provenance is missing"
    ] == 1


def test_old_protocol_and_missing_provider_fail_closed_for_model_training(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["bridge_provenance"]["protocol_version"] = "1.1"  # type: ignore[index]
    manifest["bridge_provenance"]["provider"] = ""  # type: ignore[index]
    entry = classify_manifest(manifest, tmp_path / "session_manifest.json")
    assert entry.eligible_for_order_flow_replay is True
    assert entry.eligible_for_model_training is False
    assert any("protocol 1.2" in reason for reason in entry.model_training_reasons)
    assert "bridge provider is missing" in entry.model_training_reasons


def test_missing_capability_and_aggressor_coverage_block_model_training(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["bridge_provenance"]["declared_capabilities"] = ["aggregated_depth", "trades"]  # type: ignore[index]
    manifest["bridge_provenance"]["observed"]["trades_with_aggressor_side"] = 1_999  # type: ignore[index]
    entry = classify_manifest(manifest, tmp_path / "session_manifest.json")
    assert entry.eligible_for_model_training is False
    assert any("required capabilities" in reason for reason in entry.model_training_reasons)
    assert any("aggressor-side coverage incomplete" in reason for reason in entry.model_training_reasons)


@pytest.mark.parametrize("override,label", [
    ({"clean_shutdown": False}, "unclean shutdown"),
    ({"continuity_status": "receiver_error"}, "not continuous"),
    ({"session_dropped_messages": 1}, "one session drop"),
    ({"receiver_queue_overflow": 1}, "receiver queue overflow"),
    ({"recorder_queue_overflow": 1}, "recorder queue overflow"),
    ({"receiver_intake_lost": 1}, "receiver intake loss"),
    ({"out_of_order_events": 1}, "out-of-order event"),
    ({"duplicate_stream_events": 1}, "duplicate stream event"),
    ({"malformed_events": 1}, "malformed event"),
    ({"rejected_events": 1}, "rejected event"),
    ({"missed_trade_events": 1}, "missing trade event"),
    ({"trade_sequence_gaps": 1}, "trade sequence gap"),
    ({"trades": 0}, "depth-only, no trades"),
    ({"depth_updates": 0}, "no depth"),
])
def test_every_quality_failure_blocks_order_flow_eligibility(override: dict, label: str) -> None:
    """Order-flow research demands a spotless record: any single flaw disqualifies."""
    entry = classify_manifest(_manifest(**override), Path("m.json"))
    assert entry.eligible_for_order_flow_replay is False, f"{label} must disqualify"


def test_million_drop_session_stays_visibly_ineligible() -> None:
    """The observed ~1M-drop run must never qualify for research."""
    entry = classify_manifest(
        _manifest(session_dropped_messages=1_036_394, dropped_message_count=1_036_394),
        Path("m.json"),
    )
    assert entry.eligible_for_order_flow_replay is False
    assert entry.eligible_for_analysis is True  # catalogued and visible, just not researchable
    assert any("1036394" in r.replace(",", "") or "1,036,394" in r for r in entry.reasons)


def test_lifetime_drop_total_is_distinguished_from_session_drops() -> None:
    """A huge Java LIFETIME total with zero session loss must not disqualify."""
    entry = classify_manifest(
        _manifest(dropped_message_count=1_031_435, session_dropped_messages=0),
        Path("m.json"),
    )
    assert entry.dropped_message_count == 0  # the per-session figure is what counts
    assert entry.eligible_for_order_flow_replay is True
    assert any("lifetime" in r for r in entry.reasons)  # still surfaced for the user


def test_missing_session_drop_figure_fails_closed() -> None:
    """With no per-session figure, the lifetime total is assumed - never zero."""
    manifest = _manifest(dropped_message_count=5_000)
    manifest["data_quality"].pop("session_dropped_messages", None)  # type: ignore[union-attr]
    manifest["data_quality"].pop("bridge_dropped_messages", None)  # type: ignore[union-attr]
    entry = classify_manifest(manifest, Path("m.json"))
    assert entry.dropped_message_count == 5_000
    assert entry.eligible_for_order_flow_replay is False
