"""GUI screen for the Bidirectional Paper Trading Lab (paper-only, replay-fed).

Self-contained: it drives the isolated lab engine over a RECORDED session (read
only) in a worker thread and renders the result. It holds no backend snapshot,
touches no runtime state, and imports no live-execution code. ``render`` is a
deliberate no-op - the lab is self-driven, not fed by the live snapshot loop.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

_RAW_ROOT = Path("data/raw")


def _dec(value: float) -> Decimal:
    return Decimal(str(value))


class _ReplayWorker(QThread):
    """Runs a lab replay off the GUI thread and emits a results payload."""

    done = Signal(dict)

    def __init__(self, params: dict) -> None:
        super().__init__()
        self._p = params

    def run(self) -> None:  # noqa: D401 - QThread entry point
        try:
            self.done.emit(_run_replay(self._p))
        except Exception as error:  # noqa: BLE001 - report, never crash the GUI
            self.done.emit({"error": f"{type(error).__name__}: {error}"})


def _run_replay(p: dict) -> dict:
    from app.labs.bidirectional.config import (
        AccountConfig,
        ActivationSpec,
        BreakEvenConfig,
        TrailConfig,
    )
    from app.labs.bidirectional.engine import LabEngine
    from app.labs.bidirectional.feed import replay_market_events
    from app.labs.bidirectional.statistics import compute_statistics

    session_dir: Path = p["session_dir"]
    account = AccountConfig(
        starting_balance=p["starting_balance"], tick_size=p["tick_size"],
        tick_value=p["tick_value"], commission_per_contract=p["commission"],
        entry_slippage_ticks=p["entry_slip"], exit_slippage_ticks=p["exit_slip"],
        stop_slippage_ticks=p["stop_slip"],
        max_gross_contracts=max(1, (p["long"] + p["short"]) * max(1, len(p["levels"])) * 8),
    )
    engine = LabEngine(account)
    be = BreakEvenConfig(enabled=p["be_trigger"] > 0, trigger_ticks=p["be_trigger"], offset_ticks=p["be_offset"])
    tr = TrailConfig(enabled=p["trail_dist"] > 0, activation_ticks=p["trail_act"], distance_ticks=p["trail_dist"])

    events = replay_market_events(session_dir, max_events=p["max_events"] or None)
    first = next(events, None)
    if first is None:
        return {"error": f"No market events in {session_dir.name}"}
    engine.on_event(first)
    for i, price in enumerate(p["levels"]):
        engine.arm(ActivationSpec(
            activation_id=str(i + 1), price=price, long_qty=p["long"], short_qty=p["short"],
            stop_ticks=p["stop_ticks"], break_even=be, trailing=tr,
            one_shot=p["one_shot"], max_activations=1 if p["one_shot"] else 1_000_000,
            require_leave_reenter=True, leave_distance_ticks=p["stop_ticks"] * 2,
        ))
    saw_book = bool(first.bid and first.ask)
    for ev in events:
        engine.on_event(ev)
        saw_book = saw_book or bool(ev.bid and ev.ask)

    stats = compute_statistics(engine)
    levels = [
        (st.spec.activation_id, str(st.spec.price), st.status, st.activations, len(st.setup_ids))
        for st in engine.levels.values()
    ]
    return {
        "stats": stats,
        "levels": levels,
        "log_tail": [f"{e.ts_ns}  {e.text}" for e in engine.log[-400:]],
        "warning": "" if saw_book else (
            "No bid/ask in this data; tight-stop and activation simulation may be unreliable "
            "because the exact intrabar price sequence is unavailable."),
    }


class BidirectionalLabScreen(QWidget):
    """Configure and run the simultaneous long+short activation experiment (paper)."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("screen_bidirectional_lab")
        self._worker: _ReplayWorker | None = None
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(1000)
        self._live_timer.timeout.connect(self._poll_live_state)

        outer = QVBoxLayout(self)
        title = QLabel("Bidirectional Paper Trading Lab")
        title.setStyleSheet("font-size: 18px; font-weight: 600;")
        subtitle = QLabel("PAPER ONLY - isolated experiment. Simultaneous large long+short at pre-set "
                          "activation levels, ultra-tight stops, replayed over RECORDED ticks. "
                          "No real order can be sent.")
        subtitle.setWordWrap(True)
        outer.addWidget(title)
        outer.addWidget(subtitle)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        body.addWidget(self._build_config_group())
        body.addWidget(self._build_levels_group())
        controls = QHBoxLayout()
        self.run_button = QPushButton("Run Replay")
        self.run_button.setObjectName("lab_run")
        self.run_button.clicked.connect(self._on_run)
        self.reset_button = QPushButton("Reset")
        self.reset_button.clicked.connect(self._on_reset)
        self.stop_live_button = QPushButton("Stop Live")
        self.stop_live_button.setObjectName("lab_stop_live")
        self.stop_live_button.clicked.connect(self._on_stop_live)
        self.stop_live_button.hide()
        controls.addWidget(self.run_button)
        controls.addWidget(self.stop_live_button)
        controls.addWidget(self.reset_button)
        controls.addStretch(1)
        self.status_label = QLabel("Idle. Configure levels and press Run Replay.")
        controls.addWidget(self.status_label)
        body.addLayout(controls)
        body.addWidget(self._build_results_group())

    # -- config -----------------------------------------------------------------

    def _build_config_group(self) -> QGroupBox:
        box = QGroupBox("Data source and account (paper)")
        form = QFormLayout(box)
        self.source_combo = QComboBox()
        self.source_combo.addItems(["Recorded replay", "Live (Bookmap)"])
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        form.addRow("Data source", self.source_combo)
        self.session_combo = QComboBox()
        for path in _recorded_sessions():
            self.session_combo.addItem(path.name, path)
        if self.session_combo.count() == 0:
            self.session_combo.addItem("(no recorded sessions found under data/raw)", None)
        form.addRow("Recorded session", self.session_combo)

        self.max_events = _int_spin(0, 50_000_000, 500_000, step=50_000)
        form.addRow("Max events (0 = all)", self.max_events)
        self.starting_balance = _float_spin(0, 1e12, 100_000, decimals=2, step=1000)
        form.addRow("Starting balance", self.starting_balance)
        self.tick_size = _float_spin(0.0001, 1000, 0.25, decimals=4, step=0.25)
        form.addRow("Tick size", self.tick_size)
        self.tick_value = _float_spin(0.0001, 100000, 0.50, decimals=4, step=0.25)
        form.addRow("Value per tick ($)", self.tick_value)
        self.commission = _float_spin(0, 100, 0.62, decimals=2, step=0.1)
        form.addRow("Commission / contract / side", self.commission)
        self.entry_slip = _float_spin(0, 50, 0, decimals=2, step=1)
        form.addRow("Entry slippage (ticks)", self.entry_slip)
        self.exit_slip = _float_spin(0, 50, 0, decimals=2, step=1)
        form.addRow("Exit slippage (ticks)", self.exit_slip)
        self.stop_slip = _float_spin(0, 50, 1, decimals=2, step=1)
        form.addRow("Stop slippage (ticks)", self.stop_slip)

        self.long_qty = _int_spin(0, 1_000_000, 100, step=10)
        form.addRow("LONG quantity", self.long_qty)
        self.short_qty = _int_spin(0, 1_000_000, 100, step=10)
        form.addRow("SHORT quantity", self.short_qty)
        self.stop_ticks = _float_spin(1, 10000, 2, decimals=2, step=1)
        form.addRow("Initial stop (ticks)", self.stop_ticks)
        self.be_trigger = _float_spin(0, 10000, 3, decimals=2, step=1)
        form.addRow("Break-even trigger (ticks, 0=off)", self.be_trigger)
        self.be_offset = _float_spin(0, 10000, 0, decimals=2, step=1)
        form.addRow("Break-even offset (ticks)", self.be_offset)
        self.trail_act = _float_spin(0, 10000, 5, decimals=2, step=1)
        form.addRow("Trailing activation (ticks)", self.trail_act)
        self.trail_dist = _float_spin(0, 10000, 2, decimals=2, step=1)
        form.addRow("Trailing distance (ticks, 0=off)", self.trail_dist)
        self.one_shot = QComboBox()
        self.one_shot.addItems(["repeat (leave & re-enter)", "one-shot"])
        form.addRow("Activation mode", self.one_shot)
        return box

    def _build_levels_group(self) -> QGroupBox:
        box = QGroupBox("Pre-set paired activation levels (price)")
        layout = QVBoxLayout(box)
        self.levels_table = QTableWidget(0, 1)
        self.levels_table.setHorizontalHeaderLabels(["Activation price"])
        self.levels_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.levels_table)
        row = QHBoxLayout()
        add = QPushButton("Add level")
        add.clicked.connect(lambda: self._add_level_row(""))
        remove = QPushButton("Remove selected")
        remove.clicked.connect(self._remove_level_row)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        layout.addLayout(row)
        return box

    def _build_results_group(self) -> QGroupBox:
        box = QGroupBox("Results (honest: costs subtracted, both legs counted, open legs excluded)")
        layout = QVBoxLayout(box)
        self.warning_label = QLabel("")
        self.warning_label.setStyleSheet("color: #d08000;")
        self.warning_label.setWordWrap(True)
        self.warning_label.hide()
        layout.addWidget(self.warning_label)
        self.results_view = QPlainTextEdit()
        self.results_view.setReadOnly(True)
        self.results_view.setObjectName("lab_results")
        self.results_view.setMinimumHeight(220)
        layout.addWidget(self.results_view)
        self.levels_status = QTableWidget(0, 5)
        self.levels_status.setHorizontalHeaderLabels(["ID", "Price", "Status", "Activations", "Setups"])
        self.levels_status.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(QLabel("Activation levels"))
        layout.addWidget(self.levels_status)
        layout.addWidget(QLabel("Event log (tail)"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setObjectName("lab_log")
        self.log_view.setMinimumHeight(160)
        layout.addWidget(self.log_view)
        return box

    # -- level table helpers ----------------------------------------------------

    def _add_level_row(self, text: str) -> None:
        r = self.levels_table.rowCount()
        self.levels_table.insertRow(r)
        self.levels_table.setItem(r, 0, QTableWidgetItem(text))

    def _remove_level_row(self) -> None:
        rows = sorted({i.row() for i in self.levels_table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.levels_table.removeRow(r)

    def _collect_levels(self) -> list[Decimal]:
        levels: list[Decimal] = []
        for r in range(self.levels_table.rowCount()):
            item = self.levels_table.item(r, 0)
            if item is None or not item.text().strip():
                continue
            try:
                levels.append(Decimal(item.text().strip()))
            except InvalidOperation:
                continue
        return levels

    # -- run --------------------------------------------------------------------

    def _on_run(self) -> None:
        if self.source_combo.currentIndex() == 1:  # Live (Bookmap)
            self._arm_live()
            return
        if self._worker is not None and self._worker.isRunning():
            return
        session_dir = self.session_combo.currentData()
        if session_dir is None:
            self.status_label.setText("No recorded session selected.")
            return
        levels = self._collect_levels()
        if not levels:
            self.status_label.setText("Add at least one activation price.")
            return
        params = {
            "session_dir": Path(session_dir), "levels": levels,
            "max_events": int(self.max_events.value()),
            "starting_balance": _dec(self.starting_balance.value()),
            "tick_size": _dec(self.tick_size.value()), "tick_value": _dec(self.tick_value.value()),
            "commission": _dec(self.commission.value()), "entry_slip": _dec(self.entry_slip.value()),
            "exit_slip": _dec(self.exit_slip.value()), "stop_slip": _dec(self.stop_slip.value()),
            "long": int(self.long_qty.value()), "short": int(self.short_qty.value()),
            "stop_ticks": _dec(self.stop_ticks.value()), "be_trigger": _dec(self.be_trigger.value()),
            "be_offset": _dec(self.be_offset.value()), "trail_act": _dec(self.trail_act.value()),
            "trail_dist": _dec(self.trail_dist.value()),
            "one_shot": self.one_shot.currentIndex() == 1,
        }
        self.run_button.setEnabled(False)
        self.status_label.setText(f"Replaying {params['session_dir'].name} …")
        self._worker = _ReplayWorker(params)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, result: dict) -> None:
        self.run_button.setEnabled(True)
        if result.get("error"):
            self.status_label.setText("Error.")
            self.results_view.setPlainText(str(result["error"]))
            return
        warning = result.get("warning") or ""
        self.warning_label.setText(warning)
        self.warning_label.setVisible(bool(warning))
        self.results_view.setPlainText(_format_stats(result["stats"]))
        rows = result["levels"]
        self.levels_status.setRowCount(len(rows))
        for r, (aid, price, status, acts, setups) in enumerate(rows):
            for c, value in enumerate((aid, price, status, str(acts), str(setups))):
                self.levels_status.setItem(r, c, QTableWidgetItem(value))
        self.log_view.setPlainText("\n".join(result["log_tail"]))
        self.status_label.setText("Done.")

    def _on_reset(self) -> None:
        self.results_view.clear()
        self.log_view.clear()
        self.levels_status.setRowCount(0)
        self.warning_label.hide()
        self.status_label.setText("Idle.")

    # -- live (Bookmap) mode ----------------------------------------------------

    def _on_source_changed(self, index: int) -> None:
        live = index == 1
        self.run_button.setText("Arm Live" if live else "Run Replay")
        self.session_combo.setEnabled(not live)
        self.max_events.setEnabled(not live)
        if not live:
            self._on_stop_live()
        self.status_label.setText(
            "Live: set levels, press Arm Live. The backend must have the lab live tap enabled "
            "(paper_bidirectional_lab_live_enabled) and be receiving Bookmap." if live
            else "Idle. Configure levels and press Run Replay.")

    def _config_payload(self, enabled: bool) -> dict:
        return {
            "enabled": enabled,
            "starting_balance": str(self.starting_balance.value()),
            "tick_size": str(self.tick_size.value()), "tick_value": str(self.tick_value.value()),
            "commission": str(self.commission.value()), "entry_slip": str(self.entry_slip.value()),
            "exit_slip": str(self.exit_slip.value()), "stop_slip": str(self.stop_slip.value()),
            "long": int(self.long_qty.value()), "short": int(self.short_qty.value()),
            "stop_ticks": str(self.stop_ticks.value()), "be_trigger": str(self.be_trigger.value()),
            "be_offset": str(self.be_offset.value()), "trail_act": str(self.trail_act.value()),
            "trail_dist": str(self.trail_dist.value()),
            "one_shot": self.one_shot.currentIndex() == 1,
            "levels": [str(p) for p in self._collect_levels()],
        }

    def _arm_live(self) -> None:
        levels = self._collect_levels()
        if not levels:
            self.status_label.setText("Add at least one activation price to arm live.")
            return
        try:
            from app.labs.bidirectional.live import LabConfigFile

            LabConfigFile("runtime").write(self._config_payload(enabled=True))
        except Exception as error:  # noqa: BLE001
            self.status_label.setText(f"Could not arm live: {error}")
            return
        self.stop_live_button.show()
        self._live_timer.start()
        self.status_label.setText("Armed live. Waiting for the backend to publish results …")

    def _on_stop_live(self) -> None:
        self._live_timer.stop()
        self.stop_live_button.hide()
        try:
            from app.labs.bidirectional.live import LabConfigFile

            LabConfigFile("runtime").write(self._config_payload(enabled=False))
        except Exception:  # noqa: BLE001 - stopping must never raise
            pass

    def _poll_live_state(self) -> None:
        try:
            from app.labs.bidirectional.live import LabStateFile

            state = LabStateFile("runtime").read()
        except Exception:  # noqa: BLE001
            state = None
        if not state or "stats" not in state:
            self.status_label.setText("Armed live. No results published yet (backend tap enabled?).")
            return
        self.results_view.setPlainText(_format_stats(state["stats"]))
        rows = state.get("levels", [])
        self.levels_status.setRowCount(len(rows))
        for r, lvl in enumerate(rows):
            values = (lvl.get("id", ""), lvl.get("price", ""), lvl.get("status", ""),
                      str(lvl.get("activations", 0)), str(lvl.get("setups", 0)))
            for c, value in enumerate(values):
                self.levels_status.setItem(r, c, QTableWidgetItem(str(value)))
        self.log_view.setPlainText("\n".join(state.get("log_tail", [])))
        self.status_label.setText("Live (Bookmap) — updating.")

    # -- Screen contract --------------------------------------------------------

    def render(self, snapshot: object) -> None:  # noqa: ARG002 - self-driven; ignores the live snapshot
        """No-op: the lab runs on recorded replay, never the live snapshot loop."""
        return


def _recorded_sessions() -> list[Path]:
    try:
        from app.labs.bidirectional.feed import recorded_session_dirs

        return list(reversed(recorded_session_dirs(_RAW_ROOT)))[:60]
    except Exception:  # noqa: BLE001 - a missing data dir just means an empty picker
        return []


def _int_spin(lo: int, hi: int, value: int, *, step: int = 1) -> QSpinBox:
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setValue(value)
    return s


def _float_spin(lo: float, hi: float, value: float, *, decimals: int = 2, step: float = 1.0) -> QDoubleSpinBox:
    s = QDoubleSpinBox()
    s.setDecimals(decimals)
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setValue(value)
    return s


def _format_stats(stats: dict) -> str:
    lines: list[str] = []
    for section in ("account", "general", "paired", "activation"):
        lines.append(f"[{section}]")
        for key, value in stats[section].items():
            if isinstance(value, Decimal):
                lines.append(f"  {key:<26} {value:,.2f}")
            else:
                lines.append(f"  {key:<26} {value}")
        lines.append("")
    return "\n".join(lines)
