"""Autonomous Intelligence + Reports GUI: real data, parity, honest safety."""

from __future__ import annotations

import json
from pathlib import Path

from app.gui.autonomous_view import AutonomousSnapshot, read_autonomous_snapshot


# -- view layer (pure, no Qt) ---------------------------------------------------------


def test_absent_store_is_honest_not_a_placeholder(tmp_path: Path) -> None:
    snap = read_autonomous_snapshot(tmp_path / "autonomous", tmp_path / "reports")
    assert snap.service_running is False
    assert snap.store_present is False
    assert snap.candidate_count == 0
    assert "has not run" in snap.note


def test_populated_store_surfaces_candidates_and_activity(tmp_path: Path) -> None:
    store = tmp_path / "autonomous"
    (store / "candidates").mkdir(parents=True)
    (store / "candidates" / "deadbeef.json").write_text(json.dumps({
        "candidate_id": "deadbeef", "kind": "STRATEGY", "state": "WALK_FORWARD_VALIDATED",
        "hypothesis": "pullback continuation edge", "proposer": "autonomous-researcher",
        "created_at_ns": 1_752_000_000_000_000_000, "attempt_count": 2, "errors": ["cost gate failed"],
    }), encoding="utf-8")
    (store / "activity.jsonl").write_text(json.dumps({
        "event": "candidate_proposed", "candidate_id": "deadbeef",
        "recorded_at_ns": 1_752_000_000_000_000_000}) + "\n", encoding="utf-8")

    snap = read_autonomous_snapshot(store, tmp_path / "reports")
    assert snap.service_running is True and snap.candidate_count == 1
    candidate = snap.candidates[0]
    assert candidate.kind == "STRATEGY" and candidate.state == "WALK_FORWARD_VALIDATED"
    assert candidate.last_error == "cost gate failed"
    assert snap.counts_by_state == {"WALK_FORWARD_VALIDATED": 1}
    assert snap.recent_activity and snap.recent_activity[0].event == "candidate_proposed"


def test_reports_are_categorised_dated_and_provenance_tagged(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    (reports / "daily_learning" / "2026-07-20").mkdir(parents=True)
    (reports / "daily_learning" / "2026-07-20" / "daily_learning.md").write_text(
        "delayed data", encoding="utf-8")
    (reports / "2026-07-20" / "session_x").mkdir(parents=True)
    (reports / "2026-07-20" / "session_x" / "session_report.md").write_text("real", encoding="utf-8")
    (reports / "paper").mkdir(parents=True)
    (reports / "paper" / "synthetic_run.md").write_text("synthetic fixture", encoding="utf-8")

    snap = read_autonomous_snapshot(tmp_path / "autonomous", reports)
    by_category = {r.category for r in snap.reports}
    assert {"daily_learning", "session", "paper"} <= by_category
    covered = {r.category: r.covered for r in snap.reports}
    assert covered["daily_learning"] == "2026-07-20" and covered["session"] == "2026-07-20"
    provenance = {r.category: r.provenance for r in snap.reports}
    assert provenance["daily_learning"] == "DELAYED"
    assert provenance["paper"] == "SYNTHETIC"


# -- screens (need a QApplication via pytest-qt's qapp) --------------------------------


def test_build_screens_preserves_all_originals_and_adds_two(qapp) -> None:
    from app.gui.screens import AUTONOMOUS, REPORTS, SCREEN_ORDER, build_screens

    screens = build_screens()
    originals = ["Overview", "Live Order Flow", "Paper Trading", "Sessions and Replay",
                 "Research and Model Health", "Risk and Lucid Account", "Execution",
                 "Diagnostics and Settings"]
    for name in originals:
        assert name in screens, f"original screen removed: {name}"
    assert AUTONOMOUS in screens and REPORTS in screens
    assert len(screens) == 10 and len(SCREEN_ORDER) == 10
    # Every screen is reachable from the nav order.
    assert set(SCREEN_ORDER) == set(screens)


def test_autonomous_screen_renders_real_data_and_shadow_only_safety(qapp) -> None:
    from app.gui.screens import AutonomousIntelligenceScreen

    screen = AutonomousIntelligenceScreen()
    view = AutonomousSnapshot(
        service_running=True, store_present=True, candidate_count=1, completed_count=0,
        counts_by_state={"SHADOW_OBSERVING": 1}, note="1 candidate; shadow-only.")
    screen.apply_view(view)  # pure Qt-thread update, no file I/O
    summary = screen.body.text()
    assert "Shadow-only" in summary and "LIVE locked" in summary
    assert screen.safety.text().upper().find("SHADOW-ONLY") >= 0


def test_reports_screen_reads_real_reports_and_never_claims_profit(qapp) -> None:
    from app.gui.screens import ReportsScreen

    screen = ReportsScreen()  # defaults to the real data/reports tree
    screen.apply_view(read_autonomous_snapshot())
    summary = screen.body.text()
    assert "profitability has been proven" in summary  # the disclaimer line
    # Real reports are present in this repository.
    assert "Reports:" in summary
