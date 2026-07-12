"""Tests for the GUI automation engine and its window integration."""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_API", "pyside6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import QCheckBox, QLabel, QListWidget

from app.agents.records import DecisionRecord
from app.agents.watchdog import SEVERITY_OK, SEVERITY_WARNING, WatchdogReport
from app.gui.automations import AUTOMATION_DEFINITIONS, AutomationEngine
from app.gui.main_window import MainWindow
from app.prototype.scenarios import empty_prototype_dashboard_snapshot

ACCEPTED_LINES = ("[pass] Bid liquidity reloaded 2 times", "[pass] Reclaim confirmation completed")
REJECTED_LINES = ("[fail] Bid liquidity did not reload", "[fail] Reclaim confirmation not completed")


def _record(decision: str, lines: tuple[str, ...], *, at: str = "14:30:00") -> DecisionRecord:
    return DecisionRecord(recorded_at=at, setup_name="Long absorption reclaim", decision=decision, explanations=lines)


def _snapshot(**overrides: object):
    base = replace(
        empty_prototype_dashboard_snapshot(),
        runtime_state="playing",
        synthetic_status="prototype stream active",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_automation_definitions_cover_twelve_toggles() -> None:
    """There are at least 12 documented, individually toggleable automations."""
    assert len(AUTOMATION_DEFINITIONS) >= 12
    assert len({item.key for item in AUTOMATION_DEFINITIONS}) == len(AUTOMATION_DEFINITIONS)


def test_gap_pause_and_resume(tmp_path: Path) -> None:
    """A feed gap pauses playback; recovery resumes it exactly once."""
    engine = AutomationEngine(output_root=tmp_path)

    gap = engine.process(_snapshot(synthetic_status="reconnecting"), ())
    assert "pause" in gap.control_commands

    still_gap = engine.process(_snapshot(synthetic_status="reconnecting", paused=True), ())
    assert still_gap.control_commands == ()

    recovered = engine.process(_snapshot(paused=True), ())
    assert "resume" in recovered.control_commands

    steady = engine.process(_snapshot(), ())
    assert steady.control_commands == ()


def test_slow_on_decision_then_restore(tmp_path: Path) -> None:
    """A new decision slows playback to 2x and later restores the old speed."""
    engine = AutomationEngine(output_root=tmp_path)
    history = (_record("ACCEPTED", ACCEPTED_LINES),)

    first = engine.process(_snapshot(playback_speed=10), history)
    assert "speed:2" in first.control_commands

    restore_commands: list[str] = []
    for _ in range(10):
        actions = engine.process(_snapshot(playback_speed=2), history)
        restore_commands.extend(actions.control_commands)
    assert "speed:10" in restore_commands


def test_flash_on_accept_expires(tmp_path: Path) -> None:
    """Accepted setups flash a banner that clears after a few ticks."""
    engine = AutomationEngine(output_root=tmp_path)

    actions = engine.process(_snapshot(), (_record("ACCEPTED", ACCEPTED_LINES),))
    assert actions.flash is not None
    assert "SETUP ACCEPTED" in actions.flash
    assert "no real order" in actions.flash

    for _ in range(6):
        actions = engine.process(_snapshot(), (_record("ACCEPTED", ACCEPTED_LINES),))
    assert actions.flash is None


def test_csv_export_and_decision_snapshots(tmp_path: Path) -> None:
    """Each decision lands in the CSV log and gets its own JSON snapshot."""
    engine = AutomationEngine(output_root=tmp_path)
    history = (_record("ACCEPTED", ACCEPTED_LINES), _record("REJECTED", REJECTED_LINES, at="14:35:00"))

    engine.process(_snapshot(last_trade_price="100.25"), history)

    csv_text = (tmp_path / "automation" / "decisions_log.csv").read_text(encoding="utf-8")
    assert csv_text.splitlines()[0].startswith("recorded_at,")
    assert csv_text.count("Long absorption reclaim") == 2
    assert ",REJECTED,2," in csv_text

    snapshots = sorted((tmp_path / "automation" / "decision_snapshots").glob("*.json"))
    assert len(snapshots) == 2
    payload = json.loads(snapshots[0].read_text(encoding="utf-8"))
    assert payload["synthetic"] is True
    assert payload["decision"] == "ACCEPTED"


def test_review_on_complete_writes_once(tmp_path: Path) -> None:
    """Session completion auto-writes exactly one review."""
    engine = AutomationEngine(output_root=tmp_path)
    history = (_record("REJECTED", REJECTED_LINES),)

    running = engine.process(_snapshot(), history)
    assert running.flash is None

    done = engine.process(_snapshot(synthetic_status="complete"), history)
    assert done.flash is not None
    assert "Session review auto-generated" in done.flash

    again = engine.process(_snapshot(synthetic_status="complete"), history)
    assert again.flash is None
    reviews = list((tmp_path / "automation" / "session_reviews").rglob("*.md"))
    assert len(reviews) == 1


def test_entry_limit_guard_after_three_accepted(tmp_path: Path) -> None:
    """Three accepted setups raise the persistent entry-limit banner."""
    engine = AutomationEngine(output_root=tmp_path)
    two = tuple(_record("ACCEPTED", ACCEPTED_LINES, at=f"14:{index}0:00") for index in range(2))
    three = two + (_record("ACCEPTED", ACCEPTED_LINES, at="15:00:00"),)

    assert engine.process(_snapshot(), two).persistent_banner is None
    banner = engine.process(_snapshot(), three).persistent_banner
    assert banner is not None
    assert "Daily entry limit reached" in banner


def test_blocker_coach_names_repeated_failure(tmp_path: Path) -> None:
    """The same condition failing across recent rejections gets named."""
    engine = AutomationEngine(output_root=tmp_path)
    history = (
        _record("REJECTED", REJECTED_LINES),
        _record("REJECTED", REJECTED_LINES, at="14:40:00"),
    )

    coach = engine.process(_snapshot(), history).coach
    assert coach is not None
    assert "Chronic blocker" in coach
    assert "Bid reload" in coach or "Level reclaim" in coach


def test_watchdog_journal_dedupes_consecutive_warnings(tmp_path: Path) -> None:
    """Warnings are journaled once per distinct message; OK reports are not."""
    engine = AutomationEngine(output_root=tmp_path)
    warning = WatchdogReport(SEVERITY_WARNING, "Watchdog: feed disconnected")

    engine.process(_snapshot(), (), warning)
    engine.process(_snapshot(), (), warning)
    engine.process(_snapshot(), (), WatchdogReport(SEVERITY_OK, "healthy"))

    journal = (tmp_path / "automation" / "watchdog_events.jsonl").read_text(encoding="utf-8")
    assert journal.count("feed disconnected") == 1


def test_latest_report_pointer_tracks_changes(tmp_path: Path) -> None:
    """The pointer file follows the newest report path."""
    engine = AutomationEngine(output_root=tmp_path)

    engine.process(_snapshot(report_path="not written yet"), ())
    assert not (tmp_path / "automation" / "LATEST_REPORT.txt").exists()

    engine.process(_snapshot(report_path="data/prototype/reports/2026-07-12/session_1"), ())
    pointer = (tmp_path / "automation" / "LATEST_REPORT.txt").read_text(encoding="utf-8")
    assert "session_1" in pointer


def test_replay_refresh_fires_periodically_and_toggles_off(tmp_path: Path) -> None:
    """Replay refresh fires every tenth tick and stops when disabled."""
    engine = AutomationEngine(output_root=tmp_path)
    fired = [engine.process(_snapshot(), ()).refresh_replay for _ in range(10)]
    assert fired.count(True) == 1

    engine.enabled["auto_refresh_replay"] = False
    fired_disabled = [engine.process(_snapshot(), ()).refresh_replay for _ in range(10)]
    assert fired_disabled.count(True) == 0


def test_disabled_automation_does_not_fire(tmp_path: Path) -> None:
    """Toggling an automation off suppresses it."""
    engine = AutomationEngine(output_root=tmp_path)
    engine.enabled["auto_pause_on_gap"] = False

    actions = engine.process(_snapshot(synthetic_status="reconnecting"), ())
    assert actions.control_commands == ()


def test_startup_checklist_reports_environment(tmp_path: Path) -> None:
    """The checklist reflects the real repo environment."""
    repo_root = Path(__file__).resolve().parents[1]
    engine = AutomationEngine(output_root=tmp_path, repo_root=repo_root)

    items = engine.startup_checklist()

    names = [item.name for item in items]
    assert "Virtual environment" in names
    assert "Scenario library" in names
    assert "Data directory writable" in names
    assert "Last self-check" in names
    by_name = {item.name: item for item in items}
    assert by_name["Scenario library"].passed is True
    assert by_name["Data directory writable"].passed is True


def test_window_runs_automations_and_renders_toggles(qtbot: object, tmp_path: Path) -> None:
    """The window pauses on a gap, logs the decision CSV, and shows toggles."""
    commands: list[str] = []
    snapshots = [
        _snapshot(
            synthetic_status="reconnecting",
            current_setup="Rejected lookalike",
            decision="REJECTED",
            explanations=REJECTED_LINES,
        ),
    ]
    window = MainWindow(
        prototype_snapshot_provider=lambda: snapshots[0],
        prototype_control_handler=commands.append,
        automation_output_root=tmp_path,
    )
    qtbot.addWidget(window)
    window.refresh_live_dashboard()

    assert "pause" in commands
    assert (tmp_path / "automation" / "decisions_log.csv").exists()

    toggle = window.findChild(QCheckBox, "automation_toggle_auto_pause_on_gap")
    assert toggle is not None
    toggle.setChecked(False)
    assert window._automation_engine.enabled["auto_pause_on_gap"] is False

    checklist = window.findChild(QListWidget, "startup_checklist_list")
    assert checklist is not None
    assert checklist.count() >= 4

    count_label = window.findChild(QLabel, "automation_count_auto_pause_on_gap")
    assert count_label is not None
    assert count_label.text() == "fired 1x"


def test_window_shows_entry_limit_banner_and_coach(qtbot: object, tmp_path: Path) -> None:
    """Persistent banner and coach labels surface through the window."""
    snapshots = [
        _snapshot(current_setup="Rejected lookalike", decision="REJECTED", explanations=REJECTED_LINES),
    ]
    window = MainWindow(
        prototype_snapshot_provider=lambda: snapshots[0],
        automation_output_root=tmp_path,
    )
    qtbot.addWidget(window)
    window.refresh_live_dashboard()

    snapshots[0] = replace(
        snapshots[0],
        current_setup="Second lookalike",
        explanations=("[fail] Bid liquidity did not reload",),
    )
    window.refresh_live_dashboard()

    coach = window.findChild(QLabel, "blocker_coach_label")
    assert coach is not None
    assert coach.isHidden() is False
    assert "Chronic blocker" in coach.text()
    assert "Bid reload" in coach.text()
