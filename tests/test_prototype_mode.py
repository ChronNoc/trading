"""Tests for the free synthetic prototype mode."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_API", "pyside6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QComboBox, QLabel, QListWidget, QPushButton, QTableWidget

from app.prototype.scenarios import (
    PROTOTYPE_SYMBOL,
    PrototypeDashboardSnapshot,
    build_default_prototype_scenario,
    empty_prototype_dashboard_snapshot,
    evaluate_prototype_setups,
    scenario_with_controls,
)
from app.runtime.controller import AutomaticRuntimeController
from bookmap_addon.events import is_control_event, parse_stream_message
from tools.start_assistant import AssistantStartupError
from tools.start_prototype import (
    PrototypeInstanceLock,
    PrototypeRuntimeConfig,
    PrototypeRuntimeState,
    parse_args,
    run_headless_prototype,
)
from tools.synthetic_bookmap_feed import PrototypePlaybackController, _event_payload


def test_synthetic_scenario_is_deterministic_and_has_expected_setup_outcomes() -> None:
    """The seeded prototype scenario accepts the clean case and rejects the lookalike."""
    first = build_default_prototype_scenario(seed=123)
    second = build_default_prototype_scenario(seed=123)

    assert [event.event for event in first.events] == [event.event for event in second.events]
    results = evaluate_prototype_setups(first)
    assert results["clean"].accepted is True
    assert results["rejected"].accepted is False
    rejected_failures = {condition.key for condition in results["rejected"].conditions if not condition.passed}
    assert {"bid_reload_count", "reclaimed_level"}.issubset(rejected_failures)


def test_prototype_wire_payloads_keep_exact_market_schemas_and_control_metadata() -> None:
    """Synthetic feed messages parse through the same strict receiver schemas."""
    scenario = build_default_prototype_scenario()
    stream = scenario_with_controls(scenario, playback_speed=5)

    control_types = []
    market_count = 0
    for scheduled in stream:
        parsed = parse_stream_message(_event_payload(scheduled.event))
        if is_control_event(parsed):
            control_types.append(parsed["type"])
            if parsed["type"] == "prototype_mode":
                assert parsed["source_mode"] == "prototype"
                assert parsed["synthetic"] is True
                assert parsed["valid_for_real_training"] is False
                assert parsed["valid_for_analysis"] == "prototype_only"
        else:
            market_count += 1
            assert "playback_speed" not in parsed

    assert {"connected", "heartbeat", "prototype_mode", "realtime_started", "session_ended"}.issubset(control_types)
    assert market_count > 0


def test_speed_changes_do_not_change_logical_market_outcomes() -> None:
    """Playback speed metadata affects timing only, not the deterministic market stream."""
    scenario = build_default_prototype_scenario(seed=999)
    speed_1 = [event.event for event in scenario_with_controls(scenario, playback_speed=1) if not is_control_event(event.event)]
    speed_10 = [event.event for event in scenario_with_controls(scenario, playback_speed=10) if not is_control_event(event.event)]

    assert speed_1 == speed_10
    assert evaluate_prototype_setups(scenario)["clean"].accepted is True


def test_headless_prototype_runs_through_receiver_and_writes_only_prototype_artifacts(tmp_path: Path) -> None:
    """The one-command prototype path uses the real receiver and recorder."""
    config = PrototypeRuntimeConfig(
        port=0,
        output_root=tmp_path / "data" / "prototype" / "raw",
        report_root=tmp_path / "data" / "prototype" / "reports",
        gui=False,
        wall_clock=False,
    )
    controller = AutomaticRuntimeController.from_config(
        "config/session_profiles.yaml",
        report_root=config.report_root,
    )
    state = PrototypeRuntimeState(playback=PrototypePlaybackController(speed=5))

    asyncio.run(run_headless_prototype(config, controller, state, run_once=True))

    manifests = sorted(config.output_root.rglob("session_manifest.json"))
    assert manifests
    manifest_data = [json.loads(path.read_text(encoding="utf-8")) for path in manifests]
    assert all(manifest["source_mode"] == "prototype" for manifest in manifest_data)
    assert any(manifest["synthetic"] is True for manifest in manifest_data)
    assert all(manifest["valid_for_real_training"] is False for manifest in manifest_data)
    assert any(manifest["analysis_scope"] == "prototype_only" for manifest in manifest_data)
    assert not (tmp_path / "data" / "raw").exists()
    assert config.report_root.exists()
    assert state.depth_events > 0
    assert state.trade_events > 0
    assert state.control_events > 0
    assert state.decision == "REJECTED"
    assert state.snapshot().shadow_order in ("", None)
    assert any(decision["decision"] == "accepted" for decision in controller.decisions)
    assert any(decision["decision"] == "rejected" for decision in controller.decisions)


def test_prototype_gui_panel_renders_and_controls_update_producer(qtbot: object) -> None:
    """The GUI surfaces prototype state and controls the synthetic producer only."""
    from app.gui.main_window import MainWindow

    commands: list[str] = []
    snapshot = replace(
        empty_prototype_dashboard_snapshot(),
        runtime_state="playing",
        synthetic_status="connected",
        decision="ACCEPTED",
        explanations=("LONG SETUP: ACCEPTED", "[pass] Price at overnight_low"),
        depth_events=10,
        trade_events=2,
        control_events=3,
    )
    window = MainWindow(
        prototype_snapshot_provider=lambda: snapshot,
        prototype_control_handler=commands.append,
        replay_data_root=Path("data/prototype/raw"),
    )
    qtbot.addWidget(window)

    banner = window.findChild(QLabel, "prototype_banner_label")
    status = window.findChild(QLabel, "prototype_synthetic_status")
    decision = window.findChild(QLabel, "prototype_decision")
    pause = window.findChild(QPushButton, "prototype_pause_button")
    speed = window.findChild(QComboBox, "prototype_speed_selector")

    assert banner is not None
    assert banner.text() == "PROTOTYPE DATA - NOT REAL MARKET DATA"
    assert status is not None
    assert status.text() == "connected"
    assert decision is not None
    assert decision.text() == "ACCEPTED"
    assert pause is not None
    pause.click()
    assert commands[-1] == "pause"
    assert speed is not None
    speed.setCurrentText("10")
    assert commands[-1] == "speed:10"


def test_prototype_snapshot_drives_both_main_screens_and_clears_rejected_order(qtbot: object) -> None:
    """Prototype refreshes Screen 1 and Screen 2 from the same current snapshot."""
    from app.gui.main_window import MainWindow

    snapshots = [
        replace(
            empty_prototype_dashboard_snapshot(),
            synthetic_status="connected",
            session="Prototype New York open",
            depth_events=12,
            trade_events=4,
            current_setup="Clean long absorption reclaim",
            decision="ACCEPTED",
            explanations=("[pass] absorption",),
            shadow_order="would have submitted a shadow bracket",
        ),
    ]
    window = MainWindow(prototype_snapshot_provider=lambda: snapshots[0])
    qtbot.addWidget(window)

    snapshots[0] = replace(
        snapshots[0],
        current_setup="Rejected lookalike",
        decision="rejected",
        explanations=("[pass] sell pressure", "[fail] no bid reload", "[fail] no reclaim"),
        shadow_order="would have submitted long MNQ-PROTOTYPE shadow bracket near 97.00",
    )
    window.refresh_live_dashboard()

    feed_table = window.findChild(QTableWidget, "connection_status_table")
    setup = window.findChild(QLabel, "dashboard_setup_name")
    status = window.findChild(QLabel, "dashboard_setup_status")
    session = window.findChild(QLabel, "dashboard_session")
    mode = window.findChild(QLabel, "dashboard_mode_value")
    shadow_order = window.findChild(QLabel, "prototype_shadow_order")
    decision_heading = window.findChild(QLabel, "decision_status")
    conditions = window.findChild(QListWidget, "decision_condition_list")

    assert feed_table is not None
    assert [feed_table.item(row, 1).text() for row in range(3)] == ["receiving"] * 3
    assert setup is not None and setup.text() == "Rejected lookalike"
    assert status is not None and status.text() == "rejected"
    assert session is not None and session.text() == "Prototype New York open"
    assert mode is not None and mode.text() == "PROTOTYPE"
    assert shadow_order is not None and shadow_order.text() in ("", None)
    assert decision_heading is not None
    assert decision_heading.text() == "Rejected lookalike: rejected"
    assert conditions is not None
    rendered_conditions = [conditions.item(row).text() for row in range(conditions.count())]
    assert rendered_conditions == list(snapshots[0].explanations)


def test_prototype_lock_prevents_duplicate_instances(tmp_path: Path) -> None:
    """The one-click runtime has a clear duplicate-instance guard."""
    lock_path = tmp_path / "prototype.lock"
    first = PrototypeInstanceLock(lock_path)
    first.acquire()
    try:
        with pytest.raises(AssistantStartupError):
            PrototypeInstanceLock(lock_path).acquire()
    finally:
        first.release()

    assert not lock_path.exists()


def test_parse_args_and_batch_files_use_module_launchers() -> None:
    """Both Windows entry points use their local-venv module launchers."""
    config = parse_args(
        [
            "--no-gui",
            "--port",
            "8777",
            "--speed",
            "10",
            "--output-root",
            "data/prototype/raw-test",
        ],
    )
    assert config.port == 8777
    assert config.speed == 10
    assert config.output_root == Path("data/prototype/raw-test")
    for batch_path, module in (
        (Path("start_mnq_prototype.bat"), "tools.start_prototype"),
        (Path("start_mnq_assistant.bat"), "tools.start_assistant"),
    ):
        batch_text = batch_path.read_text(encoding="utf-8")
        launch_lines = [
            line
            for line in batch_text.splitlines()
            if line.startswith(('".venv\\Scripts\\python.exe"', '"%PYTHON%"'))
        ]
        assert 'cd /d "%~dp0"' in batch_text
        assert ".venv\\Scripts\\python.exe" in batch_text
        assert len(launch_lines) == 1
        assert f"-m {module}" in launch_lines[0]
        assert ".py" not in launch_lines[0]
        assert "admin" not in batch_text.lower()


def test_disconnect_reconnect_and_warmup_flow_reaches_controller_state(tmp_path: Path) -> None:
    """Prototype control events exercise warm-up and reconnect paths in the runtime."""
    config = PrototypeRuntimeConfig(
        port=0,
        output_root=tmp_path / "prototype" / "raw",
        report_root=tmp_path / "prototype" / "reports",
        gui=False,
        wall_clock=False,
    )
    controller = AutomaticRuntimeController.from_config(
        "config/session_profiles.yaml",
        report_root=config.report_root,
    )
    state = PrototypeRuntimeState(playback=PrototypePlaybackController(speed=5))

    asyncio.run(run_headless_prototype(config, controller, state, run_once=True))

    assert state.warmup.endswith("complete")
    assert any(event.status == "data_gap" for event in controller.health.events)
    assert any(event.status == "disconnected" for event in controller.health.events)
    assert state.synthetic_status == "complete"


def test_prototype_code_does_not_import_execution_or_broker_modules() -> None:
    """Prototype code remains structurally separate from real execution modules."""
    root = Path(__file__).resolve().parents[1]
    combined = "\n".join(
        (root / path).read_text(encoding="utf-8").lower()
        for path in (
            "app/prototype/scenarios.py",
            str(Path("tools") / "start_prototype.py"),
            "tools/synthetic_bookmap_feed.py",
            "start_mnq_prototype.bat",
        )
    )

    assert "app.execution" not in combined
    assert "tradovate" not in combined
    assert "broker" not in combined
    assert PROTOTYPE_SYMBOL.lower() in combined
