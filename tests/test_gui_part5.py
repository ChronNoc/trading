"""Tests for Part 5 GUI: mode banner, decision-log viewer, leaderboard, traces."""

from __future__ import annotations

import json
import os

os.environ.setdefault("QT_API", "pyside6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from dataclasses import replace
from pathlib import Path

from PySide6.QtWidgets import QComboBox, QLabel, QPushButton, QTableWidget, QTextEdit

from app.discovery.supervisor import ModeSupervisor
from app.gui.main_window import MainWindow
from app.prototype.scenarios import empty_prototype_dashboard_snapshot


def _snapshot(**overrides: object):
    base = replace(
        empty_prototype_dashboard_snapshot(),
        runtime_state="playing",
        synthetic_status="connected",
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_mode_banner_shows_sim_and_live_armed_no_by_default(qtbot: object, tmp_path: Path) -> None:
    """Without an armed config, the banner is unmissably SIM and LIVE is NO."""
    window = MainWindow(mode_supervisor=ModeSupervisor(tmp_path / "production_config.yaml"))
    qtbot.addWidget(window)

    banner = window.findChild(QLabel, "mode_banner_label")
    armed = window.findChild(QLabel, "live_armed_label")
    arm_button = window.findChild(QPushButton, "arm_live_button")

    assert banner is not None
    assert "SIMULATION / SHADOW MODE" in banner.text()
    assert "NO REAL ORDERS" in banner.text()
    assert armed is not None
    assert armed.text() == "LIVE ARMED: NO"
    assert arm_button is not None
    assert arm_button.isEnabled() is False


def test_mode_banner_reflects_armed_config_read_only(qtbot: object, tmp_path: Path) -> None:
    """An armed config changes the banner; the GUI itself never writes the flag."""
    config = tmp_path / "production_config.yaml"
    config.write_text("live_mode: true\n", encoding="utf-8")
    window = MainWindow(mode_supervisor=ModeSupervisor(config))
    qtbot.addWidget(window)

    banner = window.findChild(QLabel, "mode_banner_label")
    armed = window.findChild(QLabel, "live_armed_label")

    assert banner is not None
    assert "LIVE MODE ARMED" in banner.text()
    assert armed is not None
    assert armed.text() == "LIVE ARMED: YES"
    assert config.read_text(encoding="utf-8") == "live_mode: true\n"


def test_decision_log_filters_by_outcome_and_regime(qtbot: object, tmp_path: Path) -> None:
    """The viewer filters accepted/rejected and by regime tag."""
    snapshots = [
        _snapshot(
            current_setup="Clean setup",
            decision="ACCEPTED",
            regime="trending",
            explanations=("[pass] Reclaim confirmation completed",),
        ),
    ]
    window = MainWindow(
        prototype_snapshot_provider=lambda: snapshots[0],
        automation_output_root=tmp_path,
        mode_supervisor=ModeSupervisor(tmp_path / "production_config.yaml"),
    )
    qtbot.addWidget(window)
    window.refresh_live_dashboard()
    snapshots[0] = _snapshot(
        current_setup="Lookalike",
        decision="REJECTED",
        regime="ranging",
        explanations=("[fail] Bid liquidity did not reload",),
    )
    window.refresh_live_dashboard()

    table = window.findChild(QTableWidget, "decision_log_table")
    decision_filter = window.findChild(QComboBox, "decision_filter_combo")
    regime_filter = window.findChild(QComboBox, "regime_filter_combo")

    assert table is not None
    assert table.rowCount() == 2

    assert decision_filter is not None
    decision_filter.setCurrentText("Rejected")
    assert table.rowCount() == 1
    assert table.item(0, 1).text() == "Lookalike"

    decision_filter.setCurrentText("All")
    assert regime_filter is not None
    regime_items = [regime_filter.itemText(index) for index in range(regime_filter.count())]
    assert "trending" in regime_items
    assert "ranging" in regime_items
    regime_filter.setCurrentText("trending")
    assert table.rowCount() == 1
    assert table.item(0, 1).text() == "Clean setup"


def test_decision_trace_explains_rejection_on_click(qtbot: object, tmp_path: Path) -> None:
    """Selecting a rejected decision shows the one-click why trace."""
    snapshots = [
        _snapshot(
            current_setup="Lookalike",
            decision="REJECTED",
            regime="ranging",
            explanations=(
                "[pass] Price at overnight_low",
                "[fail] Bid liquidity did not reload",
            ),
        ),
    ]
    window = MainWindow(
        prototype_snapshot_provider=lambda: snapshots[0],
        automation_output_root=tmp_path,
        mode_supervisor=ModeSupervisor(tmp_path / "production_config.yaml"),
    )
    qtbot.addWidget(window)
    window.refresh_live_dashboard()

    table = window.findChild(QTableWidget, "decision_log_table")
    trace = window.findChild(QTextEdit, "decision_trace_text")
    assert table is not None and trace is not None
    table.setCurrentCell(0, 0)

    text = trace.toPlainText()
    assert "Lookalike: REJECTED" in text
    assert "Regime: ranging" in text
    assert "nobody reloaded the bid" in text
    assert "[fail] Bid liquidity did not reload" in text


def test_leaderboard_renders_composite_breakdown_and_trace(qtbot: object, tmp_path: Path) -> None:
    """The leaderboard shows every metric column and rejection reasons on click."""
    discovery_root = tmp_path / "discovery"
    discovery_root.mkdir()
    entries = [
        {
            "parameters": "reload=2|volume=450|pull=0.55|target=2.0|stop=1.0",
            "accepted": True,
            "rejection_reasons": [],
            "composite": "1.5000",
            "win_rate": "0.6000",
            "profit_factor": "2.1000",
            "max_drawdown_r": "3.0000",
            "sortino": "1.2000",
            "expectancy_r": "0.4500",
            "trade_count": 180,
            "regime_expectancy": {"trending/high_vol": "0.6", "ranging/low_vol": "0.2"},
        },
        {
            "parameters": "reload=1|volume=300|pull=0.40|target=1.5|stop=1.0",
            "accepted": False,
            "rejection_reasons": ["sample_size: only 40 trades; minimum is 100 - a small sample is noise"],
            "composite": "0.9000",
            "win_rate": "0.7000",
            "profit_factor": "1.4000",
            "max_drawdown_r": "5.0000",
            "sortino": "0.8000",
            "expectancy_r": "0.2000",
            "trade_count": 40,
            "regime_expectancy": {},
        },
    ]
    (discovery_root / "candidates.jsonl").write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n",
        encoding="utf-8",
    )

    window = MainWindow(
        discovery_root=discovery_root,
        mode_supervisor=ModeSupervisor(tmp_path / "production_config.yaml"),
    )
    qtbot.addWidget(window)

    table = window.findChild(QTableWidget, "leaderboard_table")
    trace = window.findChild(QTextEdit, "candidate_trace_text")
    assert table is not None and trace is not None
    assert table.rowCount() == 2
    assert table.columnCount() == 9
    headers = [table.horizontalHeaderItem(index).text() for index in range(table.columnCount())]
    assert "Win rate" in headers
    assert "Sortino" in headers
    assert "Max DD (R)" in headers
    assert table.item(1, 8).text() == "NO"

    table.setCurrentCell(1, 0)
    text = trace.toPlainText()
    assert "Rejected because:" in text
    assert "sample_size" in text

    table.setCurrentCell(0, 0)
    text = trace.toPlainText()
    assert "cleared every gate" in text
    assert "trending/high_vol: 0.6" in text


def test_watchdog_reports_live_runtime_in_assistant_mode(qtbot: object, tmp_path: Path) -> None:
    """Without a prototype provider, the watchdog reflects the live Bookmap runtime."""
    from app.runtime.controller import RuntimeSnapshot

    runtime = RuntimeSnapshot(
        state="RECORDING_ONLY",
        mode="SHADOW",
        bookmap_status="connected",
        recording=True,
        exact_contract="MNQU6",
        contract_reason="current",
        source_mode="delayed",
        session_name="Closed",
        session_date="2026-07-13",
        minutes_since_open=None,
        minutes_until_close=None,
        regime="extreme/thin/unstable",
        profile_id="global_observe_only",
        profile_fallback="fell back to the global profile",
        profile_validation="unavailable",
        historical_sample_count=0,
        decisions_allowed=False,
        warmup_complete=True,
        sample_count=240,
        data_age_ms=None,
        dropped_message_count=24407,
        threshold_summary="DELAYED",
        shadow_decisions=0,
        report_root="data/reports",
        data_delay_minutes=15,
    )
    window = MainWindow(
        runtime_snapshot_provider=lambda: runtime,
        mode_supervisor=ModeSupervisor(tmp_path / "production_config.yaml"),
    )
    qtbot.addWidget(window)
    window.refresh_live_dashboard()

    watchdog = window.findChild(QLabel, "watchdog_status_label")
    assert watchdog is not None
    assert "Bookmap connected" in watchdog.text()
    assert "recording yes" in watchdog.text()
    assert "dropped 24407" in watchdog.text()


def test_price_chart_plots_live_market_price_in_assistant_mode(qtbot: object, tmp_path: Path) -> None:
    """Without a prototype feed, the chart follows the live dashboard price."""
    from decimal import Decimal

    from app.market.state import MarketState

    prices = [Decimal("29526.25")]

    def market_state() -> MarketState:
        state = MarketState()
        state = state.update(
            {
                "type": "depth_update",
                "timestamp": 1,
                "symbol": "MNQ",
                "side": "bid",
                "price": str(prices[0]),
                "previous_size": "0",
                "new_size": "10",
            },
        )
        return state.update(
            {
                "type": "depth_update",
                "timestamp": 2,
                "symbol": "MNQ",
                "side": "ask",
                "price": str(prices[0] + Decimal("0.50")),
                "previous_size": "0",
                "new_size": "8",
            },
        )

    window = MainWindow(
        market_state_provider=market_state,
        mode_supervisor=ModeSupervisor(tmp_path / "production_config.yaml"),
    )
    qtbot.addWidget(window)

    window.refresh_live_dashboard()
    prices[0] = Decimal("29530.00")
    window.refresh_live_dashboard()

    assert window.price_chart.point_count() == 2
