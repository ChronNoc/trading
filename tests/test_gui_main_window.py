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

from app.database.recorder import MarketEventRecorder, MarketSessionRecorder
from app.gui.main_window import (
    LIVE_DISABLED_TOOLTIP,
    LIVE_MODE,
    MainWindow,
    OBSERVE_MODE,
    SIM_MODE,
)
from app.market.state import MarketState
from app.risk.limits import EntryLimitState, InstrumentConfig
from app.runtime.controller import RuntimeSnapshot


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
    assert tabs.count() == 12
    assert [tabs.tabText(index) for index in range(tabs.count())] == [
        "Live dashboard",
        "Decision explanation",
        "Order management",
        "Risk configuration",
        "Replay",
        "Model health",
        "Ask",
        "Session review",
        "Automations",
        "Decision log",
        "Leaderboard",
        "Paper trading",
    ]
    for object_name in (
        "live_dashboard_tab",
        "decision_explanation_tab",
        "order_management_tab",
        "risk_configuration_tab",
        "replay_tab",
        "model_health_tab",
        "ask_tab",
        "session_review_tab",
        "automations_tab",
        "decision_log_tab",
        "leaderboard_tab",
        "paper_trading_tab",
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
    market_price = window.findChild(QLabel, "dashboard_market_price")
    bid_ask = window.findChild(QLabel, "dashboard_bid_ask")
    spread = window.findChild(QLabel, "dashboard_spread")

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
    assert market_price is not None
    assert market_price.text() == "100.125"
    assert bid_ask is not None
    assert bid_ask.text() == "100.00 / 100.25"
    assert spread is not None
    assert spread.text() == "0.25"

    state = state.update(_depth_event(timestamp=400, side="bid", price="100.25", previous_size="0", new_size="10"))
    state = state.update(_depth_event(timestamp=500, side="ask", price="100.25", previous_size="8", new_size="0"))
    state = state.update(_depth_event(timestamp=600, side="ask", price="100.50", previous_size="0", new_size="8"))
    window.refresh_live_dashboard()

    assert market_price.text() == "100.375"
    assert bid_ask.text() == "100.25 / 100.50"
    assert spread.text() == "0.25"


def test_live_dashboard_renders_runtime_automation_snapshot(qtbot: object) -> None:
    """The live dashboard surfaces automatic runtime status from the controller."""
    runtime = RuntimeSnapshot(
        state="SHADOW_READY",
        mode="SHADOW",
        bookmap_status="connected",
        recording=True,
        exact_contract="MNQU6",
        contract_reason="current",
        source_mode="delayed",
        data_delay_minutes=15,
        session_name="New York open",
        session_date="2026-07-10",
        minutes_since_open=10,
        minutes_until_close=110,
        regime="normal/normal/trending",
        profile_id="ny_open_normal_trend",
        profile_fallback="matched session, volatility, liquidity, and behavior",
        profile_validation="validated",
        historical_sample_count=120,
        decisions_allowed=True,
        warmup_complete=True,
        sample_count=42,
        data_age_ms=12,
        dropped_message_count=0,
        current_session_dropped_message_count=0,
        threshold_summary="historical thresholds",
        shadow_decisions=3,
        report_root="data/reports",
    )
    window = MainWindow(runtime_snapshot_provider=lambda: runtime)
    qtbot.addWidget(window)

    state_label = window.findChild(QLabel, "runtime_state_value")
    contract_label = window.findChild(QLabel, "runtime_exact_contract")
    profile_label = window.findChild(QLabel, "runtime_profile_id")
    samples_label = window.findChild(QLabel, "runtime_sample_count")
    delay_label = window.findChild(QLabel, "runtime_data_delay")

    assert state_label is not None
    assert state_label.text() == "SHADOW_READY"
    assert contract_label is not None
    assert contract_label.text() == "MNQU6"
    assert profile_label is not None
    assert profile_label.text() == "ny_open_normal_trend"
    assert samples_label is not None
    assert samples_label.text() == "42 / complete"
    assert delay_label is not None
    assert delay_label.text() == "15 minutes delayed"


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


def test_replay_tab_lists_session_partition_recordings(qtbot: object, tmp_path: Path) -> None:
    """The replay tab discovers Java bridge session directories under data/raw/{date}/."""
    timestamp_ns = _timestamp_ns(2026, 7, 10, 14, 30)
    recorder = MarketSessionRecorder(
        root_dir=tmp_path,
        session_start_utc=datetime(2026, 7, 10, 14, 30, tzinfo=UTC),
    )
    recorder.record(
        {
            "type": "depth_update",
            "timestamp": timestamp_ns,
            "symbol": "MNQ",
            "side": "ask",
            "price": "100.25",
            "previous_size": "0",
            "new_size": "8",
        },
    )
    recorder.record_control_event({"type": "session_ended", "timestamp_ns": timestamp_ns + 1})

    window = MainWindow(replay_data_root=tmp_path)
    qtbot.addWidget(window)

    picker = window.findChild(QComboBox, "replay_session_picker")
    table = window.findChild(QTableWidget, "replay_markers_table")

    assert picker is not None
    assert picker.currentText() == "2026-07-10/session_20260710T143000Z"
    assert table is not None
    assert table.rowCount() == 1
    assert table.item(0, 1).text() == "depth"


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


def test_agent_panels_narrate_and_track_decision_history(qtbot: object) -> None:
    """The narrator, condition matrix, timeline, and watchdog follow prototype decisions."""
    from dataclasses import replace

    from PySide6.QtWidgets import QListWidget

    from app.prototype.scenarios import empty_prototype_dashboard_snapshot

    snapshots = [
        replace(
            empty_prototype_dashboard_snapshot(),
            runtime_state="playing",
            synthetic_status="connected",
            depth_events=10,
            trade_events=4,
            current_setup="Clean long absorption reclaim",
            decision="ACCEPTED",
            explanations=(
                "[pass] Price at overnight_low",
                "[pass] Bid liquidity reloaded 2 times",
                "[pass] Reclaim confirmation completed",
            ),
        ),
    ]
    window = MainWindow(prototype_snapshot_provider=lambda: snapshots[0])
    qtbot.addWidget(window)

    window.refresh_live_dashboard()
    snapshots[0] = replace(
        snapshots[0],
        current_setup="Rejected lookalike",
        decision="REJECTED",
        explanations=(
            "[pass] Price at overnight_low",
            "[fail] Bid liquidity did not reload",
            "[fail] Reclaim confirmation not completed",
        ),
    )
    window.refresh_live_dashboard()

    narrator = window.findChild(QTextEdit, "ai_narrator_text")
    timeline = window.findChild(QListWidget, "decision_timeline_list")
    matrix = window.findChild(QTableWidget, "condition_matrix_table")
    watchdog = window.findChild(QLabel, "watchdog_status_label")

    assert narrator is not None
    assert "REJECTED" in narrator.toPlainText()
    assert "nobody reloaded the bid" in narrator.toPlainText()
    assert timeline is not None
    assert timeline.count() == 2
    assert "ACCEPTED" in timeline.item(0).text()
    assert "REJECTED" in timeline.item(1).text()
    assert matrix is not None
    assert matrix.columnCount() == 2
    assert matrix.rowCount() >= 3
    assert watchdog is not None
    assert "feeds healthy" in watchdog.text()


def test_ask_tab_answers_from_snapshot_and_history(qtbot: object) -> None:
    """The ask tab answers counter questions from the snapshot without guessing."""
    from dataclasses import replace

    from PySide6.QtWidgets import QLineEdit, QPushButton

    from app.prototype.scenarios import empty_prototype_dashboard_snapshot

    snapshot = replace(
        empty_prototype_dashboard_snapshot(),
        depth_events=120,
        trade_events=34,
        control_events=6,
    )
    window = MainWindow(prototype_snapshot_provider=lambda: snapshot)
    qtbot.addWidget(window)

    question_input = window.findChild(QLineEdit, "ask_input")
    send_button = window.findChild(QPushButton, "ask_send_button")
    conversation = window.findChild(QTextEdit, "ask_conversation")

    assert question_input is not None
    assert send_button is not None
    assert conversation is not None
    question_input.setText("how many events so far?")
    send_button.click()

    text = conversation.toPlainText()
    assert "how many events so far?" in text
    assert "120 depth" in text
    assert "34 trade" in text
    assert question_input.text() == ""


def test_session_review_tab_writes_markdown_report(qtbot: object, tmp_path: Path) -> None:
    """Generating a review writes a markdown file and renders it in the tab."""
    from dataclasses import replace

    from PySide6.QtWidgets import QPushButton

    from app.prototype.scenarios import empty_prototype_dashboard_snapshot

    snapshots = [
        replace(
            empty_prototype_dashboard_snapshot(),
            runtime_state="playing",
            synthetic_status="connected",
            current_setup="Rejected lookalike",
            decision="REJECTED",
            explanations=(
                "[pass] Price at overnight_low",
                "[fail] Bid liquidity did not reload",
            ),
        ),
    ]
    window = MainWindow(
        prototype_snapshot_provider=lambda: snapshots[0],
        review_output_root=tmp_path,
    )
    qtbot.addWidget(window)
    window.refresh_live_dashboard()

    button = window.findChild(QPushButton, "session_review_button")
    review_text = window.findChild(QTextEdit, "session_review_text")
    path_label = window.findChild(QLabel, "session_review_path")

    assert button is not None
    button.click()

    written = sorted(tmp_path.rglob("ai_review_*.md"))
    assert len(written) == 1
    assert review_text is not None
    assert "Decisions recorded: 1" in review_text.toPlainText()
    assert path_label is not None
    assert path_label.text() == str(written[0])


def test_session_review_without_decisions_explains_instead_of_writing(
    qtbot: object, tmp_path: Path
) -> None:
    """With no recorded decisions the review tab explains and writes nothing."""
    from PySide6.QtWidgets import QPushButton

    window = MainWindow(review_output_root=tmp_path)
    qtbot.addWidget(window)

    button = window.findChild(QPushButton, "session_review_button")
    review_text = window.findChild(QTextEdit, "session_review_text")

    assert button is not None
    button.click()

    assert review_text is not None
    assert "No decisions recorded yet" in review_text.toPlainText()
    assert not list(tmp_path.rglob("*.md"))


def test_order_management_controls_disabled_without_broker(window: MainWindow) -> None:
    """Flatten/Cancel-all/Disable-automation must not look operational (no broker path)."""
    from PySide6.QtWidgets import QPushButton

    for object_name in ("flatten_button", "cancel_all_button", "disable_automation_button"):
        button = window.findChild(QPushButton, object_name)
        assert button is not None
        assert button.isEnabled() is False


def test_paper_trading_r_column_is_rounded(qtbot: object, tmp_path: Path) -> None:
    """The paper-trading R column shows a rounded value, not a long Decimal."""
    import json as _json

    from PySide6.QtWidgets import QPushButton, QTableWidget

    from app.discovery.supervisor import ModeSupervisor

    processed = tmp_path / "processed"
    processed.mkdir()
    base_ns = 1_752_537_751_000_000_000
    outcome = {
        "session_id": "session_20260715T002231Z",
        "setup_id": "abs-reclaim-long-0001",
        "provenance": "REAL_DELAYED",
        "direction": "long",
        "trading_day": "2026-07-15",
        "decision_ts_ns": base_ns,
        "entry_ts_ns": base_ns + 1_000_000_000,
        "exit_ts_ns": base_ns + 60_000_000_000,
        "defended_price": "29450.00",
        "entry_reference_price": "29451.00",
        "entry": "29451.25",
        "stop": "29441.25",
        "target": "29471.25",
        "exit_reference_price": "29471.25",
        "exit": "29471.00",
        "commission": "1.24",
        "slippage_cost": "1.00",
        "gross_pnl_per_contract": "40.50",
        "net_pnl_per_contract": "38.26",
        "risk_per_contract": "22.24",
        "r_multiple": "1.4481242038012959",
        "outcome": "target_first",
        "strategy_version": "order-flow-plan-v1",
        "builder_version": "real-episodes-v2",
        "source_event_range": [1, 200],
        "decision_hash": "d" * 64,
        "input_hash": "i" * 64,
        "ordering_mode": "receive_sequence",
        "ordering_ambiguous": False,
        "data_quality_ok": True,
        "eligible_for_ledger": True,
        "decision": "accepted",
        "strategy_accepted": True,
    }
    (processed / "outcomes.episodes.jsonl").write_text(_json.dumps(outcome) + "\n", encoding="utf-8")

    win = MainWindow(processed_root=processed, mode_supervisor=ModeSupervisor(tmp_path / "prod.yaml"))
    qtbot.addWidget(win)
    win.findChild(QPushButton, "paper_run_button").click()

    table = win.findChild(QTableWidget, "paper_trades_table")
    assert table is not None and table.rowCount() > 0
    # R column is index 11 in the full lineage/economics ledger.
    r_text = table.item(0, 11).text()
    assert "." in r_text and len(r_text.split(".")[-1]) <= 2
