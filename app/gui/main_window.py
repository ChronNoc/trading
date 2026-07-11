"""PySide6 desktop skeleton for the MNQ trading assistant."""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import cast

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QStandardItemModel
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
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
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
        self._current_mode = OBSERVE_MODE
        self._reverting_mode = False

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        layout.addWidget(self._build_header())

        self.tabs = QTabWidget()
        self.tabs.setObjectName("main_tab_widget")
        self.tabs.addTab(self._build_live_dashboard_tab(), "Live dashboard")
        self.tabs.addTab(self._build_decision_explanation_tab(), "Decision explanation")
        self.tabs.addTab(self._build_order_management_tab(), "Order management")
        self.tabs.addTab(self._build_risk_configuration_tab(), "Risk configuration")
        self.tabs.addTab(self._build_replay_tab(), "Replay")
        self.tabs.addTab(self._build_model_health_tab(), "Model health")
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

        layout.addWidget(title)
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
                        ("Instrument", QLabel(data.instrument)),
                        ("Mode", _named_label("dashboard_mode_value", self._current_mode)),
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
                        ("Setup", QLabel(data.setup_name)),
                        ("Confidence", QLabel(f"{data.confidence}%")),
                        ("Status", QLabel(data.setup_status)),
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
        refresh_button = QPushButton("Refresh")
        refresh_button.setObjectName("live_dashboard_refresh_button")
        refresh_button.clicked.connect(self.refresh_live_dashboard)
        layout.addWidget(refresh_button, 4, 1, alignment=Qt.AlignmentFlag.AlignRight)
        return tab

    def refresh_live_dashboard(self) -> None:
        """Refresh dashboard labels from the current market and risk providers."""
        data = self._current_dashboard_snapshot()
        mode_value = self.findChild(QLabel, "dashboard_mode_value")
        losses_count = self.findChild(QLabel, "dashboard_losses_count")
        daily_risk_used = self.findChild(QLabel, "dashboard_daily_risk_used")
        daily_risk_remaining = self.findChild(QLabel, "dashboard_daily_risk_remaining")
        if mode_value is not None:
            mode_value.setText(self._current_mode)
        if losses_count is not None:
            losses_count.setText(str(data.losses_count))
        if daily_risk_used is not None:
            daily_risk_used.setText(_format_money(data.daily_risk_used))
        if daily_risk_remaining is not None:
            daily_risk_remaining.setText(_format_money(data.daily_risk_remaining))
        self._refresh_runtime_labels()
        self._refresh_prototype_labels()

    def _build_prototype_panel(self) -> QWidget:
        snapshot = self._current_prototype_snapshot()
        banner = _named_label("prototype_banner_label", snapshot.banner)
        banner.setStyleSheet("font-weight: 700; color: #9a3412;")
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
                ("Shadow order", _named_label("prototype_shadow_order", snapshot.shadow_order)),
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
            "prototype_shadow_order": snapshot.shadow_order,
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

    def _build_decision_explanation_tab(self) -> QWidget:
        result = self._mock_data.decision
        tab = QWidget()
        tab.setObjectName("decision_explanation_tab")
        layout = QVBoxLayout(tab)

        heading = QLabel(f"{result.setup_name}: {result.status}")
        heading.setObjectName("decision_status")
        heading.setStyleSheet("font-size: 16px; font-weight: 600;")
        layout.addWidget(heading)

        conditions = QListWidget()
        conditions.setObjectName("decision_condition_list")
        for condition in result.conditions:
            conditions.addItem(f"[{condition.status}] {condition.message}")
        layout.addWidget(conditions)
        return tab

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
            button.setToolTip("Placeholder control; no broker action is wired yet.")
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

    def _current_dashboard_snapshot(self) -> DashboardSnapshot:
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


def _prototype_counters(snapshot: PrototypeDashboardSnapshot) -> str:
    return (
        f"depth={snapshot.depth_events}, trades={snapshot.trade_events}, "
        f"control={snapshot.control_events}"
    )


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
    return str(value.normalize())


def _optional_decimal(value: Decimal | None) -> str:
    if value is None:
        return "none"
    return _format_decimal(value)


if __name__ == "__main__":
    raise SystemExit(main())
