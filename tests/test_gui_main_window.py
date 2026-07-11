"""Smoke tests for the PySide6 main window."""

from __future__ import annotations

import os

os.environ.setdefault("QT_API", "pyside6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import QComboBox, QLabel, QTableWidget, QTabWidget, QTextEdit

from app.database.recorder import MarketEventRecorder
from app.gui.main_window import (
    LIVE_DISABLED_TOOLTIP,
    LIVE_MODE,
    MainWindow,
    OBSERVE_MODE,
    SIM_MODE,
)
from app.market.state import MarketState
from app.risk.limits import EntryLimitState, InstrumentConfig


@pytest.fixture
def window(qtbot: object) -> Iterator[MainWindow]:
    """Create and show the main window for offscreen GUI tests."""
    main_window = MainWindow()
    qtbot.addWidget(main_window)
    main_window.show()
    yield main_window


def test_main_window_renders_all_required_tabs(window: MainWindow) -> None:
    """The skeleton launches headlessly and renders all six requested screens."""
    tabs = window.findChild(QTabWidget, "main_tab_widget")

    assert tabs is not None
    assert tabs.count() == 6
    assert [tabs.tabText(index) for index in range(tabs.count())] == [
        "Live dashboard",
        "Decision explanation",
        "Order management",
        "Risk configuration",
        "Replay",
        "Model health",
    ]
    for object_name in (
        "live_dashboard_tab",
        "decision_explanation_tab",
        "order_management_tab",
        "risk_configuration_tab",
        "replay_tab",
        "model_health_tab",
    ):
        assert window.findChild(type(tabs.widget(0)), object_name) is not None


def test_mode_selector_defaults_to_observe_and_live_is_disabled(window: MainWindow) -> None:
    """The mode selector starts in OBSERVE and keeps LIVE greyed out."""
    selector = window.findChild(QComboBox, "mode_selector")

    assert selector is not None
    assert selector.currentText() == OBSERVE_MODE
    live_index = selector.findText(LIVE_MODE)
    model = selector.model()
    assert isinstance(model, QStandardItemModel)
    live_item = model.item(live_index)
    assert live_item is not None
    assert live_item.isEnabled() is False
    assert live_item.toolTip() == LIVE_DISABLED_TOOLTIP


def test_switching_to_sim_requires_confirmation(qtbot: object) -> None:
    """SIM mode changes only when the confirmation hook allows it."""
    prompts = 0

    def reject_sim() -> bool:
        nonlocal prompts
        prompts += 1
        return False

    window = MainWindow(confirm_simulator_mode=reject_sim)
    qtbot.addWidget(window)
    selector = window.findChild(QComboBox, "mode_selector")
    assert selector is not None

    selector.setCurrentText(SIM_MODE)

    assert prompts == 1
    assert window.current_mode == OBSERVE_MODE
    assert selector.currentText() == OBSERVE_MODE


def test_confirmed_sim_mode_updates_dashboard(qtbot: object) -> None:
    """Confirmed SIM mode updates the window state and dashboard mode label."""
    window = MainWindow(confirm_simulator_mode=lambda: True)
    qtbot.addWidget(window)
    selector = window.findChild(QComboBox, "mode_selector")
    assert selector is not None

    selector.setCurrentText(SIM_MODE)

    assert window.current_mode == SIM_MODE
    mode_label = window.findChild(QLabel, "dashboard_mode_value")
    assert mode_label is not None
    assert mode_label.text() == SIM_MODE


def test_live_dashboard_reads_market_state_and_risk_state(qtbot: object) -> None:
    """The live dashboard renders provider-backed receiver and risk state."""
    state = MarketState()
    for event in (
        _depth_event(timestamp=100, side="bid", price="100.00", previous_size="0", new_size="10"),
        _depth_event(timestamp=200, side="ask", price="100.25", previous_size="0", new_size="8"),
        {
            "timestamp_ns": 300,
            "price": "100.25",
            "size": "3",
            "aggressor_side": "buy",
            "instrument": "MNQ",
            "sequence_id": 1,
        },
    ):
        state = state.update(event)

    window = MainWindow(
        market_state_provider=lambda: state,
        risk_state_provider=lambda: _risk_state(),
    )
    qtbot.addWidget(window)

    feed_table = window.findChild(QTableWidget, "connection_status_table")
    losses_count = window.findChild(QLabel, "dashboard_losses_count")
    risk_used = window.findChild(QLabel, "dashboard_daily_risk_used")
    risk_remaining = window.findChild(QLabel, "dashboard_daily_risk_remaining")

    assert feed_table is not None
    assert feed_table.item(0, 1).text() == "receiving"
    assert feed_table.item(1, 1).text() == "receiving"
    assert feed_table.item(2, 1).text() == "receiving"
    assert losses_count is not None
    assert losses_count.text() == "2"
    assert risk_used is not None
    assert risk_used.text() == "$125.00"
    assert risk_remaining is not None
    assert risk_remaining.text() == "$375.00"


def test_replay_tab_lists_recorded_sessions_and_renders_parquet_events(
    qtbot: object,
    tmp_path: Path,
) -> None:
    """The replay tab lists data/raw date partitions and renders recorded depth/trade rows."""
    recorder = MarketEventRecorder(root_dir=tmp_path)
    timestamp_ns = _timestamp_ns(2026, 7, 10, 14, 30)
    recorder.record(
        {
            "type": "depth_update",
            "timestamp": timestamp_ns,
            "symbol": "MNQ",
            "side": "bid",
            "price": "100.00",
            "previous_size": "0",
            "new_size": "10",
        },
    )
    recorder.record(
        {
            "timestamp_ns": timestamp_ns + 1,
            "price": "100.25",
            "size": "3",
            "aggressor_side": "buy",
            "instrument": "MNQ",
            "sequence_id": 1,
        },
    )

    window = MainWindow(replay_data_root=tmp_path)
    qtbot.addWidget(window)

    picker = window.findChild(QComboBox, "replay_session_picker")
    table = window.findChild(QTableWidget, "replay_markers_table")
    explanation = window.findChild(QTextEdit, "replay_explanation_panel")

    assert picker is not None
    assert picker.currentText() == "2026-07-10"
    assert table is not None
    assert table.rowCount() == 2
    assert table.item(0, 1).text() == "depth"
    assert "MNQ bid 100.00" in table.item(0, 2).text()
    assert table.item(1, 1).text() == "trade"
    assert "MNQ buy 3 @ 100.25" in table.item(1, 2).text()
    assert explanation is not None
    assert "Depth updates: 1" in explanation.toPlainText()
    assert "Trades: 1" in explanation.toPlainText()


def _risk_state() -> EntryLimitState:
    return EntryLimitState(
        instrument_configs=(InstrumentConfig(symbol="MNQ"),),
        open_positions=1,
        losing_trades_today=2,
        entries_today=1,
        daily_realized_loss=Decimal("125"),
        daily_risk_budget=Decimal("500"),
        proposed_trade_risk=Decimal("20"),
        max_risk_per_trade=Decimal("30"),
        has_open_losing_position=False,
        adds_to_existing_position=False,
        stop_distance_ticks=Decimal("8"),
        connection_healthy=True,
        account_synchronized=True,
        unresolved_order_cancellation_pending=False,
        current_date=datetime(2026, 7, 10, tzinfo=UTC).date(),
    )


def _depth_event(
    *,
    timestamp: int,
    side: str,
    price: str,
    previous_size: str,
    new_size: str,
) -> dict[str, object]:
    return {
        "type": "depth_update",
        "timestamp": timestamp,
        "symbol": "MNQ",
        "side": side,
        "price": price,
        "previous_size": previous_size,
        "new_size": new_size,
    }


def _timestamp_ns(year: int, month: int, day: int, hour: int, minute: int) -> int:
    timestamp = datetime(year, month, day, hour, minute, tzinfo=UTC)
    return int(timestamp.timestamp()) * 1_000_000_000
