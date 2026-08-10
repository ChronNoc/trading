"""GUI screen for the Bidirectional Paper Trading Lab (paper-only).

Self-contained: it drives the isolated lab engine either over a RECORDED session
(read only, in a worker thread) or off the LIVE Bookmap tap (polled from a
runtime file). It holds no backend snapshot, touches no runtime state, and
imports no live-execution code. ``render`` is a deliberate no-op - the lab is
self-driven, not fed by the live snapshot loop.

The layout is deliberately plain: the few things you need are up top in ordinary
words; everything technical is tucked into collapsible "Advanced" / "Technical
details" sections so a first-time user is never buried in jargon.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtGui import QColor
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

_START_HINT = ("Nothing is running yet. Type a price (or a few) above, press Start, and when the "
               "price reaches one of them you'll see a BUY and a SELL appear here — each with a "
               "tight stop, and their live profit/loss. It's paper money; nothing real is ordered.")


def _dec(value: float) -> Decimal:
    return Decimal(str(value))


def _to_dec(value: object) -> Decimal:
    """Tolerant Decimal: handles Decimal, str (JSON round-trip) or None."""
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _money_color(amount: Decimal) -> QColor:
    return QColor("#1a9d55") if amount >= 0 else QColor("#c0392b")


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
    from app.labs.bidirectional.statistics import compute_statistics, open_positions, recent_trades

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
        {"id": st.spec.activation_id, "price": str(st.spec.price), "status": st.status,
         "activations": st.activations, "setups": len(st.setup_ids)}
        for st in engine.levels.values()
    ]
    return {
        "stats": stats,
        "positions": open_positions(engine),
        "recent_trades": recent_trades(engine),
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
        subtitle = QLabel("Practice sandbox — paper money only. It opens a big BUY and a big SELL at "
                          "the same moment when price hits a level you pick, keeps a tight stop on each, "
                          "and lets the winning side run. Nothing real is ever ordered.")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color:#666;")
        outer.addWidget(title)
        outer.addWidget(subtitle)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        body = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        body.addWidget(self._build_essentials_group())
        body.addWidget(self._build_advanced_group())

        controls = QHBoxLayout()
        self.run_button = QPushButton("Start")
        self.run_button.setObjectName("lab_run")
        self.run_button.clicked.connect(self._on_run)
        self.stop_live_button = QPushButton("Stop")
        self.stop_live_button.setObjectName("lab_stop_live")
        self.stop_live_button.clicked.connect(self._on_stop_live)
        self.stop_live_button.hide()
        self.reset_button = QPushButton("Clear")
        self.reset_button.clicked.connect(self._on_reset)
        controls.addWidget(self.run_button)
        controls.addWidget(self.stop_live_button)
        controls.addWidget(self.reset_button)
        controls.addStretch(1)
        self.status_label = QLabel("")
        controls.addWidget(self.status_label)
        body.addLayout(controls)

        body.addWidget(self._build_results_group())

        # Default to the live Bookmap feed - that's what "watch it live" means.
        self.source_combo.setCurrentIndex(1)
        self._on_source_changed(1)

    # -- set-up (the only things most people touch) -----------------------------

    def _build_essentials_group(self) -> QGroupBox:
        box = QGroupBox("Set it up")
        v = QVBoxLayout(box)
        how = QLabel("How it works: when the price reaches a price you list below, the bot instantly "
                     "opens a BUY and a SELL of the same size, puts a tight stop on each, and lets the "
                     "winning side keep running. Paper money — nothing real is ordered.")
        how.setWordWrap(True)
        how.setStyleSheet("color:#666;")
        v.addWidget(how)

        form = QFormLayout()
        self.source_combo = QComboBox()
        self.source_combo.addItems(["Recorded data (test on a past session)", "Live (Bookmap, right now)"])
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        form.addRow("Run on", self.source_combo)
        self.size_qty = _int_spin(1, 1_000_000, 100, step=10)
        form.addRow("Contracts on each side", self.size_qty)
        self.stop_ticks = _float_spin(1, 10000, 2, decimals=2, step=1)
        form.addRow("Stop size (ticks — 1 tick = 0.25 pts)", self.stop_ticks)
        v.addLayout(form)

        # Recorded-session picker: only relevant when running on past data.
        self.session_row = QWidget()
        srow = QFormLayout(self.session_row)
        srow.setContentsMargins(0, 0, 0, 0)
        self.session_combo = QComboBox()
        for path in _recorded_sessions():
            self.session_combo.addItem(path.name, path)
        if self.session_combo.count() == 0:
            self.session_combo.addItem("(no recorded sessions found under data/raw)", None)
        srow.addRow("Recorded session", self.session_combo)
        v.addWidget(self.session_row)

        v.addWidget(QLabel("Prices to trade at (type a price, one per row):"))
        self.levels_table = QTableWidget(0, 1)
        self.levels_table.setHorizontalHeaderLabels(["Price"])
        self.levels_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.levels_table.setMinimumHeight(90)
        v.addWidget(self.levels_table)
        row = QHBoxLayout()
        add = QPushButton("Add price")
        add.clicked.connect(lambda: self._add_level_row(""))
        remove = QPushButton("Remove selected")
        remove.clicked.connect(self._remove_level_row)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        v.addLayout(row)
        return box

    def _build_advanced_group(self) -> QGroupBox:
        inner = QWidget()
        form = QFormLayout(inner)
        self.starting_balance = _float_spin(0, 1e12, 100_000, decimals=2, step=1000)
        form.addRow("Starting balance ($)", self.starting_balance)
        self.tick_size = _float_spin(0.0001, 1000, 0.25, decimals=4, step=0.25)
        form.addRow("Tick size", self.tick_size)
        self.tick_value = _float_spin(0.0001, 100000, 0.50, decimals=4, step=0.25)
        form.addRow("Value per tick ($)", self.tick_value)
        self.commission = _float_spin(0, 100, 0.62, decimals=2, step=0.1)
        form.addRow("Commission per contract per side ($)", self.commission)
        self.entry_slip = _float_spin(0, 50, 0, decimals=2, step=1)
        form.addRow("Entry slippage (ticks)", self.entry_slip)
        self.exit_slip = _float_spin(0, 50, 0, decimals=2, step=1)
        form.addRow("Exit slippage (ticks)", self.exit_slip)
        self.stop_slip = _float_spin(0, 50, 1, decimals=2, step=1)
        form.addRow("Stop slippage (ticks)", self.stop_slip)
        self.be_trigger = _float_spin(0, 10000, 3, decimals=2, step=1)
        form.addRow("Break-even trigger (ticks, 0=off)", self.be_trigger)
        self.be_offset = _float_spin(0, 10000, 0, decimals=2, step=1)
        form.addRow("Break-even offset (ticks)", self.be_offset)
        self.trail_act = _float_spin(0, 10000, 5, decimals=2, step=1)
        form.addRow("Trailing start (ticks)", self.trail_act)
        self.trail_dist = _float_spin(0, 10000, 2, decimals=2, step=1)
        form.addRow("Trailing distance (ticks, 0=off)", self.trail_dist)
        self.one_shot = QComboBox()
        self.one_shot.addItems(["Repeat (re-arm after price leaves & returns)", "One-shot (fire only once)"])
        form.addRow("Repeat behaviour", self.one_shot)
        self.max_events = _int_spin(0, 50_000_000, 500_000, step=50_000)
        form.addRow("Max events for replay (0 = all)", self.max_events)
        return _collapsible_group(
            "Advanced settings (optional — good defaults are already set)", inner, expanded=False)

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

    # -- results ----------------------------------------------------------------

    def _build_results_group(self) -> QGroupBox:
        box = QGroupBox("What's happening")
        v = QVBoxLayout(box)
        self.warning_label = QLabel("")
        self.warning_label.setStyleSheet("color: #d08000;")
        self.warning_label.setWordWrap(True)
        self.warning_label.hide()
        v.addWidget(self.warning_label)

        self.summary_label = QLabel(_START_HINT)
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("font-size: 14px;")
        v.addWidget(self.summary_label)

        v.addWidget(QLabel("Open orders (live):"))
        self.positions_table = QTableWidget(0, 6)
        self.positions_table.setObjectName("lab_positions")
        self.positions_table.setHorizontalHeaderLabels(
            ["Side", "Contracts", "Entry", "Stop", "Status", "P&L now $"])
        self.positions_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.positions_table.setMinimumHeight(120)
        v.addWidget(self.positions_table)

        v.addWidget(QLabel("Recent orders (newest first):"))
        self.recent_table = QTableWidget(0, 6)
        self.recent_table.setObjectName("lab_recent")
        self.recent_table.setHorizontalHeaderLabels(
            ["Side", "Contracts", "Entry", "Exit", "Why closed", "Result $"])
        self.recent_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.recent_table.setMinimumHeight(120)
        v.addWidget(self.recent_table)

        # Everything jargon-heavy lives here, collapsed by default.
        tech = QWidget()
        tv = QVBoxLayout(tech)
        tv.setContentsMargins(0, 0, 0, 0)
        self.results_view = QPlainTextEdit()
        self.results_view.setReadOnly(True)
        self.results_view.setObjectName("lab_results")
        self.results_view.setMinimumHeight(200)
        tv.addWidget(QLabel("Full statistics"))
        tv.addWidget(self.results_view)
        self.levels_status = QTableWidget(0, 5)
        self.levels_status.setHorizontalHeaderLabels(["ID", "Price", "Status", "Activations", "Setups"])
        self.levels_status.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        tv.addWidget(QLabel("Activation levels"))
        tv.addWidget(self.levels_status)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setObjectName("lab_log")
        self.log_view.setMinimumHeight(140)
        tv.addWidget(QLabel("Event log (tail)"))
        tv.addWidget(self.log_view)
        v.addWidget(_collapsible_group(
            "Technical details (full numbers & event log)", tech, expanded=False))
        return box

    def _render_levels(self, rows: list) -> None:
        self.levels_status.setRowCount(len(rows))
        for r, lvl in enumerate(rows):
            values = (lvl.get("id", ""), lvl.get("price", ""), lvl.get("status", ""),
                      str(lvl.get("activations", 0)), str(lvl.get("setups", 0)))
            for c, value in enumerate(values):
                self.levels_status.setItem(r, c, QTableWidgetItem(str(value)))

    def _render_positions(self, rows: list) -> None:
        self.positions_table.setRowCount(len(rows))
        for r, pos in enumerate(rows):
            unreal = _to_dec(pos.get("unrealized"))
            status = ("trailing stop" if pos.get("trailing")
                      else "at break-even" if pos.get("break_even") else "holding")
            cells = (str(pos.get("side", "")).upper(), str(pos.get("qty", "")), str(pos.get("entry", "")),
                     str(pos.get("stop", "")), status, f"{unreal:,.2f}")
            for c, value in enumerate(cells):
                item = QTableWidgetItem(value)
                if c == 5:
                    item.setForeground(_money_color(unreal))
                self.positions_table.setItem(r, c, item)

    def _render_recent(self, rows: list) -> None:
        self.recent_table.setRowCount(len(rows))
        for r, trade in enumerate(rows):
            net = _to_dec(trade.get("net"))
            cells = (str(trade.get("side", "")).upper(), str(trade.get("qty", "")), str(trade.get("entry", "")),
                     str(trade.get("exit", "")), _why_closed(str(trade.get("reason", ""))), f"{net:,.2f}")
            for c, value in enumerate(cells):
                item = QTableWidgetItem(value)
                if c == 5:
                    item.setForeground(_money_color(net))
                self.recent_table.setItem(r, c, item)

    # -- run --------------------------------------------------------------------

    def _on_run(self) -> None:
        if self.source_combo.currentIndex() == 1:  # Live (Bookmap)
            self._arm_live()
            return
        if self._worker is not None and self._worker.isRunning():
            return
        session_dir = self.session_combo.currentData()
        if session_dir is None:
            self.status_label.setText("Pick a recorded session first.")
            return
        levels = self._collect_levels()
        if not levels:
            self.status_label.setText("Add at least one price to trade at.")
            return
        size = int(self.size_qty.value())
        params = {
            "session_dir": Path(session_dir), "levels": levels,
            "max_events": int(self.max_events.value()),
            "starting_balance": _dec(self.starting_balance.value()),
            "tick_size": _dec(self.tick_size.value()), "tick_value": _dec(self.tick_value.value()),
            "commission": _dec(self.commission.value()), "entry_slip": _dec(self.entry_slip.value()),
            "exit_slip": _dec(self.exit_slip.value()), "stop_slip": _dec(self.stop_slip.value()),
            "long": size, "short": size,
            "stop_ticks": _dec(self.stop_ticks.value()), "be_trigger": _dec(self.be_trigger.value()),
            "be_offset": _dec(self.be_offset.value()), "trail_act": _dec(self.trail_act.value()),
            "trail_dist": _dec(self.trail_dist.value()),
            "one_shot": self.one_shot.currentIndex() == 1,
        }
        self.run_button.setEnabled(False)
        self.status_label.setText(f"Testing on {params['session_dir'].name} …")
        self._worker = _ReplayWorker(params)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, result: dict) -> None:
        self.run_button.setEnabled(True)
        if result.get("error"):
            self.status_label.setText("Error.")
            self.summary_label.setText(str(result["error"]))
            self.results_view.setPlainText(str(result["error"]))
            return
        warning = result.get("warning") or ""
        self.warning_label.setText(warning)
        self.warning_label.setVisible(bool(warning))
        self.summary_label.setText(_plain_summary(result))
        self.results_view.setPlainText(_format_stats(result["stats"]))
        self._render_positions(result.get("positions", []))
        self._render_recent(result.get("recent_trades", []))
        self._render_levels(result.get("levels", []))
        self.log_view.setPlainText("\n".join(result["log_tail"]))
        self.status_label.setText("Done.")

    def _on_reset(self) -> None:
        self.summary_label.setText(_START_HINT)
        self.results_view.clear()
        self.log_view.clear()
        self.levels_status.setRowCount(0)
        self.positions_table.setRowCount(0)
        self.recent_table.setRowCount(0)
        self.warning_label.hide()
        self.status_label.setText("")

    # -- live (Bookmap) mode ----------------------------------------------------

    def _on_source_changed(self, index: int) -> None:
        live = index == 1
        self.run_button.setText("Start (Live)" if live else "Start (test)")
        self.session_row.setVisible(not live)
        if not live:
            self._on_stop_live()
        self.status_label.setText(
            "Live: type your price(s), then press Start (Live). Needs Bookmap running and feeding."
            if live else "Test mode: pick a saved session and price(s), then press Start (test).")

    def _config_payload(self, enabled: bool) -> dict:
        size = int(self.size_qty.value())
        return {
            "enabled": enabled,
            "starting_balance": str(self.starting_balance.value()),
            "tick_size": str(self.tick_size.value()), "tick_value": str(self.tick_value.value()),
            "commission": str(self.commission.value()), "entry_slip": str(self.entry_slip.value()),
            "exit_slip": str(self.exit_slip.value()), "stop_slip": str(self.stop_slip.value()),
            "long": size, "short": size,
            "stop_ticks": str(self.stop_ticks.value()), "be_trigger": str(self.be_trigger.value()),
            "be_offset": str(self.be_offset.value()), "trail_act": str(self.trail_act.value()),
            "trail_dist": str(self.trail_dist.value()),
            "one_shot": self.one_shot.currentIndex() == 1,
            "levels": [str(p) for p in self._collect_levels()],
        }

    def _arm_live(self) -> None:
        levels = self._collect_levels()
        if not levels:
            self.status_label.setText("Add at least one price to trade at, then press Start.")
            return
        try:
            from app.labs.bidirectional.live import LabConfigFile

            LabConfigFile("runtime").write(self._config_payload(enabled=True))
        except Exception as error:  # noqa: BLE001
            self.status_label.setText(f"Could not start live: {error}")
            return
        self.stop_live_button.show()
        self._live_timer.start()
        self.summary_label.setText("Started. Waiting for price to reach one of your levels …")
        self.status_label.setText("Live — armed. Watching Bookmap …")

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
            self.status_label.setText("Live — armed. No results yet (is the Bookmap feed running?).")
            return
        self.summary_label.setText(_plain_summary(state))
        self.results_view.setPlainText(_format_stats(state["stats"]))
        self._render_positions(state.get("positions", []))
        self._render_recent(state.get("recent_trades", []))
        self._render_levels(state.get("levels", []))
        self.log_view.setPlainText("\n".join(state.get("log_tail", [])))
        open_n = len(state.get("positions", []))
        self.status_label.setText(f"Live — updating. {open_n} order(s) open.")

    # -- Screen contract --------------------------------------------------------

    def render(self, snapshot: object) -> None:  # noqa: ARG002 - self-driven; ignores the live snapshot
        """No-op: the lab runs itself, never the live snapshot loop."""
        return


def _collapsible_group(title: str, inner: QWidget, *, expanded: bool = False) -> QGroupBox:
    """A checkable group whose contents hide when unchecked (a simple expander)."""
    box = QGroupBox(title)
    box.setCheckable(True)
    box.setChecked(expanded)
    layout = QVBoxLayout(box)
    layout.addWidget(inner)
    inner.setVisible(expanded)
    box.toggled.connect(inner.setVisible)
    return box


def _why_closed(reason: str) -> str:
    mapping = {
        "stop": "stopped out (hit its stop)",
        "session_close": "closed at session end",
        "flatten": "closed manually",
        "": "still open",
    }
    return mapping.get(reason, reason)


def _plain_summary(payload: dict) -> str:
    """A few lines of plain English describing the open orders and the score so far."""
    positions = payload.get("positions", []) or []
    stats = payload.get("stats") or {}
    general = stats.get("general", {}) if isinstance(stats, dict) else {}

    lines: list[str] = []
    if positions:
        total = sum((_to_dec(p.get("unrealized")) for p in positions), Decimal("0"))
        word = "up" if total >= 0 else "down"
        lines.append(f"OPEN NOW: {len(positions)} order(s), together {word} ${abs(total):,.2f} (paper).")
        for p in positions:
            unreal = _to_dec(p.get("unrealized"))
            w = "up" if unreal >= 0 else "down"
            extra = " — trailing stop" if p.get("trailing") else (" — at break-even" if p.get("break_even") else "")
            lines.append(f"   • {str(p.get('side', '')).upper()} {p.get('qty', '')} @ {p.get('entry', '')}"
                         f"  ({w} ${abs(unreal):,.2f}){extra}")
    else:
        lines.append("OPEN NOW: nothing open. When price reaches a level, a BUY and a SELL appear here.")

    total_trades = general.get("total_trades", 0)
    if total_trades:
        net = _to_dec(general.get("net_pnl", "0"))
        wins = general.get("wins", 0)
        losses = general.get("losses", 0)
        verb = "made" if net >= 0 else "lost"
        lines.append(f"DONE SO FAR: {total_trades} order(s) closed — {wins} won, {losses} lost. "
                     f"Overall you {verb} ${abs(net):,.2f} (paper / fake money).")
    else:
        lines.append("DONE SO FAR: no orders have closed yet.")
    return "\n".join(lines)


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
