"""Tests for the price chart, replay scrubber, scenario library, rule extractor, and self-check."""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_API", "pyside6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel, QSlider, QTableWidget

from app.database.recorder import MarketEventRecorder
from app.gui.main_window import MainWindow
from app.gui.price_chart import PriceChartWidget
from app.prototype.scenario_library import (
    ScenarioDefinitionError,
    available_scenarios,
    load_scenario_yaml,
)
from app.prototype.scenarios import empty_prototype_dashboard_snapshot, evaluate_prototype_setups
from tools.self_check import run_self_check
from tools.start_assistant import AssistantStartupError
from tools.start_prototype import parse_args
from video_analysis.extract_rules import extract_candidates, run_extraction

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_DIR = REPO_ROOT / "config" / "prototype_scenarios"


def test_price_chart_accumulates_prices_and_markers(qtbot: object) -> None:
    """The chart parses prices, skips duplicates, and tags decisions."""
    chart = PriceChartWidget()
    qtbot.addWidget(chart)

    assert chart.add_price("100.00") is True
    assert chart.add_price("100.00") is False
    assert chart.add_price("100.25") is True
    assert chart.add_price("not a price") is False
    chart.mark_decision(accepted=True)

    assert chart.point_count() == 2
    assert chart.marker_count() == 1
    chart.clear()
    assert chart.point_count() == 0
    assert chart.marker_count() == 0


def test_price_chart_rolls_old_points_and_reindexes_markers(qtbot: object) -> None:
    """Overflowing points drop from the left and markers shift with them."""
    chart = PriceChartWidget(max_points=3)
    qtbot.addWidget(chart)

    chart.add_price("1")
    chart.mark_decision(accepted=False)
    for value in ("2", "3", "4"):
        chart.add_price(value)

    assert chart.point_count() == 3
    assert chart.marker_count() == 0


def test_live_dashboard_feeds_the_price_chart_from_snapshots(qtbot: object) -> None:
    """Refreshing with new trade prices grows the chart; decisions add markers."""
    snapshots = [
        replace(
            empty_prototype_dashboard_snapshot(),
            runtime_state="playing",
            synthetic_status="connected",
            last_trade_price="100.00",
        ),
    ]
    window = MainWindow(prototype_snapshot_provider=lambda: snapshots[0])
    qtbot.addWidget(window)

    window.refresh_live_dashboard()
    snapshots[0] = replace(
        snapshots[0],
        last_trade_price="100.50",
        current_setup="Clean long absorption reclaim",
        decision="ACCEPTED",
        explanations=("[pass] Reclaim confirmation completed",),
    )
    window.refresh_live_dashboard()

    assert window.price_chart.point_count() == 2
    assert window.price_chart.marker_count() == 1


def test_replay_scrubber_limits_visible_events(qtbot: object, tmp_path: Path) -> None:
    """Moving the scrubber shows only events up to that position."""
    recorder = MarketEventRecorder(root_dir=tmp_path)
    base_ns = int(datetime(2026, 7, 10, 14, 30, tzinfo=UTC).timestamp()) * 1_000_000_000
    recorder.record(
        {
            "type": "depth_update",
            "timestamp": base_ns,
            "symbol": "MNQ",
            "side": "bid",
            "price": "100.00",
            "previous_size": "0",
            "new_size": "10",
        },
    )
    recorder.record(
        {
            "timestamp_ns": base_ns + 1,
            "price": "100.25",
            "size": "3",
            "aggressor_side": "buy",
            "instrument": "MNQ",
            "sequence_id": 1,
        },
    )

    window = MainWindow(replay_data_root=tmp_path)
    qtbot.addWidget(window)

    scrubber = window.findChild(QSlider, "replay_scrubber")
    table = window.findChild(QTableWidget, "replay_markers_table")
    label = window.findChild(QLabel, "replay_scrubber_label")

    assert scrubber is not None
    assert table is not None
    assert label is not None
    assert scrubber.maximum() == 2
    assert scrubber.value() == 2

    scrubber.setValue(1)
    assert table.rowCount() == 1
    assert table.item(0, 1).text() == "depth"
    assert "1 / 2 events" in label.text()

    scrubber.setValue(0)
    assert table.rowCount() == 0
    assert "0 / 2 events" in label.text()

    scrubber.setValue(2)
    assert table.rowCount() == 2


def test_shipped_scenarios_load_and_evaluate_correctly() -> None:
    """Both shipped scenario files build scenarios whose windows evaluate as labeled."""
    listed = available_scenarios(SCENARIO_DIR)
    assert [entry.name for entry in listed] == ["default_demo", "volatile_open_drill"]

    for entry in listed:
        scenario = load_scenario_yaml(entry.path)
        evaluations = evaluate_prototype_setups(scenario)
        assert evaluations["clean"].accepted is True, entry.name
        assert evaluations["rejected"].accepted is False, entry.name


def test_scenario_loader_rejects_invalid_definitions(tmp_path: Path) -> None:
    """Missing windows, unknown kinds, and missing prices are clear errors."""
    no_clean = tmp_path / "no_clean.yaml"
    no_clean.write_text(
        "windows:\n  - kind: warmup\n  - kind: absorption\n    level: '99.00'\n",
        encoding="utf-8",
    )
    with pytest.raises(ScenarioDefinitionError, match="reload: true and reclaim: true"):
        load_scenario_yaml(no_clean)

    unknown_kind = tmp_path / "unknown.yaml"
    unknown_kind.write_text("windows:\n  - kind: moon_phase\n", encoding="utf-8")
    with pytest.raises(ScenarioDefinitionError, match="unknown window kind"):
        load_scenario_yaml(unknown_kind)

    missing_level = tmp_path / "missing_level.yaml"
    missing_level.write_text(
        "windows:\n  - kind: absorption\n    reload: true\n    reclaim: true\n",
        encoding="utf-8",
    )
    with pytest.raises(ScenarioDefinitionError, match="'level' price"):
        load_scenario_yaml(missing_level)


def test_scenario_loader_is_deterministic_per_seed(tmp_path: Path) -> None:
    """The same file loads to identical events; markets stay Decimal-safe strings."""
    path = SCENARIO_DIR / "default_demo.yaml"

    first = load_scenario_yaml(path)
    second = load_scenario_yaml(path)

    assert first.events == second.events
    assert first.seed == 20260710
    depth_prices = [
        Decimal(str(event.event["price"]))
        for event in first.events
        if event.event.get("type") == "depth_update"
    ]
    assert depth_prices


def test_parse_args_accepts_existing_scenario_and_rejects_missing() -> None:
    """--scenario must point at a real file."""
    config = parse_args(["--scenario", str(SCENARIO_DIR / "default_demo.yaml"), "--no-gui"])
    assert config.scenario == SCENARIO_DIR / "default_demo.yaml"

    with pytest.raises(AssistantStartupError, match="scenario file not found"):
        parse_args(["--scenario", "does/not/exist.yaml"])


def test_extract_rules_finds_cited_candidates(tmp_path: Path) -> None:
    """Transcript sentences produce spec-field candidates with citations."""
    transcript = {
        "segments": [
            {"start": 12.0, "end": 15.5, "text": "I put my stop 8 ticks below the level."},
            {"start": 30.0, "end": 34.0, "text": "You want at least 400 contracts sold into that bid."},
            {"start": 55.0, "end": 58.0, "text": "Price should reclaim 1 tick over the level."},
            {"start": 70.0, "end": 73.0, "text": "Nothing tradeable in this part."},
        ],
    }
    transcript_path = tmp_path / "transcript.json"
    transcript_path.write_text(json.dumps(transcript), encoding="utf-8")

    candidates = extract_candidates(transcript["segments"])
    fields = {candidate.field for candidate in candidates}
    assert "exit.stop_method" in fields
    assert "entry_setup.aggressive_volume_minimum" in fields
    assert "entry_setup.reclaim_ticks" in fields

    draft_path, citations_path = run_extraction(transcript_path, tmp_path / "out")
    draft = draft_path.read_text(encoding="utf-8")
    citations = citations_path.read_text(encoding="utf-8")
    assert "DRAFT" in draft
    assert "exit.stop_method" in draft
    assert 'value: "8"' in draft
    assert "00:12" in citations
    assert "at least 400 contracts" in citations


def test_extract_rules_missing_transcript_is_a_clear_error(tmp_path: Path) -> None:
    """A missing transcript raises FileNotFoundError, not a stack trace downstream."""
    with pytest.raises(FileNotFoundError, match="Transcript file not found"):
        run_extraction(tmp_path / "missing.json", tmp_path / "out")


def test_self_check_writes_pass_and_fail_reports(tmp_path: Path) -> None:
    """The self-check report reflects the injected runner outcome."""
    moment = datetime(2026, 7, 12, 3, 0, tzinfo=UTC)

    pass_path = run_self_check(
        runner=lambda: (0, "205 passed in 8.0s"),
        report_root=tmp_path,
        now=moment,
    )
    assert pass_path.name == "self_check_2026-07-12_030000.md"
    pass_text = pass_path.read_text(encoding="utf-8")
    assert pass_text.startswith("# Self-check PASS")
    assert "205 passed" in pass_text

    fail_path = run_self_check(
        runner=lambda: (1, "1 failed, 204 passed"),
        report_root=tmp_path / "fail",
        now=moment,
    )
    fail_text = fail_path.read_text(encoding="utf-8")
    assert fail_text.startswith("# Self-check FAIL")
    assert "should not be trusted" in fail_text
