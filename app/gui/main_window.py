"""PySide6 desktop skeleton for the MNQ trading assistant."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import cast

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QStandardItemModel
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.agents import AI_COMMENTARY_LABEL
from app.agents.ask import answer_question
from app.agents.narrator import narrate_decision
from app.agents.records import DecisionRecord, canonical_condition_name, parse_explanation_lines
from app.agents.session_reviewer import write_review
from app.agents.watchdog import SEVERITY_OK, SEVERITY_WARNING, Watchdog, WatchdogReport
from app.discovery.metrics import TradeResult
from app.discovery.paper_account import PaperAccountConfig, run_single_account, single_account_summary
from app.discovery.supervisor import ModeSupervisor
from app.gui.automations import AUTOMATION_DEFINITIONS, AutomationEngine
from app.gui.price_chart import PriceChartWidget
from app.gui.theme import (
    APP_STYLESHEET,
    AUTOMATION_BANNER_STYLE,
    BANNER_STYLE,
    COACH_STYLE,
    WATCHDOG_INFO_STYLE,
    WATCHDOG_OK_STYLE,
    WATCHDOG_WARNING_STYLE,
)
from app.market.receiver import get_current_market_state
from app.market.state import MarketState
from app.prototype.scenarios import PrototypeDashboardSnapshot, empty_prototype_dashboard_snapshot
from app.risk.limits import EntryLimitState
from app.runtime.controller import RuntimeSnapshot
from app.strategy.setups import SetupConditionResult, SetupEvaluationResult


OBSERVE_MODE = "OBSERVE"
SIM_MODE = "SIM"
LIVE_MODE = "LIVE"
LIVE_DISABLED_TOOLTIP = "enabled after Stage F gate"


@dataclass(frozen=True, slots=True)
class FeedStatus:
    """Connection status for one incoming market-data feed."""

    name: str
    status: str


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    """Dashboard state displayed by the live dashboard tab."""

    feeds: tuple[FeedStatus, ...]
    instrument: str
    session: str
    market_price: str
    bid_ask: str
    spread: str
    mode: str
    daily_risk_used: Decimal
    daily_risk_remaining: Decimal
    losses_count: int
    setup_name: str
    confidence: Decimal
    setup_status: str


@dataclass(frozen=True, slots=True)
class OrderManagementSnapshot:
    """Mock order-management state displayed by the order tab."""

    entry_order: str
    filled_qty: int
    average_fill: Decimal | None
    stop: Decimal | None
    target: Decimal | None
    unrealized_pnl: Decimal
    remaining_risk: Decimal


@dataclass(frozen=True, slots=True)
class RiskConfigurationSnapshot:
    """Mock editable risk configuration displayed by the risk tab."""

    trade_open: bool
    account_risk_base: Decimal
    daily_risk_percent: Decimal
    max_trades: int
    max_losses: int
    max_contracts: int
    max_stop_ticks: int
    slippage_allowance: Decimal
    commission_estimate: Decimal
    session_hours: str
    news_lockout_enabled: bool
    consecutive_loss_lock_enabled: bool


@dataclass(frozen=True, slots=True)
class ReplayMarker:
    """Replay row for a recorded setup, trade, or depth event."""

    timestamp: str
    event_type: str
    description: str


@dataclass(frozen=True, slots=True)
class ReplaySnapshot:
    """Replay state displayed by the replay tab."""

    sessions: tuple[str, ...]
    selected_session: str
    markers: tuple[ReplayMarker, ...]
    explanation: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModelHealthSnapshot:
    """Mock model-health state displayed by the model health tab."""

    model_version: str
    training_period: str
    testing_period: str
    setup_count: int
    prediction_distribution: str
    calibration: str
    drift: str
    last_retrain_date: str


@dataclass(frozen=True, slots=True)
class MockGuiData:
    """Container for all placeholder data rendered by the GUI skeleton."""

    dashboard: DashboardSnapshot
    decision: SetupEvaluationResult
    order: OrderManagementSnapshot
    risk: RiskConfigurationSnapshot
    replay: ReplaySnapshot
    model_health: ModelHealthSnapshot


@dataclass(frozen=True, slots=True)
class ReplaySession:
    """Recorded session metadata discovered under data/raw."""

    label: str
    depth_path: Path | None
    trades_path: Path | None


@dataclass(frozen=True, slots=True)
class ReplayLoadResult:
    """Loaded replay rows and summary text for one recorded session."""

    markers: tuple[ReplayMarker, ...]
    explanation: tuple[str, ...]


class MainWindow(QMainWindow):
    """Main PySide6 window for the MNQ trading assistant skeleton."""

    def __init__(
        self,
        *,
        mock_data: MockGuiData | None = None,
        market_state_provider: Callable[[], MarketState] | None = None,
        risk_state_provider: Callable[[], EntryLimitState | None] | None = None,
        runtime_snapshot_provider: Callable[[], RuntimeSnapshot] | None = None,
        prototype_snapshot_provider: Callable[[], PrototypeDashboardSnapshot] | None = None,
        prototype_control_handler: Callable[[str], None] | None = None,
        replay_data_root: str | Path = Path("data/raw"),
        confirm_simulator_mode: Callable[[], bool] | None = None,
        review_output_root: str | Path = Path("data/prototype/reports"),
        automation_output_root: str | Path = Path("data/prototype"),
        discovery_root: str | Path = Path("data/discovery"),
        mode_supervisor: ModeSupervisor | None = None,
        stall_clock: Callable[[], float] | None = None,
        stall_after_seconds: float = 20.0,
    ) -> None:
        """Initialize the main window with live data providers and mode controls."""
        super().__init__()
        self.setWindowTitle("MNQ Order-Flow Trading Assistant")
        self.resize(1180, 760)

        self._mock_data = mock_data or create_mock_gui_data()
        self._market_state_provider = market_state_provider or get_current_market_state
        self._risk_state_provider = risk_state_provider or (lambda: None)
        self._runtime_snapshot_provider = runtime_snapshot_provider
        self._prototype_snapshot_provider = prototype_snapshot_provider
        self._prototype_control_handler = prototype_control_handler
        self._replay_data_root = Path(replay_data_root)
        self._confirm_simulator_mode = confirm_simulator_mode
        self._review_output_root = Path(review_output_root)
        self._current_mode = OBSERVE_MODE
        self._reverting_mode = False
        self._decision_history: list[DecisionRecord] = []
        self._watchdog = Watchdog()
        self._last_watchdog_report: WatchdogReport | None = None
        self._stall_clock = stall_clock or time.monotonic
        self._stall_after_seconds = stall_after_seconds
        self._last_live_price: str | None = None
        self._last_live_price_change_ts: float | None = None
        self._automation_engine = AutomationEngine(output_root=Path(automation_output_root))
        self._discovery_root = Path(discovery_root)
        self._mode_supervisor = mode_supervisor or ModeSupervisor()
        self.setStyleSheet(APP_STYLESHEET)

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(self._build_header())
        layout.addWidget(self._build_mode_banner())

        self.tabs = QTabWidget()
        self.tabs.setObjectName("main_tab_widget")
        self.tabs.addTab(self._build_live_dashboard_tab(), "Live dashboard")
        self.tabs.addTab(self._build_decision_explanation_tab(), "Decision explanation")
        self.tabs.addTab(self._build_order_management_tab(), "Order management")
        self.tabs.addTab(self._build_risk_configuration_tab(), "Risk configuration")
        self.tabs.addTab(self._build_replay_tab(), "Replay")
        self.tabs.addTab(self._build_model_health_tab(), "Model health")
        self.tabs.addTab(self._build_ask_tab(), "Ask")
        self.tabs.addTab(self._build_session_review_tab(), "Session review")
        self.tabs.addTab(self._build_automations_tab(), "Automations")
        self.tabs.addTab(self._build_decision_log_tab(), "Decision log")
        self.tabs.addTab(self._build_leaderboard_tab(), "Leaderboard")
        self.tabs.addTab(self._build_paper_trading_tab(), "Paper trading")
        layout.addWidget(self.tabs)

        self.setCentralWidget(root)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(1000)
        self._refresh_timer.timeout.connect(self.refresh_live_dashboard)
        if self._runtime_snapshot_provider is not None or self._prototype_snapshot_provider is not None:
            self._refresh_timer.start()

    @property
    def current_mode(self) -> str:
        """Return the selected operating mode."""
        return self._current_mode

    def _build_header(self) -> QWidget:
        header = QWidget()
        layout = QHBoxLayout(header)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        title = QLabel("MNQ Order-Flow Trading Assistant")
        title.setObjectName("window_title")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")

        mode_label = QLabel("Mode")
        self.mode_selector = QComboBox()
        self.mode_selector.setObjectName("mode_selector")
        self.mode_selector.addItems([OBSERVE_MODE, SIM_MODE, LIVE_MODE])
        self.mode_selector.setCurrentText(OBSERVE_MODE)
        self._disable_live_mode_item()
        self.mode_selector.currentTextChanged.connect(self._handle_mode_change)

        watchdog_status = _named_label("watchdog_status_label", "Watchdog: idle")
        watchdog_status.setStyleSheet(WATCHDOG_INFO_STYLE)

        automation_banner = _named_label("automation_banner_label", "")
        automation_banner.setStyleSheet(AUTOMATION_BANNER_STYLE)
        automation_banner.setVisible(False)

        layout.addWidget(title)
        layout.addWidget(watchdog_status)
        layout.addWidget(automation_banner)
        layout.addStretch(1)
        layout.addWidget(mode_label)
        layout.addWidget(self.mode_selector)
        return header

    def _disable_live_mode_item(self) -> None:
        live_index = self.mode_selector.findText(LIVE_MODE)
        model = cast(QStandardItemModel, self.mode_selector.model())
        live_item = model.item(live_index)
        live_item.setEnabled(False)
        live_item.setToolTip(LIVE_DISABLED_TOOLTIP)
        self.mode_selector.setItemData(live_index, LIVE_DISABLED_TOOLTIP, Qt.ItemDataRole.ToolTipRole)

    def _handle_mode_change(self, selected_mode: str) -> None:
        if self._reverting_mode or selected_mode == self._current_mode:
            return

        if selected_mode == SIM_MODE:
            if self._request_sim_mode_confirmation():
                self._current_mode = SIM_MODE
                self._update_mode_labels()
                return
            self._revert_mode_selector()
            return

        if selected_mode == OBSERVE_MODE:
            self._current_mode = OBSERVE_MODE
            self._update_mode_labels()
            return

        self._revert_mode_selector()

    def _request_sim_mode_confirmation(self) -> bool:
        if self._confirm_simulator_mode is not None:
            return self._confirm_simulator_mode()

        answer = QMessageBox.question(
            self,
            "Switch to SIM mode",
            "Switch from OBSERVE to SIM mode?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _revert_mode_selector(self) -> None:
        self._reverting_mode = True
        self.mode_selector.setCurrentText(self._current_mode)
        self._reverting_mode = False

    def _update_mode_labels(self) -> None:
        mode_value = self.findChild(QLabel, "dashboard_mode_value")
        if mode_value is not None:
            mode_value.setText(self._current_mode)

    def _build_live_dashboard_tab(self) -> QWidget:
        data = self._current_dashboard_snapshot()
        tab = QWidget()
        tab.setObjectName("live_dashboard_tab")
        layout = QGridLayout(tab)
        layout.setSpacing(12)

        feed_table = QTableWidget(len(data.feeds), 2)
        feed_table.setObjectName("connection_status_table")
        feed_table.setHorizontalHeaderLabels(["Feed", "Status"])
        for row, feed in enumerate(data.feeds):
            feed_table.setItem(row, 0, _read_only_item(feed.name))
            feed_table.setItem(row, 1, _read_only_item(feed.status))
        feed_table.horizontalHeader().setStretchLastSection(True)

        layout.addWidget(_group("Connection status per feed", feed_table), 0, 0)
        layout.addWidget(
            _group(
                "Session",
                _form_widget(
                    (
                        ("Instrument", _named_label("dashboard_instrument", data.instrument)),
                        ("Session", _named_label("dashboard_session", data.session)),
                        ("Market price", _named_label("dashboard_market_price", data.market_price)),
                        ("Bid / Ask", _named_label("dashboard_bid_ask", data.bid_ask)),
                        ("Spread", _named_label("dashboard_spread", data.spread)),
                        ("Mode", _named_label("dashboard_mode_value", data.mode)),
                        ("Losses count", _named_label("dashboard_losses_count", str(data.losses_count))),
                    ),
                ),
            ),
            0,
            1,
        )
        layout.addWidget(
            _group(
                "Daily risk",
                _form_widget(
                    (
                        ("Used", _named_label("dashboard_daily_risk_used", _format_money(data.daily_risk_used))),
                        (
                            "Remaining",
                            _named_label("dashboard_daily_risk_remaining", _format_money(data.daily_risk_remaining)),
                        ),
                    ),
                ),
            ),
            1,
            0,
        )
        layout.addWidget(
            _group(
                "Current setup",
                _form_widget(
                    (
                        ("Setup", _named_label("dashboard_setup_name", data.setup_name)),
                        ("Confidence", _named_label("dashboard_setup_confidence", f"{data.confidence}%")),
                        ("Status", _named_label("dashboard_setup_status", data.setup_status)),
                    ),
                ),
            ),
            1,
            1,
        )
        runtime = self._current_runtime_snapshot()
        start_stop = QWidget()
        start_stop_layout = QHBoxLayout(start_stop)
        start_stop_layout.setContentsMargins(0, 0, 0, 0)
        self.start_assistant_button = QPushButton("Start assistant")
        self.start_assistant_button.setObjectName("start_assistant_button")
        self.start_assistant_button.setEnabled(False)
        self.start_assistant_button.setToolTip("Automatic runtime is started by start_mnq_assistant.bat.")
        self.stop_assistant_button = QPushButton("Stop assistant")
        self.stop_assistant_button.setObjectName("stop_assistant_button")
        self.stop_assistant_button.setEnabled(False)
        self.stop_assistant_button.setToolTip("Close the window or stop the launcher console to stop the runtime.")
        start_stop_layout.addWidget(self.start_assistant_button)
        start_stop_layout.addWidget(self.stop_assistant_button)
        start_stop_layout.addStretch(1)
        layout.addWidget(
            _group(
                "Assistant automation",
                _form_widget(
                    (
                        ("Controls", start_stop),
                        ("Runtime state", _named_label("runtime_state_value", _runtime_text(runtime, "state"))),
                        ("Runtime mode", _named_label("runtime_mode_value", _runtime_text(runtime, "mode"))),
                        ("Bookmap", _named_label("runtime_bookmap_status", _runtime_text(runtime, "bookmap_status"))),
                        ("Recording", _named_label("runtime_recording_status", _runtime_bool_text(runtime, "recording"))),
                        ("Contract", _named_label("runtime_exact_contract", _runtime_text(runtime, "exact_contract"))),
                        ("Source mode", _named_label("runtime_source_mode", _runtime_text(runtime, "source_mode"))),
                        ("Data delay", _named_label("runtime_data_delay", _runtime_delay_text(runtime))),
                        ("Session", _named_label("runtime_session_name", _runtime_text(runtime, "session_name"))),
                        ("Minutes", _named_label("runtime_session_minutes", _runtime_minutes_text(runtime))),
                        ("Regime", _named_label("runtime_regime", _runtime_text(runtime, "regime"))),
                        ("Profile", _named_label("runtime_profile_id", _runtime_text(runtime, "profile_id"))),
                        ("Validation", _named_label("runtime_profile_validation", _runtime_text(runtime, "profile_validation"))),
                        (
                            "Historical samples",
                            _named_label("runtime_historical_sample_count", _runtime_int_text(runtime, "historical_sample_count")),
                        ),
                        ("Fallback", _named_label("runtime_profile_fallback", _runtime_text(runtime, "profile_fallback"))),
                        ("Decisions", _named_label("runtime_decisions_allowed", _runtime_bool_text(runtime, "decisions_allowed"))),
                        ("Thresholds", _named_label("runtime_threshold_summary", _runtime_text(runtime, "threshold_summary"))),
                        ("Warm-up samples", _named_label("runtime_sample_count", _runtime_sample_text(runtime))),
                        ("Data age", _named_label("runtime_data_age", _runtime_data_age_text(runtime))),
                        ("Dropped", _named_label("runtime_dropped_messages", _runtime_int_text(runtime, "dropped_message_count"))),
                        ("Shadow decisions", _named_label("runtime_shadow_decisions", _runtime_int_text(runtime, "shadow_decisions"))),
                        ("Reports", _named_label("runtime_report_root", _runtime_text(runtime, "report_root"))),
                    ),
                ),
            ),
            2,
            0,
            1,
            2,
        )
        layout.addWidget(
            _group(
                "Prototype feed",
                self._build_prototype_panel(),
            ),
            3,
            0,
            1,
            2,
        )
        self.price_chart = PriceChartWidget()
        self.price_chart.setObjectName("price_chart_widget")
        layout.addWidget(
            _group("Price path (green=accepted / red=rejected)", self.price_chart),
            4,
            0,
            1,
            2,
        )
        refresh_button = QPushButton("Refresh")
        refresh_button.setObjectName("live_dashboard_refresh_button")
        refresh_button.clicked.connect(self.refresh_live_dashboard)
        layout.addWidget(refresh_button, 5, 1, alignment=Qt.AlignmentFlag.AlignRight)
        return tab

    def refresh_live_dashboard(self) -> None:
        """Refresh dashboard labels from the current market and risk providers."""
        data = self._current_dashboard_snapshot()
        mode_value = self.findChild(QLabel, "dashboard_mode_value")
        losses_count = self.findChild(QLabel, "dashboard_losses_count")
        daily_risk_used = self.findChild(QLabel, "dashboard_daily_risk_used")
        daily_risk_remaining = self.findChild(QLabel, "dashboard_daily_risk_remaining")
        dashboard_values = {
            "dashboard_instrument": data.instrument,
            "dashboard_session": data.session,
            "dashboard_market_price": data.market_price,
            "dashboard_bid_ask": data.bid_ask,
            "dashboard_spread": data.spread,
            "dashboard_setup_name": data.setup_name,
            "dashboard_setup_confidence": f"{data.confidence}%",
            "dashboard_setup_status": data.setup_status,
        }
        if mode_value is not None:
            mode_value.setText(data.mode)
        if losses_count is not None:
            losses_count.setText(str(data.losses_count))
        if daily_risk_used is not None:
            daily_risk_used.setText(_format_money(data.daily_risk_used))
        if daily_risk_remaining is not None:
            daily_risk_remaining.setText(_format_money(data.daily_risk_remaining))
        for object_name, text in dashboard_values.items():
            label = self.findChild(QLabel, object_name)
            if label is not None:
                label.setText(text)
        feed_table = self.findChild(QTableWidget, "connection_status_table")
        if feed_table is not None:
            feed_table.setRowCount(len(data.feeds))
            for row, feed in enumerate(data.feeds):
                feed_table.setItem(row, 0, _read_only_item(feed.name))
                feed_table.setItem(row, 1, _read_only_item(feed.status))
        self._refresh_runtime_labels()
        self._refresh_prototype_labels()
        self._refresh_price_chart()
        self._refresh_decision_explanation()
        self._refresh_watchdog()
        self._refresh_mode_banner()
        self._refresh_decision_log()
        self._run_automations()

    def _refresh_price_chart(self) -> None:
        """Feed the price-path chart from prototype or live market prices."""
        chart = getattr(self, "price_chart", None)
        if chart is None:
            return
        if self._prototype_snapshot_provider is not None:
            snapshot = self._current_prototype_snapshot()
            if snapshot.last_trade_price:
                chart.add_price(snapshot.last_trade_price)
            return
        # Live assistant mode: plot the dashboard market price so the chart
        # is never a dead panel while real data flows.
        chart.add_price(self._current_dashboard_snapshot().market_price)

    def _build_prototype_panel(self) -> QWidget:
        snapshot = self._current_prototype_snapshot()
        banner = _named_label("prototype_banner_label", snapshot.banner)
        banner.setStyleSheet(BANNER_STYLE)
        controls = self._build_prototype_controls(snapshot)
        explanations = QTextEdit()
        explanations.setObjectName("prototype_explanations")
        explanations.setReadOnly(True)
        explanations.setMaximumHeight(86)
        explanations.setPlainText("\n".join(snapshot.explanations))
        return _form_widget(
            (
                ("Warning", banner),
                ("Controls", controls),
                ("Runtime state", _named_label("prototype_runtime_state", snapshot.runtime_state)),
                ("Synthetic status", _named_label("prototype_synthetic_status", snapshot.synthetic_status)),
                ("Instrument", _named_label("prototype_instrument", snapshot.instrument)),
                ("Source mode", _named_label("prototype_source_mode", snapshot.source_mode)),
                ("Scenario", _named_label("prototype_scenario", snapshot.scenario)),
                ("Session", _named_label("prototype_session", snapshot.session)),
                ("Regime", _named_label("prototype_regime", snapshot.regime)),
                ("Profile", _named_label("prototype_profile", snapshot.profile)),
                ("Warm-up", _named_label("prototype_warmup", snapshot.warmup)),
                ("Event counters", _named_label("prototype_event_counters", _prototype_counters(snapshot))),
                ("Current setup", _named_label("prototype_current_setup", snapshot.current_setup)),
                ("Decision", _named_label("prototype_decision", snapshot.decision)),
                ("Raw features", _named_label("prototype_raw_features", snapshot.raw_features)),
                ("Normalized features", _named_label("prototype_normalized_features", snapshot.normalized_features)),
                ("Shadow order", _named_label("prototype_shadow_order", _prototype_shadow_order(snapshot))),
                ("Report path", _named_label("prototype_report_path", snapshot.report_path)),
                ("Explanation", explanations),
            ),
        )

    def _build_prototype_controls(self, snapshot: PrototypeDashboardSnapshot) -> QWidget:
        controls = QWidget()
        layout = QHBoxLayout(controls)
        layout.setContentsMargins(0, 0, 0, 0)
        for object_name, label, command in (
            ("prototype_pause_button", "Pause", "pause"),
            ("prototype_resume_button", "Resume", "resume"),
            ("prototype_restart_button", "Restart", "restart"),
            ("prototype_jump_clean_button", "Jump to clean", "jump_clean"),
            ("prototype_jump_rejected_button", "Jump to rejected", "jump_rejected"),
        ):
            button = QPushButton(label)
            button.setObjectName(object_name)
            button.clicked.connect(lambda _checked=False, item=command: self._handle_prototype_command(item))
            layout.addWidget(button)
        speed_selector = QComboBox()
        speed_selector.setObjectName("prototype_speed_selector")
        speed_selector.addItems(["1", "2", "5", "10"])
        speed_selector.setCurrentText(str(snapshot.playback_speed))
        speed_selector.currentTextChanged.connect(lambda value: self._handle_prototype_command(f"speed:{value}"))
        layout.addWidget(speed_selector)
        layout.addStretch(1)
        return controls

    def _handle_prototype_command(self, command: str) -> None:
        if self._prototype_control_handler is not None:
            self._prototype_control_handler(command)
        self._refresh_prototype_labels()

    def _current_prototype_snapshot(self) -> PrototypeDashboardSnapshot:
        if self._prototype_snapshot_provider is None:
            return empty_prototype_dashboard_snapshot()
        return self._prototype_snapshot_provider()

    def _refresh_prototype_labels(self) -> None:
        snapshot = self._current_prototype_snapshot()
        values = {
            "prototype_banner_label": snapshot.banner,
            "prototype_runtime_state": snapshot.runtime_state,
            "prototype_synthetic_status": snapshot.synthetic_status,
            "prototype_instrument": snapshot.instrument,
            "prototype_source_mode": snapshot.source_mode,
            "prototype_scenario": snapshot.scenario,
            "prototype_session": snapshot.session,
            "prototype_regime": snapshot.regime,
            "prototype_profile": snapshot.profile,
            "prototype_warmup": snapshot.warmup,
            "prototype_event_counters": _prototype_counters(snapshot),
            "prototype_current_setup": snapshot.current_setup,
            "prototype_decision": snapshot.decision,
            "prototype_raw_features": snapshot.raw_features,
            "prototype_normalized_features": snapshot.normalized_features,
            "prototype_shadow_order": _prototype_shadow_order(snapshot),
            "prototype_report_path": snapshot.report_path,
        }
        for object_name, text in values.items():
            label = self.findChild(QLabel, object_name)
            if label is not None:
                label.setText(text)
        explanations = self.findChild(QTextEdit, "prototype_explanations")
        if explanations is not None:
            explanations.setPlainText("\n".join(snapshot.explanations))
        speed_selector = self.findChild(QComboBox, "prototype_speed_selector")
        if speed_selector is not None and speed_selector.currentText() != str(snapshot.playback_speed):
            speed_selector.blockSignals(True)
            speed_selector.setCurrentText(str(snapshot.playback_speed))
            speed_selector.blockSignals(False)

    @property
    def _is_live_recording_mode(self) -> bool:
        """True when driven by a live runtime with no prototype feed.

        In this mode strategy decisions are disabled (delayed/recording-only
        data), so the decision tab must not show fabricated placeholder
        conditions as if a real decision occurred.
        """
        return self._prototype_snapshot_provider is None and self._runtime_snapshot_provider is not None

    def _build_decision_explanation_tab(self) -> QWidget:
        prototype = self._current_prototype_snapshot() if self._prototype_snapshot_provider is not None else None
        result = self._mock_data.decision
        live_recording = self._is_live_recording_mode
        tab = QWidget()
        tab.setObjectName("decision_explanation_tab")
        layout = QVBoxLayout(tab)

        if live_recording:
            heading_text = "No strategy decision - recording only"
        elif prototype is not None:
            heading_text = f"{prototype.current_setup}: {prototype.decision}"
        else:
            heading_text = f"{result.setup_name}: {result.status}"
        heading = QLabel(heading_text)
        heading.setObjectName("decision_status")
        heading.setStyleSheet("font-size: 16px; font-weight: 600;")
        layout.addWidget(heading)

        conditions = QListWidget()
        conditions.setObjectName("decision_condition_list")
        if live_recording:
            conditions.addItem(
                "Shadow decisions are disabled on delayed Bookmap data - the assistant is recording only.",
            )
            conditions.addItem(
                "Decisions appear here on real-time data, or in Prototype mode against synthetic setups.",
            )
        elif prototype is not None:
            conditions.addItems(prototype.explanations)
        else:
            for condition in result.conditions:
                conditions.addItem(f"[{condition.status}] {condition.message}")
        layout.addWidget(conditions)

        coach_label = _named_label("blocker_coach_label", "")
        coach_label.setStyleSheet(COACH_STYLE)
        coach_label.setVisible(False)
        layout.addWidget(coach_label)

        narrator_label = _named_label("ai_narrator_label", AI_COMMENTARY_LABEL)
        narrator_label.setStyleSheet(BANNER_STYLE)
        narrator = QTextEdit()
        narrator.setObjectName("ai_narrator_text")
        narrator.setReadOnly(True)
        narrator.setMaximumHeight(96)
        if live_recording:
            narrator.setPlainText(
                "Recording live order flow only. No entry or exit decision is being made - "
                "delayed data cannot drive a scalping decision, so the assistant just records. "
                "Decisions and narration appear on real-time data or in Prototype mode.",
            )
        elif prototype is not None:
            narrator.setPlainText(
                narrate_decision(prototype.current_setup, prototype.decision, prototype.explanations),
            )
        else:
            narrator.setPlainText(
                narrate_decision(result.setup_name, result.status, result.render_lines()),
            )
        narrator_panel = QWidget()
        narrator_layout = QVBoxLayout(narrator_panel)
        narrator_layout.setContentsMargins(0, 0, 0, 0)
        narrator_layout.addWidget(narrator_label)
        narrator_layout.addWidget(narrator)
        layout.addWidget(_group("Plain-English narration", narrator_panel))

        matrix = QTableWidget(0, 0)
        matrix.setObjectName("condition_matrix_table")
        matrix.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(_group("Condition history (rows: conditions, columns: decisions)", matrix))

        timeline = QListWidget()
        timeline.setObjectName("decision_timeline_list")
        timeline.setMaximumHeight(96)
        layout.addWidget(_group("Decision timeline", timeline))
        return tab

    def _refresh_decision_explanation(self) -> None:
        """Refresh Screen 2 from the prototype snapshot when prototype mode is active."""
        if self._prototype_snapshot_provider is None:
            return
        snapshot = self._current_prototype_snapshot()
        heading = self.findChild(QLabel, "decision_status")
        conditions = self.findChild(QListWidget, "decision_condition_list")
        if heading is not None:
            heading.setText(f"{snapshot.current_setup}: {snapshot.decision}")
        if conditions is not None:
            conditions.clear()
            conditions.addItems(snapshot.explanations)
        self._record_decision(snapshot)
        self._refresh_agent_views(snapshot)

    def _record_decision(self, snapshot: PrototypeDashboardSnapshot) -> None:
        """Capture a newly finalized decision into the session history."""
        if snapshot.decision.casefold() in ("", "none"):
            return
        latest = self._decision_history[-1] if self._decision_history else None
        if (
            latest is not None
            and latest.setup_name == snapshot.current_setup
            and latest.decision == snapshot.decision
            and latest.explanations == snapshot.explanations
        ):
            return
        record = DecisionRecord(
            recorded_at=datetime.now(timezone.utc).strftime("%H:%M:%S"),
            setup_name=snapshot.current_setup,
            decision=snapshot.decision,
            explanations=snapshot.explanations,
            regime=snapshot.regime,
        )
        self._decision_history.append(record)
        chart = getattr(self, "price_chart", None)
        if chart is not None:
            chart.mark_decision(accepted=record.accepted)

    def _refresh_agent_views(self, snapshot: PrototypeDashboardSnapshot) -> None:
        """Repaint the narrator, condition matrix, and decision timeline."""
        narrator = self.findChild(QTextEdit, "ai_narrator_text")
        if narrator is not None:
            narrator.setPlainText(
                narrate_decision(snapshot.current_setup, snapshot.decision, snapshot.explanations),
            )

        history = self._decision_history[-12:]
        matrix = self.findChild(QTableWidget, "condition_matrix_table")
        if matrix is not None:
            condition_names: list[str] = []
            parsed_records = []
            for record in history:
                parsed = {
                    canonical_condition_name(condition.message): condition.passed
                    for condition in parse_explanation_lines(record.explanations)
                }
                parsed_records.append(parsed)
                for name in parsed:
                    if name not in condition_names:
                        condition_names.append(name)
            matrix.setRowCount(len(condition_names))
            matrix.setColumnCount(len(history))
            matrix.setHorizontalHeaderLabels([record.recorded_at for record in history])
            matrix.setVerticalHeaderLabels(condition_names)
            for column, parsed in enumerate(parsed_records):
                for row, name in enumerate(condition_names):
                    passed = parsed.get(name)
                    if passed is None:
                        item = _read_only_item("-")
                    else:
                        item = _read_only_item("pass" if passed else "fail")
                        item.setBackground(QColor(20, 83, 45) if passed else QColor(127, 29, 29))
                        item.setForeground(QColor(220, 252, 231) if passed else QColor(254, 226, 226))
                    matrix.setItem(row, column, item)

        timeline = self.findChild(QListWidget, "decision_timeline_list")
        if timeline is not None:
            timeline.clear()
            for record in history:
                item = QListWidgetItem(f"{record.recorded_at}  {record.setup_name}: {record.decision}")
                item.setBackground(QColor(20, 83, 45) if record.accepted else QColor(127, 29, 29))
                item.setForeground(QColor(220, 252, 231) if record.accepted else QColor(254, 226, 226))
                timeline.addItem(item)

    def _live_feed_stall_seconds(self) -> float | None:
        """Seconds since the live market price last changed, or None if unknown.

        The runtime socket can read "connected" long after data stops
        arriving (e.g. Bookmap paused, session ended upstream). Tracking the
        dashboard market price across refresh ticks catches that freeze even
        when the socket flag lies.
        """
        price = self._current_dashboard_snapshot().market_price
        now = self._stall_clock()
        if price in ("", "waiting", "unknown"):
            return None
        if price != self._last_live_price:
            self._last_live_price = price
            self._last_live_price_change_ts = now
            return 0.0
        if self._last_live_price_change_ts is None:
            self._last_live_price_change_ts = now
            return 0.0
        return now - self._last_live_price_change_ts

    def _refresh_watchdog(self) -> None:
        """Update the header watchdog line from prototype or live runtime state."""
        label = self.findChild(QLabel, "watchdog_status_label")
        if label is None:
            return
        if self._prototype_snapshot_provider is None and self._runtime_snapshot_provider is not None:
            runtime = self._current_runtime_snapshot()
            if runtime is not None:
                connected = runtime.bookmap_status == "connected"
                stalled_seconds = self._live_feed_stall_seconds()
                base = (
                    f"Watchdog: Bookmap {runtime.bookmap_status}; "
                    f"recording {'yes' if runtime.recording else 'no'}; "
                    f"dropped {runtime.dropped_message_count}"
                )
                if connected and stalled_seconds is not None and stalled_seconds >= self._stall_after_seconds:
                    label.setText(
                        f"{base} - STALLED: no price change in {int(stalled_seconds)}s "
                        "(socket looks connected but no data is arriving)",
                    )
                    label.setStyleSheet(WATCHDOG_WARNING_STYLE)
                else:
                    label.setText(base)
                    label.setStyleSheet(WATCHDOG_OK_STYLE if connected else WATCHDOG_WARNING_STYLE)
                return
        report = self._watchdog.evaluate(self._current_prototype_snapshot())
        self._last_watchdog_report = report
        label.setText(report.message)
        if report.severity == SEVERITY_WARNING:
            label.setStyleSheet(WATCHDOG_WARNING_STYLE)
        elif report.severity == SEVERITY_OK:
            label.setStyleSheet(WATCHDOG_OK_STYLE)
        else:
            label.setStyleSheet(WATCHDOG_INFO_STYLE)

    def _build_order_management_tab(self) -> QWidget:
        data = self._mock_data.order
        tab = QWidget()
        tab.setObjectName("order_management_tab")
        layout = QVBoxLayout(tab)

        layout.addWidget(
            _group(
                "Order state",
                _form_widget(
                    (
                        ("Entry order", QLabel(data.entry_order)),
                        ("Filled qty", QLabel(str(data.filled_qty))),
                        ("Avg fill", QLabel(_optional_decimal(data.average_fill))),
                        ("Stop", QLabel(_optional_decimal(data.stop))),
                        ("Target", QLabel(_optional_decimal(data.target))),
                        ("Unrealized P&L", QLabel(_format_money(data.unrealized_pnl))),
                        ("Remaining risk", QLabel(_format_money(data.remaining_risk))),
                    ),
                ),
            ),
        )

        buttons = QWidget()
        button_layout = QHBoxLayout(buttons)
        button_layout.setContentsMargins(0, 0, 0, 0)
        for object_name, label in (
            ("flatten_button", "Flatten"),
            ("cancel_all_button", "Cancel-all"),
            ("disable_automation_button", "Disable-automation"),
        ):
            button = QPushButton(label)
            button.setObjectName(object_name)
            # No broker path exists in this build; these controls must not look
            # operational (Phase 8). Disabled until a reviewed execution module.
            button.setEnabled(False)
            button.setToolTip("Disabled: no broker connection in this build (OBSERVE/SHADOW only).")
            button_layout.addWidget(button)
        button_layout.addStretch(1)
        layout.addWidget(buttons)
        layout.addStretch(1)
        return tab

    def _build_risk_configuration_tab(self) -> QWidget:
        data = self._mock_data.risk
        tab = QWidget()
        tab.setObjectName("risk_configuration_tab")
        layout = QVBoxLayout(tab)

        fields = (
            ("Account risk base", _line_edit(_format_decimal(data.account_risk_base))),
            ("Daily risk %", _line_edit(_format_decimal(data.daily_risk_percent))),
            ("Max trades", _spin_box(data.max_trades, 1, 100)),
            ("Max losses", _spin_box(data.max_losses, 0, 100)),
            ("Max contracts", _spin_box(data.max_contracts, 1, 100)),
            ("Max stop ticks", _spin_box(data.max_stop_ticks, 1, 1000)),
            ("Slippage allowance", _line_edit(_format_decimal(data.slippage_allowance))),
            ("Commission estimate", _line_edit(_format_decimal(data.commission_estimate))),
            ("Session hours", _line_edit(data.session_hours)),
            ("News lockout", _check_box(data.news_lockout_enabled)),
            ("Consecutive-loss lock", _check_box(data.consecutive_loss_lock_enabled)),
        )
        form = _form_widget(fields)
        form.setObjectName("risk_configuration_form")
        form.setEnabled(not data.trade_open)

        trade_state = "locked while a trade is open" if data.trade_open else "editable"
        layout.addWidget(QLabel(f"Risk configuration is {trade_state}."))
        layout.addWidget(form)
        layout.addStretch(1)
        return tab

    def _build_replay_tab(self) -> QWidget:
        sessions = self._list_replay_sessions()
        selected_session = sessions[0].label if sessions else "No recorded sessions"
        data = self._load_replay_snapshot(selected_session)
        tab = QWidget()
        tab.setObjectName("replay_tab")
        layout = QGridLayout(tab)
        layout.setSpacing(12)

        self.replay_session_picker = QComboBox()
        self.replay_session_picker.setObjectName("replay_session_picker")
        self.replay_session_picker.addItems([session.label for session in sessions] or [selected_session])
        self.replay_session_picker.setCurrentText(selected_session)
        self.replay_session_picker.currentTextChanged.connect(self.refresh_replay_session)
        layout.addWidget(_group("Historical session", self.replay_session_picker), 0, 0)

        self.replay_markers_table = QTableWidget(len(data.markers), 3)
        self.replay_markers_table.setObjectName("replay_markers_table")
        self.replay_markers_table.setHorizontalHeaderLabels(["Time", "Type", "Description"])
        for row, marker in enumerate(data.markers):
            self.replay_markers_table.setItem(row, 0, _read_only_item(marker.timestamp))
            self.replay_markers_table.setItem(row, 1, _read_only_item(marker.event_type))
            self.replay_markers_table.setItem(row, 2, _read_only_item(marker.description))
        self.replay_markers_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(
            _group("Recorded depth and trade events", self.replay_markers_table),
            1,
            0,
        )

        self._replay_markers = data.markers
        scrubber_panel = QWidget()
        scrubber_layout = QHBoxLayout(scrubber_panel)
        scrubber_layout.setContentsMargins(0, 0, 0, 0)
        self.replay_scrubber = QSlider(Qt.Orientation.Horizontal)
        self.replay_scrubber.setObjectName("replay_scrubber")
        self.replay_scrubber.setMinimum(0)
        self.replay_scrubber.setMaximum(len(data.markers))
        self.replay_scrubber.setValue(len(data.markers))
        self.replay_scrubber.valueChanged.connect(self._apply_replay_scrubber)
        scrubber_layout.addWidget(self.replay_scrubber)
        scrubber_layout.addWidget(
            _named_label("replay_scrubber_label", _scrubber_text(len(data.markers), data.markers)),
        )
        layout.addWidget(_group("Scrub through the session", scrubber_panel), 2, 0, 1, 2)

        self.replay_explanation_panel = QTextEdit()
        self.replay_explanation_panel.setObjectName("replay_explanation_panel")
        self.replay_explanation_panel.setReadOnly(True)
        self.replay_explanation_panel.setPlainText("\n".join(data.explanation))
        layout.addWidget(_group("Session summary", self.replay_explanation_panel), 0, 1, 2, 1)
        return tab

    def refresh_replay_session(self, session_date: str) -> None:
        """Refresh replay table rows for the selected recorded date."""
        data = self._load_replay_snapshot(session_date)
        table = self.findChild(QTableWidget, "replay_markers_table")
        explanation = self.findChild(QTextEdit, "replay_explanation_panel")
        if table is None or explanation is None:
            return

        table.setRowCount(len(data.markers))
        for row, marker in enumerate(data.markers):
            table.setItem(row, 0, _read_only_item(marker.timestamp))
            table.setItem(row, 1, _read_only_item(marker.event_type))
            table.setItem(row, 2, _read_only_item(marker.description))
        explanation.setPlainText("\n".join(data.explanation))
        self._replay_markers = data.markers
        scrubber = self.findChild(QSlider, "replay_scrubber")
        if scrubber is not None:
            scrubber.blockSignals(True)
            scrubber.setMaximum(len(data.markers))
            scrubber.setValue(len(data.markers))
            scrubber.blockSignals(False)
            self._apply_replay_scrubber(len(data.markers))

    def _apply_replay_scrubber(self, position: int) -> None:
        """Show only the recorded events up to the scrubber position."""
        table = self.findChild(QTableWidget, "replay_markers_table")
        label = self.findChild(QLabel, "replay_scrubber_label")
        markers = getattr(self, "_replay_markers", ())
        if table is not None:
            visible = markers[:position]
            table.setRowCount(len(visible))
            for row, marker in enumerate(visible):
                table.setItem(row, 0, _read_only_item(marker.timestamp))
                table.setItem(row, 1, _read_only_item(marker.event_type))
                table.setItem(row, 2, _read_only_item(marker.description))
        if label is not None:
            label.setText(_scrubber_text(position, markers))

    def _current_dashboard_snapshot(self) -> DashboardSnapshot:
        if self._prototype_snapshot_provider is not None:
            prototype = self._current_prototype_snapshot()
            return DashboardSnapshot(
                feeds=(
                    FeedStatus(
                        "Market receiver",
                        _prototype_feed_status(
                            prototype.synthetic_status,
                            prototype.depth_events + prototype.trade_events,
                        ),
                    ),
                    FeedStatus("Depth feed", _prototype_feed_status(prototype.synthetic_status, prototype.depth_events)),
                    FeedStatus("Trade feed", _prototype_feed_status(prototype.synthetic_status, prototype.trade_events)),
                ),
                instrument=prototype.instrument,
                session=prototype.session,
                market_price=prototype.last_trade_price or "waiting",
                bid_ask="prototype feed",
                spread="prototype feed",
                mode=prototype.source_mode,
                daily_risk_used=Decimal("0"),
                daily_risk_remaining=Decimal("0"),
                losses_count=0,
                setup_name=prototype.current_setup,
                confidence=Decimal("0"),
                setup_status=prototype.decision,
            )
        market_state = self._market_state_provider()
        risk_state = self._risk_state_provider()
        instrument = _instrument_from_risk_state(risk_state) or _instrument_from_market_state(market_state)
        daily_risk_used = risk_state.daily_realized_loss if risk_state is not None else Decimal("0")
        daily_risk_budget = risk_state.daily_risk_budget if risk_state is not None else Decimal("0")
        daily_risk_remaining = max(daily_risk_budget - daily_risk_used, Decimal("0"))
        losses_count = risk_state.losing_trades_today if risk_state is not None else 0

        return DashboardSnapshot(
            feeds=(
                FeedStatus(
                    "Market receiver",
                    "receiving" if market_state.timestamp_ns > 0 else "waiting",
                ),
                FeedStatus(
                    "Depth feed",
                    "receiving" if market_state.best_bid is not None or market_state.best_ask is not None else "waiting",
                ),
                FeedStatus(
                    "Trade feed",
                    "receiving"
                    if market_state.executed_buy_volume > Decimal("0")
                    or market_state.executed_sell_volume > Decimal("0")
                    else "waiting",
                ),
            ),
            instrument=instrument,
            session=_runtime_text(self._current_runtime_snapshot(), "session_name"),
            market_price=_market_price_text(market_state),
            bid_ask=_bid_ask_text(market_state),
            spread=_spread_text(market_state),
            mode=self._current_mode,
            daily_risk_used=daily_risk_used,
            daily_risk_remaining=daily_risk_remaining,
            losses_count=losses_count,
            setup_name="Awaiting strategy evaluation" if market_state.timestamp_ns > 0 else "No market state received",
            confidence=Decimal("0"),
            setup_status=_market_status_text(market_state),
        )

    def _current_runtime_snapshot(self) -> RuntimeSnapshot | None:
        if self._runtime_snapshot_provider is None:
            return None
        return self._runtime_snapshot_provider()

    def _refresh_runtime_labels(self) -> None:
        runtime = self._current_runtime_snapshot()
        values = {
            "runtime_state_value": _runtime_text(runtime, "state"),
            "runtime_mode_value": _runtime_text(runtime, "mode"),
            "runtime_bookmap_status": _runtime_text(runtime, "bookmap_status"),
            "runtime_recording_status": _runtime_bool_text(runtime, "recording"),
            "runtime_exact_contract": _runtime_text(runtime, "exact_contract"),
            "runtime_source_mode": _runtime_text(runtime, "source_mode"),
            "runtime_data_delay": _runtime_delay_text(runtime),
            "runtime_session_name": _runtime_text(runtime, "session_name"),
            "runtime_session_minutes": _runtime_minutes_text(runtime),
            "runtime_regime": _runtime_text(runtime, "regime"),
            "runtime_profile_id": _runtime_text(runtime, "profile_id"),
            "runtime_profile_validation": _runtime_text(runtime, "profile_validation"),
            "runtime_historical_sample_count": _runtime_int_text(runtime, "historical_sample_count"),
            "runtime_profile_fallback": _runtime_text(runtime, "profile_fallback"),
            "runtime_decisions_allowed": _runtime_bool_text(runtime, "decisions_allowed"),
            "runtime_threshold_summary": _runtime_text(runtime, "threshold_summary"),
            "runtime_sample_count": _runtime_sample_text(runtime),
            "runtime_data_age": _runtime_data_age_text(runtime),
            "runtime_dropped_messages": _runtime_int_text(runtime, "dropped_message_count"),
            "runtime_shadow_decisions": _runtime_int_text(runtime, "shadow_decisions"),
            "runtime_report_root": _runtime_text(runtime, "report_root"),
        }
        for object_name, text in values.items():
            label = self.findChild(QLabel, object_name)
            if label is not None:
                label.setText(text)

    def _list_replay_sessions(self) -> tuple[ReplaySession, ...]:
        if not self._replay_data_root.exists():
            return ()

        sessions: list[ReplaySession] = []
        for child in sorted(self._replay_data_root.iterdir()):
            if not child.is_dir():
                continue
            depth_path = child / "depth.parquet"
            trades_path = child / "trades.parquet"
            if depth_path.exists() or trades_path.exists():
                sessions.append(
                    ReplaySession(
                        label=child.name,
                        depth_path=depth_path if depth_path.exists() else None,
                        trades_path=trades_path if trades_path.exists() else None,
                    ),
                )
            for session_dir in sorted(child.iterdir()):
                if not session_dir.is_dir():
                    continue
                depth_path = session_dir / "depth.parquet"
                trades_path = session_dir / "trades.parquet"
                if depth_path.exists() or trades_path.exists():
                    sessions.append(
                        ReplaySession(
                            label=f"{child.name}/{session_dir.name}",
                            depth_path=depth_path if depth_path.exists() else None,
                            trades_path=trades_path if trades_path.exists() else None,
                        ),
                    )
        return tuple(sessions)

    def _load_replay_snapshot(self, session_date: str) -> ReplaySnapshot:
        sessions = self._list_replay_sessions()
        matching_session = next((session for session in sessions if session.label == session_date), None)
        if matching_session is None:
            return ReplaySnapshot(
                sessions=tuple(session.label for session in sessions),
                selected_session=session_date,
                markers=(),
                explanation=(f"No recorded session found in {self._replay_data_root}.",),
            )

        load_result = _load_replay_session(matching_session)
        return ReplaySnapshot(
            sessions=tuple(session.label for session in sessions),
            selected_session=session_date,
            markers=load_result.markers,
            explanation=load_result.explanation,
        )

    def _build_mode_banner(self) -> QWidget:
        banner_row = QWidget()
        layout = QHBoxLayout(banner_row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        mode_banner = _named_label("mode_banner_label", "")
        mode_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mode_banner.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        live_armed = _named_label("live_armed_label", "")

        arm_button = QPushButton("Arm LIVE")
        arm_button.setObjectName("arm_live_button")
        arm_button.setEnabled(False)
        arm_button.setToolTip(
            "LIVE mode can only be armed by a human editing the protected "
            "config/production_config.yaml and approving it via the config guard. "
            "This GUI never changes it.",
        )

        layout.addWidget(mode_banner, stretch=1)
        layout.addWidget(live_armed)
        layout.addWidget(arm_button)
        self._refresh_mode_banner(mode_banner, live_armed)
        return banner_row

    def _refresh_mode_banner(self, banner: QLabel | None = None, armed_label: QLabel | None = None) -> None:
        banner = banner or self.findChild(QLabel, "mode_banner_label")
        armed_label = armed_label or self.findChild(QLabel, "live_armed_label")
        view = self._mode_supervisor.view()
        if banner is not None:
            if view.live_armed:
                banner.setText("!!! LIVE MODE ARMED - REAL ORDERS POSSIBLE !!!")
                banner.setStyleSheet(
                    "background-color: #7f1d1d; color: #fecaca; border: 2px solid #ef4444; "
                    "border-radius: 6px; padding: 6px; font-size: 15px; font-weight: 800;",
                )
            else:
                banner.setText("SIMULATION / SHADOW MODE - NO REAL ORDERS CAN BE PLACED")
                banner.setStyleSheet(
                    "background-color: #14532d; color: #bbf7d0; border: 1px solid #22c55e; "
                    "border-radius: 6px; padding: 6px; font-size: 14px; font-weight: 700;",
                )
        if armed_label is not None:
            armed_label.setText(f"LIVE ARMED: {'YES' if view.live_armed else 'NO'}")
            armed_label.setStyleSheet(
                "color: #f87171; font-weight: 800;" if view.live_armed else "color: #4ade80; font-weight: 700;",
            )

    def _build_decision_log_tab(self) -> QWidget:
        tab = QWidget()
        tab.setObjectName("decision_log_tab")
        layout = QVBoxLayout(tab)

        filter_row = QWidget()
        filter_layout = QHBoxLayout(filter_row)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        decision_filter = QComboBox()
        decision_filter.setObjectName("decision_filter_combo")
        decision_filter.addItems(["All", "Accepted", "Rejected"])
        decision_filter.currentTextChanged.connect(lambda _text: self._refresh_decision_log())
        regime_filter = QComboBox()
        regime_filter.setObjectName("regime_filter_combo")
        regime_filter.addItem("All regimes")
        regime_filter.currentTextChanged.connect(lambda _text: self._refresh_decision_log())
        filter_layout.addWidget(QLabel("Decision:"))
        filter_layout.addWidget(decision_filter)
        filter_layout.addWidget(QLabel("Regime:"))
        filter_layout.addWidget(regime_filter)
        filter_layout.addStretch(1)
        layout.addWidget(filter_row)

        table = QTableWidget(0, 4)
        table.setObjectName("decision_log_table")
        table.setHorizontalHeaderLabels(["Time", "Setup", "Decision", "Regime"])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.itemSelectionChanged.connect(self._show_decision_trace)
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(_group("Decisions this session", table))

        trace = QTextEdit()
        trace.setObjectName("decision_trace_text")
        trace.setReadOnly(True)
        trace.setPlaceholderText("Select a decision to see exactly why it was accepted or rejected.")
        layout.addWidget(_group("Why? (one-click trace)", trace))
        return tab

    def _filtered_decision_records(self) -> list[DecisionRecord]:
        decision_filter = self.findChild(QComboBox, "decision_filter_combo")
        regime_filter = self.findChild(QComboBox, "regime_filter_combo")
        records = list(self._decision_history)
        if decision_filter is not None:
            selected = decision_filter.currentText()
            if selected == "Accepted":
                records = [record for record in records if record.accepted]
            elif selected == "Rejected":
                records = [record for record in records if not record.accepted]
        if regime_filter is not None and regime_filter.currentText() != "All regimes":
            records = [record for record in records if record.regime == regime_filter.currentText()]
        return records

    def _refresh_decision_log(self) -> None:
        table = self.findChild(QTableWidget, "decision_log_table")
        regime_filter = self.findChild(QComboBox, "regime_filter_combo")
        if regime_filter is not None:
            known = {regime_filter.itemText(index) for index in range(regime_filter.count())}
            for record in self._decision_history:
                if record.regime and record.regime not in known:
                    regime_filter.addItem(record.regime)
                    known.add(record.regime)
        if table is None:
            return
        records = self._filtered_decision_records()
        table.setRowCount(len(records))
        for row, record in enumerate(records):
            table.setItem(row, 0, _read_only_item(record.recorded_at))
            table.setItem(row, 1, _read_only_item(record.setup_name))
            table.setItem(row, 2, _read_only_item(record.decision))
            table.setItem(row, 3, _read_only_item(record.regime or "-"))

    def _show_decision_trace(self) -> None:
        table = self.findChild(QTableWidget, "decision_log_table")
        trace = self.findChild(QTextEdit, "decision_trace_text")
        if table is None or trace is None:
            return
        row = table.currentRow()
        records = self._filtered_decision_records()
        if row < 0 or row >= len(records):
            return
        record = records[row]
        lines = [
            f"{record.setup_name}: {record.decision} at {record.recorded_at}",
            f"Regime: {record.regime or 'unknown'}",
            "",
            narrate_decision(record.setup_name, record.decision, record.explanations),
            "",
            "Condition detail:",
            *record.explanations,
        ]
        trace.setPlainText("\n".join(lines))

    def _build_leaderboard_tab(self) -> QWidget:
        tab = QWidget()
        tab.setObjectName("leaderboard_tab")
        layout = QVBoxLayout(tab)

        intro = QLabel(
            "Model-candidate leaderboard - full composite breakdown, never just a win-percent column. "
            "Data source: discovery pipeline output (synthetic until Stage C).",
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        reload_button = QPushButton("Reload candidates")
        reload_button.setObjectName("leaderboard_reload_button")
        reload_button.clicked.connect(self._refresh_leaderboard)
        layout.addWidget(reload_button)

        table = QTableWidget(0, 9)
        table.setObjectName("leaderboard_table")
        table.setHorizontalHeaderLabels(
            ["Parameters", "Composite", "Win rate", "Profit factor", "Max DD (R)", "Sortino", "Expectancy (R)", "Trades", "Accepted"],
        )
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.itemSelectionChanged.connect(self._show_candidate_trace)
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(_group("Candidates (ranked by composite)", table))

        trace = QTextEdit()
        trace.setObjectName("candidate_trace_text")
        trace.setReadOnly(True)
        trace.setPlaceholderText("Select a candidate to see its gates and rejection reasons.")
        layout.addWidget(_group("Why? (one-click trace)", trace))
        self._leaderboard_entries: list[dict[str, object]] = []
        self._leaderboard_table = table
        self._refresh_leaderboard()
        return tab

    def _refresh_leaderboard(self) -> None:
        table = getattr(self, "_leaderboard_table", None)
        if table is None:
            return
        entries: list[dict[str, object]] = []
        candidates_path = self._discovery_root / "candidates.jsonl"
        if candidates_path.is_file():
            for line in candidates_path.read_text(encoding="utf-8").strip().splitlines():
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        self._leaderboard_entries = entries
        table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            table.setItem(row, 0, _read_only_item(str(entry.get("parameters", ""))))
            table.setItem(row, 1, _read_only_item(str(entry.get("composite", ""))))
            table.setItem(row, 2, _read_only_item(str(entry.get("win_rate", ""))))
            table.setItem(row, 3, _read_only_item(str(entry.get("profit_factor", ""))))
            table.setItem(row, 4, _read_only_item(str(entry.get("max_drawdown_r", ""))))
            table.setItem(row, 5, _read_only_item(str(entry.get("sortino", ""))))
            table.setItem(row, 6, _read_only_item(str(entry.get("expectancy_r", ""))))
            table.setItem(row, 7, _read_only_item(str(entry.get("trade_count", ""))))
            table.setItem(row, 8, _read_only_item("yes" if entry.get("accepted") else "NO"))

    def _show_candidate_trace(self) -> None:
        table = self.findChild(QTableWidget, "leaderboard_table")
        trace = self.findChild(QTextEdit, "candidate_trace_text")
        if table is None or trace is None:
            return
        row = table.currentRow()
        if row < 0 or row >= len(self._leaderboard_entries):
            return
        entry = self._leaderboard_entries[row]
        lines = [f"Candidate: {entry.get('parameters', '')}", ""]
        rejections = entry.get("rejection_reasons") or []
        if rejections:
            lines.append("Rejected because:")
            lines.extend(f"- {reason}" for reason in rejections)
        else:
            lines.append("Accepted: cleared every gate.")
        regime_expectancy = entry.get("regime_expectancy") or {}
        if regime_expectancy:
            lines += ["", "Per-regime expectancy (R):"]
            lines.extend(f"- {tag}: {value}" for tag, value in regime_expectancy.items())
        trace.setPlainText("\n".join(lines))

    def _run_automations(self) -> None:
        """Run the automation engine for this tick and apply its directives."""
        actions = self._automation_engine.process(
            self._current_prototype_snapshot(),
            tuple(self._decision_history),
            self._last_watchdog_report,
        )
        if self._prototype_control_handler is not None:
            for command in actions.control_commands:
                self._prototype_control_handler(command)
        banner = self.findChild(QLabel, "automation_banner_label")
        if banner is not None:
            text = actions.persistent_banner or actions.flash or ""
            banner.setText(text)
            banner.setVisible(bool(text))
        coach = self.findChild(QLabel, "blocker_coach_label")
        if coach is not None:
            coach.setText(actions.coach or "")
            coach.setVisible(bool(actions.coach))
        if actions.refresh_replay:
            self._auto_refresh_replay_sessions()
        self._refresh_automation_counts()

    def _auto_refresh_replay_sessions(self) -> None:
        """Rescan recorded sessions and update the replay picker if changed."""
        picker = self.findChild(QComboBox, "replay_session_picker")
        if picker is None:
            return
        sessions = self._list_replay_sessions()
        labels = [session.label for session in sessions] or ["No recorded sessions"]
        current_labels = [picker.itemText(index) for index in range(picker.count())]
        if labels == current_labels:
            return
        selected = picker.currentText()
        picker.blockSignals(True)
        picker.clear()
        picker.addItems(labels)
        picker.setCurrentText(selected if selected in labels else labels[0])
        picker.blockSignals(False)
        self.refresh_replay_session(picker.currentText())

    def _build_automations_tab(self) -> QWidget:
        tab = QWidget()
        tab.setObjectName("automations_tab")
        layout = QVBoxLayout(tab)

        intro = QLabel(
            "Workflow automations only - none of these can ever place, modify, or cancel an order.",
        )
        intro.setStyleSheet(BANNER_STYLE)
        layout.addWidget(intro)

        toggle_rows: list[tuple[str, QWidget]] = []
        for definition in AUTOMATION_DEFINITIONS:
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            toggle = QCheckBox(definition.title)
            toggle.setObjectName(f"automation_toggle_{definition.key}")
            toggle.setChecked(self._automation_engine.enabled[definition.key])
            toggle.setToolTip(definition.description)
            toggle.toggled.connect(
                lambda checked, key=definition.key: self._automation_engine.enabled.__setitem__(key, checked),
            )
            count = _named_label(f"automation_count_{definition.key}", "fired 0x")
            count.setStyleSheet(WATCHDOG_INFO_STYLE)
            row_layout.addWidget(toggle)
            row_layout.addStretch(1)
            row_layout.addWidget(count)
            toggle_rows.append((definition.description, row))
        layout.addWidget(_group("Automations (toggle any off)", _form_widget(tuple(toggle_rows))))

        checklist = QListWidget()
        checklist.setObjectName("startup_checklist_list")
        for item in self._automation_engine.startup_checklist():
            entry = QListWidgetItem(f"{'PASS' if item.passed else 'FAIL'}  {item.name} - {item.detail}")
            entry.setForeground(QColor(74, 222, 128) if item.passed else QColor(248, 113, 113))
            checklist.addItem(entry)
        layout.addWidget(_group("Startup checklist", checklist))
        layout.addStretch(1)
        return tab

    def _refresh_automation_counts(self) -> None:
        """Update the fired-count labels in the automations tab."""
        for definition in AUTOMATION_DEFINITIONS:
            label = self.findChild(QLabel, f"automation_count_{definition.key}")
            if label is not None:
                label.setText(f"fired {self._automation_engine.fire_counts[definition.key]}x")

    def _build_paper_trading_tab(self) -> QWidget:
        tab = QWidget()
        tab.setObjectName("paper_trading_tab")
        layout = QVBoxLayout(tab)

        intro = QLabel(
            "SYNTHETIC DEMO - NOT REAL PERFORMANCE. One fixed $100,000 paper "
            "account, mentor's rules (10-point stop, max 3 trades / 3 losses "
            "per day), NO resets: a blown account stops and the loss stands. "
            "The trade stream is reconstructed from discovery candidates that "
            "run on SYNTHETIC episodes - these numbers describe a synthetic "
            "stream, not a market edge, until real Stage-C data exists. "
            "No real orders.",
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(BANNER_STYLE)
        layout.addWidget(intro)

        run_button = QPushButton("Run paper simulation")
        run_button.setObjectName("paper_run_button")
        run_button.clicked.connect(self._run_paper_simulation)
        layout.addWidget(run_button)

        progress = QListWidget()
        progress.setObjectName("paper_progress_list")
        progress.setMaximumHeight(200)
        layout.addWidget(_group("Consistency progress", progress))

        table = QTableWidget(0, 6)
        table.setObjectName("paper_trades_table")
        table.setHorizontalHeaderLabels(["Account", "Date", "Contracts", "R", "P&L", "Balance"])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(_group("Simulated trades (green win / red loss)", table))
        self._paper_progress = progress
        self._paper_table = table
        return tab

    def _load_discovery_trades(self) -> tuple[TradeResult, ...]:
        """Build a trade sequence from the discovery candidates log, if any.

        Uses the top-ranked candidate's per-regime expectancy to synthesize a
        representative outcome stream. This is discovery/synthetic data until
        real Stage-C setups replace it.
        """
        path = self._discovery_root / "candidates.jsonl"
        if not path.is_file():
            return ()
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            return ()
        top = json.loads(lines[0])
        trade_count = int(top.get("trade_count", 0) or 0)
        expectancy = Decimal(str(top.get("expectancy_r", "0") or "0"))
        win_rate = Decimal(str(top.get("win_rate", "0") or "0"))
        if trade_count <= 0:
            return ()
        # Reconstruct a deterministic win/loss stream matching the reported
        # win rate and expectancy: wins of +w, losses of -1R.
        wins = int((win_rate * Decimal(trade_count)).to_integral_value())
        losses = trade_count - wins
        # Solve win payoff so mean == expectancy: (wins*w - losses) / n = E.
        win_payoff = (
            (expectancy * Decimal(trade_count) + Decimal(losses)) / Decimal(wins)
            if wins
            else Decimal("0")
        )
        trades: list[TradeResult] = []
        for index in range(trade_count):
            is_win = index % trade_count < wins
            day = f"2026-07-{(index // 6) % 28 + 1:02d}"
            trades.append(
                TradeResult(
                    r_multiple=win_payoff if is_win else Decimal("-1"),
                    session="new_york_open",
                    regime_tag="trending/high_vol",
                    trade_date=day,
                ),
            )
        return tuple(trades)

    def _run_paper_simulation(self) -> None:
        """Run ONE fixed $100k account (no resets) and render the honest ledger.

        The trade stream currently comes from synthetic discovery data, so the
        summary is labeled a synthetic demo, never real performance. No
        reset-until-profitable survivorship.
        """
        progress = getattr(self, "_paper_progress", None)
        table = getattr(self, "_paper_table", None)
        if progress is None or table is None:
            return
        trades = self._load_discovery_trades()
        progress.clear()
        table.setRowCount(0)
        if not trades:
            progress.addItem("No discovery candidates yet - run: python -m tools.run_discovery")
            return

        account = run_single_account(trades, PaperAccountConfig())
        # Trades are reconstructed from discovery candidates, which run on
        # synthetic episodes until real Stage-C data exists.
        for line in single_account_summary(account, synthetic=True):
            progress.addItem(line)

        table.setRowCount(len(account.trades))
        for row, trade in enumerate(account.trades):
            cells = [
                str(trade.account_number),
                trade.trade_date,
                str(trade.contracts),
                str(trade.r_multiple.quantize(Decimal("0.01"))),
                _format_money(trade.pnl),
                _format_money(trade.balance_after),
            ]
            for column, value in enumerate(cells):
                item = _read_only_item(value)
                item.setForeground(QColor(74, 222, 128) if trade.won else QColor(248, 113, 113))
                table.setItem(row, column, item)

    def _build_ask_tab(self) -> QWidget:
        tab = QWidget()
        tab.setObjectName("ask_tab")
        layout = QVBoxLayout(tab)

        banner = _named_label("ask_banner_label", AI_COMMENTARY_LABEL)
        banner.setStyleSheet(BANNER_STYLE)
        layout.addWidget(banner)
        layout.addWidget(QLabel("Ask about the current prototype state. Answers come only from recorded data."))

        conversation = QTextEdit()
        conversation.setObjectName("ask_conversation")
        conversation.setReadOnly(True)
        layout.addWidget(conversation)

        input_row = QWidget()
        input_layout = QHBoxLayout(input_row)
        input_layout.setContentsMargins(0, 0, 0, 0)
        question_input = QLineEdit()
        question_input.setObjectName("ask_input")
        question_input.setPlaceholderText("e.g. why was the last setup rejected?")
        question_input.returnPressed.connect(self._handle_ask_question)
        send_button = QPushButton("Ask")
        send_button.setObjectName("ask_send_button")
        send_button.clicked.connect(self._handle_ask_question)
        input_layout.addWidget(question_input)
        input_layout.addWidget(send_button)
        layout.addWidget(input_row)
        return tab

    def _handle_ask_question(self) -> None:
        """Answer the typed question from snapshot and history only."""
        question_input = self.findChild(QLineEdit, "ask_input")
        conversation = self.findChild(QTextEdit, "ask_conversation")
        if question_input is None or conversation is None:
            return
        question = question_input.text().strip()
        if not question:
            return
        answer = answer_question(
            question,
            snapshot=self._current_prototype_snapshot(),
            history=tuple(self._decision_history),
        )
        conversation.append(f"You: {question}")
        conversation.append(f"Assistant: {answer}")
        conversation.append("")
        question_input.clear()

    def _build_session_review_tab(self) -> QWidget:
        tab = QWidget()
        tab.setObjectName("session_review_tab")
        layout = QVBoxLayout(tab)

        banner = _named_label("session_review_banner", AI_COMMENTARY_LABEL)
        banner.setStyleSheet(BANNER_STYLE)
        layout.addWidget(banner)
        layout.addWidget(
            QLabel("Generates a markdown review of every decision recorded this session (synthetic data only)."),
        )

        generate_button = QPushButton("Generate session review")
        generate_button.setObjectName("session_review_button")
        generate_button.clicked.connect(self._handle_generate_review)
        layout.addWidget(generate_button)

        layout.addWidget(_named_label("session_review_path", "Review not generated yet."))

        review_text = QTextEdit()
        review_text.setObjectName("session_review_text")
        review_text.setReadOnly(True)
        layout.addWidget(review_text)
        return tab

    def _handle_generate_review(self) -> None:
        """Write the markdown session review and render it in the tab."""
        review_text = self.findChild(QTextEdit, "session_review_text")
        path_label = self.findChild(QLabel, "session_review_path")
        if review_text is None or path_label is None:
            return
        if not self._decision_history:
            review_text.setPlainText(
                "No decisions recorded yet - run the prototype scenario first, then generate the review.",
            )
            return
        path = write_review(tuple(self._decision_history), self._review_output_root)
        review_text.setPlainText(path.read_text(encoding="utf-8"))
        path_label.setText(str(path))

    def _build_model_health_tab(self) -> QWidget:
        data = self._mock_data.model_health
        tab = QWidget()
        tab.setObjectName("model_health_tab")
        layout = QVBoxLayout(tab)
        layout.addWidget(
            _group(
                "Model health",
                _form_widget(
                    (
                        ("Model version", QLabel(data.model_version)),
                        ("Training period", QLabel(data.training_period)),
                        ("Testing period", QLabel(data.testing_period)),
                        ("Setup count", QLabel(str(data.setup_count))),
                        ("Live prediction distribution", QLabel(data.prediction_distribution)),
                        ("Calibration", QLabel(data.calibration)),
                        ("Drift", QLabel(data.drift)),
                        ("Last retrain date", QLabel(data.last_retrain_date)),
                    ),
                ),
            ),
        )
        layout.addStretch(1)
        return tab


def _load_replay_session(session: ReplaySession) -> ReplayLoadResult:
    markers: list[ReplayMarker] = []
    depth_count = 0
    trade_count = 0

    if session.depth_path is not None:
        depth_rows = _read_parquet_rows(session.depth_path)
        depth_count = len(depth_rows)
        for row in depth_rows:
            timestamp_ns = int(row["timestamp"])
            markers.append(
                ReplayMarker(
                    timestamp=_format_timestamp_ns(timestamp_ns),
                    event_type="depth",
                    description=(
                        f'{row["symbol"]} {row["side"]} {row["price"]}: '
                        f'{row["previous_size"]} -> {row["new_size"]}'
                    ),
                ),
            )

    if session.trades_path is not None:
        trade_rows = _read_parquet_rows(session.trades_path)
        trade_count = len(trade_rows)
        for row in trade_rows:
            timestamp_ns = int(row["timestamp_ns"])
            markers.append(
                ReplayMarker(
                    timestamp=_format_timestamp_ns(timestamp_ns),
                    event_type="trade",
                    description=(
                        f'{row["instrument"]} {row["aggressor_side"]} '
                        f'{row["size"]} @ {row["price"]} seq={row["sequence_id"]}'
                    ),
                ),
            )

    markers.sort(key=lambda marker: marker.timestamp)
    explanation = (
        f"Session {session.label}",
        f"Depth updates: {depth_count}",
        f"Trades: {trade_count}",
        f"Source: {session.depth_path.parent if session.depth_path is not None else session.trades_path.parent}",
    )
    return ReplayLoadResult(markers=tuple(markers), explanation=explanation)


def _read_parquet_rows(path: Path) -> list[dict[str, object]]:
    import pyarrow.parquet as pq

    return [dict(row) for row in pq.read_table(path).to_pylist()]


def _format_timestamp_ns(timestamp_ns: int) -> str:
    seconds, nanoseconds = divmod(timestamp_ns, 1_000_000_000)
    timestamp = datetime.fromtimestamp(seconds, tz=timezone.utc)
    timestamp = timestamp.replace(microsecond=nanoseconds // 1_000)
    return timestamp.isoformat()


def _instrument_from_risk_state(risk_state: EntryLimitState | None) -> str | None:
    if risk_state is None or not risk_state.instrument_configs:
        return None
    return risk_state.instrument_configs[0].symbol


def _instrument_from_market_state(market_state: MarketState) -> str:
    if market_state.timestamp_ns > 0:
        return "MNQ"
    return "UNKNOWN"


def _market_status_text(market_state: MarketState) -> str:
    if market_state.timestamp_ns <= 0:
        return "waiting for receiver"
    if market_state.best_bid is not None and market_state.best_ask is not None:
        return f"book live bid={market_state.best_bid} ask={market_state.best_ask}"
    return "receiving partial market state"


def _market_price_text(market_state: MarketState) -> str:
    if market_state.mid_price is not None:
        return _format_price(market_state.mid_price)
    if market_state.best_bid is not None:
        return _format_price(market_state.best_bid)
    if market_state.best_ask is not None:
        return _format_price(market_state.best_ask)
    return "waiting"


def _bid_ask_text(market_state: MarketState) -> str:
    bid = _format_price(market_state.best_bid) if market_state.best_bid is not None else "waiting"
    ask = _format_price(market_state.best_ask) if market_state.best_ask is not None else "waiting"
    return f"{bid} / {ask}"


def _spread_text(market_state: MarketState) -> str:
    if market_state.spread is None:
        return "waiting"
    return _format_price(market_state.spread)


def _runtime_text(runtime: RuntimeSnapshot | None, field_name: str) -> str:
    if runtime is None:
        return "not started"
    return str(getattr(runtime, field_name))


def _runtime_bool_text(runtime: RuntimeSnapshot | None, field_name: str) -> str:
    if runtime is None:
        return "not started"
    return "yes" if bool(getattr(runtime, field_name)) else "no"


def _runtime_int_text(runtime: RuntimeSnapshot | None, field_name: str) -> str:
    if runtime is None:
        return "0"
    return str(getattr(runtime, field_name))


def _runtime_sample_text(runtime: RuntimeSnapshot | None) -> str:
    if runtime is None:
        return "0 / waiting"
    status = "complete" if runtime.warmup_complete else "warming"
    return f"{runtime.sample_count} / {status}"


def _runtime_data_age_text(runtime: RuntimeSnapshot | None) -> str:
    if runtime is None or runtime.data_age_ms is None:
        return "unknown"
    return f"{runtime.data_age_ms} ms"


def _runtime_minutes_text(runtime: RuntimeSnapshot | None) -> str:
    if runtime is None:
        return "unknown"
    since = "unknown" if runtime.minutes_since_open is None else str(runtime.minutes_since_open)
    until = "unknown" if runtime.minutes_until_close is None else str(runtime.minutes_until_close)
    return f"{since} since open / {until} until close"


def _runtime_delay_text(runtime: RuntimeSnapshot | None) -> str:
    if runtime is None or runtime.data_delay_minutes is None:
        return "none"
    return f"{runtime.data_delay_minutes} minutes delayed"


def _prototype_counters(snapshot: PrototypeDashboardSnapshot) -> str:
    return (
        f"depth={snapshot.depth_events}, trades={snapshot.trade_events}, "
        f"control={snapshot.control_events}"
    )


def _prototype_feed_status(synthetic_status: str, event_count: int) -> str:
    """Translate prototype activity into the status vocabulary used by Screen 1."""
    if event_count > 0:
        return "receiving"
    return synthetic_status


def _scrubber_text(position: int, markers: Sequence[ReplayMarker]) -> str:
    """Describe the scrubber position with the timestamp at that point."""
    total = len(markers)
    if total == 0:
        return "No recorded events"
    if position <= 0:
        return f"0 / {total} events"
    timestamp = markers[min(position, total) - 1].timestamp
    return f"{min(position, total)} / {total} events (through {timestamp})"


def _prototype_shadow_order(snapshot: PrototypeDashboardSnapshot) -> str:
    """Never surface a shadow order alongside a rejected prototype decision."""
    if snapshot.decision.casefold() == "rejected":
        return ""
    return snapshot.shadow_order


def create_mock_gui_data() -> MockGuiData:
    """Create deterministic placeholder data for the GUI skeleton."""
    decision = SetupEvaluationResult(
        setup_name="LONG SETUP",
        conditions=(
            SetupConditionResult(
                key="at_important_level",
                passed=True,
                message="Price at overnight_low",
            ),
            SetupConditionResult(
                key="aggressive_sell_volume",
                passed=True,
                message="412 contracts sold aggressively",
            ),
            SetupConditionResult(
                key="downward_progress_ticks",
                passed=True,
                message="Price moved only 3 ticks lower",
            ),
            SetupConditionResult(
                key="bid_reload_count",
                passed=False,
                message="Bid liquidity did not reload",
            ),
            SetupConditionResult(
                key="reclaimed_level",
                passed=False,
                message="Reclaim confirmation not completed",
            ),
        ),
    )
    return MockGuiData(
        dashboard=DashboardSnapshot(
            feeds=(
                FeedStatus("Depth feed", "connected"),
                FeedStatus("Trade feed", "connected"),
                FeedStatus("Recorder", "standby"),
            ),
            instrument="MNQ",
            session="New York open",
            market_price="100.125",
            bid_ask="100.00 / 100.25",
            spread="0.25",
            mode=OBSERVE_MODE,
            daily_risk_used=Decimal("125.00"),
            daily_risk_remaining=Decimal("375.00"),
            losses_count=1,
            setup_name="Long absorption reclaim",
            confidence=Decimal("72.5"),
            setup_status="watching",
        ),
        decision=decision,
        order=OrderManagementSnapshot(
            entry_order="none",
            filled_qty=0,
            average_fill=None,
            stop=None,
            target=None,
            unrealized_pnl=Decimal("0"),
            remaining_risk=Decimal("375.00"),
        ),
        risk=RiskConfigurationSnapshot(
            trade_open=False,
            account_risk_base=Decimal("50000"),
            daily_risk_percent=Decimal("1.00"),
            max_trades=3,
            max_losses=3,
            max_contracts=2,
            max_stop_ticks=20,
            slippage_allowance=Decimal("1.00"),
            commission_estimate=Decimal("1.24"),
            session_hours="08:30-15:00 America/Chicago",
            news_lockout_enabled=True,
            consecutive_loss_lock_enabled=True,
        ),
        replay=ReplaySnapshot(
            sessions=("2026-07-08 RTH", "2026-07-09 RTH"),
            selected_session="2026-07-09 RTH",
            markers=(
                ReplayMarker("09:41:12", "setup", "Long absorption marker"),
                ReplayMarker("09:41:18", "entry", "Simulated long entry"),
                ReplayMarker("10:07:44", "exit", "Target reached"),
                ReplayMarker("11:22:09", "rejected", "No bid reload"),
            ),
            explanation=decision.render_lines(),
        ),
        model_health=ModelHealthSnapshot(
            model_version="rules-only-v0",
            training_period="not trained",
            testing_period="synthetic smoke tests",
            setup_count=0,
            prediction_distribution="observe-only placeholders",
            calibration="not available",
            drift="not available",
            last_retrain_date="never",
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the PySide6 GUI application."""
    app = QApplication(list(argv) if argv is not None else sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


def _group(title: str, content: QWidget, row_span: int | None = None, column_span: int | None = None) -> QGroupBox:
    group = QGroupBox(title)
    layout = QVBoxLayout(group)
    layout.addWidget(content)
    group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    return group


def _form_widget(rows: Sequence[tuple[str, QWidget]]) -> QWidget:
    frame = QFrame()
    layout = QFormLayout(frame)
    layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
    for label, widget in rows:
        layout.addRow(label, widget)
    return frame


def _read_only_item(value: str) -> QTableWidgetItem:
    item = QTableWidgetItem(value)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


def _named_label(object_name: str, text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName(object_name)
    return label


def _line_edit(text: str) -> QLineEdit:
    edit = QLineEdit(text)
    return edit


def _spin_box(value: int, minimum: int, maximum: int) -> QSpinBox:
    spin_box = QSpinBox()
    spin_box.setRange(minimum, maximum)
    spin_box.setValue(value)
    return spin_box


def _check_box(checked: bool) -> QCheckBox:
    check_box = QCheckBox()
    check_box.setChecked(checked)
    return check_box


def _format_money(value: Decimal) -> str:
    return f"${value.quantize(Decimal('0.01'))}"


def _format_decimal(value: Decimal) -> str:
    # format(..., 'f') keeps plain notation; normalize() alone renders
    # Decimal("50000") as "5E+4" in the risk configuration fields.
    return format(value.normalize(), "f")


def _format_price(value: Decimal) -> str:
    return format(value, "f")


def _optional_decimal(value: Decimal | None) -> str:
    if value is None:
        return "none"
    return _format_decimal(value)


if __name__ == "__main__":
    raise SystemExit(main())
