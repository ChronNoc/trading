"""The eight screens of the redesigned window.

Each screen owns retained widget references and exposes ``render(snapshot)``.
No screen reads disk, network, or research state: it formats the immutable
:class:`~app.gui.view_models.AppSnapshot` it is handed. That is what keeps the
Qt thread free and the receiver un-starved.
"""

from __future__ import annotations

from decimal import Decimal

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.gui.view_models import AppSnapshot, Capability, Health

# Screen identifiers double as the sidebar order.
OVERVIEW = "Overview"
LIVE_ORDER_FLOW = "Live Order Flow"
PAPER_TRADING = "Paper Trading"
SESSIONS_REPLAY = "Sessions and Replay"
RESEARCH_HEALTH = "Research and Model Health"
RISK_LUCID = "Risk and Lucid Account"
EXECUTION = "Execution"
DIAGNOSTICS = "Diagnostics and Settings"

SCREEN_ORDER = (
    OVERVIEW, LIVE_ORDER_FLOW, PAPER_TRADING, SESSIONS_REPLAY,
    RESEARCH_HEALTH, RISK_LUCID, EXECUTION, DIAGNOSTICS,
)

_HEALTH_TEXT = {
    Health.OK: "OK", Health.WARN: "WARNING", Health.FAIL: "FAILED",
    Health.IDLE: "idle", Health.LOCKED: "LOCKED",
}


def _value(object_name: str, initial: str = "—") -> QLabel:
    label = QLabel(initial)
    label.setObjectName(object_name)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _field(grid: QGridLayout, row: int, column: int, caption: str, value: QLabel) -> None:
    """Place a compact caption/value pair (no cards inside cards)."""
    name = QLabel(caption)
    name.setProperty("role", "caption")
    grid.addWidget(name, row, column * 2)
    grid.addWidget(value, row, column * 2 + 1)


def _money(amount: Decimal | None) -> str:
    return "—" if amount is None else f"${amount:,.2f}"


def _price(amount: Decimal | None) -> str:
    return "—" if amount is None else f"{amount:,.2f}"


class Screen(QWidget):
    """Base screen with a retained-widget render contract."""

    def render(self, snapshot: AppSnapshot) -> None:
        """Update retained widgets from an immutable snapshot."""
        raise NotImplementedError


class OverviewScreen(Screen):
    """Everything that matters, fitting 1366x768 without scrolling."""

    def __init__(self) -> None:
        """Build the overview with retained labels (no findChild on refresh)."""
        super().__init__()
        self.setObjectName("screen_overview")
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        self.state_label = _value("overview_state", "Starting…")
        self.state_label.setProperty("role", "headline")
        self.provenance = _value("overview_provenance")
        header = QHBoxLayout()
        header.addWidget(self.state_label)
        header.addStretch(1)
        header.addWidget(self.provenance)
        layout.addLayout(header)

        # --- market -----------------------------------------------------------
        market = QGroupBox("Market")
        market_grid = QGridLayout(market)
        self.contract = _value("overview_contract")
        self.last_price = _value("overview_last_price")
        self.bid = _value("overview_bid")
        self.ask = _value("overview_ask")
        self.spread = _value("overview_spread")
        self.mid = _value("overview_mid")
        _field(market_grid, 0, 0, "Contract", self.contract)
        _field(market_grid, 0, 1, "Last", self.last_price)
        _field(market_grid, 0, 2, "Spread", self.spread)
        _field(market_grid, 1, 0, "Bid", self.bid)
        _field(market_grid, 1, 1, "Ask", self.ask)
        _field(market_grid, 1, 2, "Mid", self.mid)
        layout.addWidget(market)

        # --- capture ----------------------------------------------------------
        capture = QGroupBox("Capture and recording")
        capture_grid = QGridLayout(capture)
        self.receiver = _value("overview_receiver")
        self.bookmap = _value("overview_bookmap")
        self.recording = _value("overview_recording")
        self.event_rate = _value("overview_event_rate")
        self.drops = _value("overview_drops")
        self.queue = _value("overview_queue")
        self.processing_age = _value("overview_processing_age")
        _field(capture_grid, 0, 0, "Receiver", self.receiver)
        _field(capture_grid, 0, 1, "Bookmap", self.bookmap)
        _field(capture_grid, 0, 2, "Recording", self.recording)
        _field(capture_grid, 1, 0, "Events/s", self.event_rate)
        _field(capture_grid, 1, 1, "Session drops", self.drops)
        _field(capture_grid, 1, 2, "Queue pressure", self.queue)
        _field(capture_grid, 2, 0, "App processing age", self.processing_age)
        layout.addWidget(capture)

        # --- setup + paper ----------------------------------------------------
        paper = QGroupBox("Delayed paper")
        paper_grid = QGridLayout(paper)
        self.setup_name = _value("overview_setup")
        self.decision = _value("overview_decision")
        self.position = _value("overview_position")
        self.risk_remaining = _value("overview_risk_remaining")
        _field(paper_grid, 0, 0, "Setup", self.setup_name)
        _field(paper_grid, 0, 1, "Decision", self.decision)
        _field(paper_grid, 1, 0, "Position", self.position)
        _field(paper_grid, 1, 1, "Risk remaining", self.risk_remaining)
        layout.addWidget(paper)

        # --- Lucid ------------------------------------------------------------
        lucid = QGroupBox("Lucid account")
        lucid_grid = QGridLayout(lucid)
        self.profile = _value("overview_profile")
        self.balance = _value("overview_balance")
        self.drawdown_room = _value("overview_drawdown_room")
        self.consistency = _value("overview_consistency")
        self.target_bar = QProgressBar()
        self.target_bar.setObjectName("overview_target_progress")
        self.target_bar.setRange(0, 100)
        self.target_bar.setFormat("Profit target: %p%")
        _field(lucid_grid, 0, 0, "Profile", self.profile)
        _field(lucid_grid, 0, 1, "Balance", self.balance)
        _field(lucid_grid, 1, 0, "Drawdown room", self.drawdown_room)
        _field(lucid_grid, 1, 1, "Consistency", self.consistency)
        lucid_grid.addWidget(self.target_bar, 2, 0, 1, 4)
        layout.addWidget(lucid)

        # --- setup checks: fills the remaining height with real content, so the
        # overview has no dead space and shows EVERY pass/fail reason.
        checks_box = QGroupBox("Setup checks (every pass/fail reason)")
        checks_layout = QVBoxLayout(checks_box)
        self.checks = _value("overview_setup_checks", "no setup evaluated yet")
        self.checks.setWordWrap(True)
        self.checks.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.checks.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        checks_layout.addWidget(self.checks)
        layout.addWidget(checks_box, 1)  # stretch=1: absorbs spare vertical space

        # --- health + LIVE lock ----------------------------------------------
        self.components = _value("overview_components")
        self.components.setWordWrap(True)
        self.live_lock = _value("overview_live_locked", "LIVE LOCKED")
        self.live_lock.setProperty("role", "locked")
        footer = QHBoxLayout()
        footer.addWidget(self.components, 1)
        footer.addWidget(self.live_lock)
        layout.addLayout(footer)

        self.next_action = _value("overview_next_action")
        self.next_action.setWordWrap(True)
        layout.addWidget(self.next_action)

    def render(self, snapshot: AppSnapshot) -> None:
        """Format the snapshot into retained labels."""
        market, capture, paper = snapshot.market, snapshot.capture, snapshot.paper
        self.state_label.setText(snapshot.plain_state)
        self.provenance.setText(market.provenance_text)
        self.contract.setText(f"{market.symbol} {market.contract}")
        self.last_price.setText(_price(market.last_price))
        self.bid.setText(_price(market.best_bid))
        self.ask.setText(_price(market.best_ask))
        self.spread.setText(_price(market.spread))
        self.mid.setText(_price(market.mid_price))

        self.receiver.setText("listening" if capture.receiver_listening else "not listening")
        self.bookmap.setText("connected" if capture.bookmap_connected else "waiting")
        self.recording.setText("yes" if capture.recording else "no")
        self.event_rate.setText(f"{market.event_rate_per_second:,.0f}")
        self.drops.setText(
            f"{capture.current_session_drops:,} this session"
            + (f" (lifetime {capture.lifetime_bridge_drops:,})"
               if capture.lifetime_bridge_drops != capture.current_session_drops else ""),
        )
        self.queue.setText(f"{capture.queue_pressure:.0%} [{_HEALTH_TEXT[capture.health]}]")
        self.processing_age.setText(
            "—" if market.processing_age_ms is None else f"{market.processing_age_ms} ms (excludes source delay)",
        )

        self.setup_name.setText(paper.setup_name)
        failed = [c for c in paper.setup_checks if not c.passed]
        self.decision.setText(
            "accepted" if paper.setup_checks and not failed
            else (f"rejected: {failed[0].name}" if failed else "awaiting evaluation"),
        )
        self.position.setText(paper.open_position)
        self.risk_remaining.setText(_money(paper.risk_remaining))

        self.profile.setText(paper.profile_name or "—")
        self.balance.setText(_money(paper.balance))
        self.drawdown_room.setText(_money(paper.drawdown_room))
        self.consistency.setText(
            "—" if paper.consistency_share is None else f"{paper.consistency_share:.0%}",
        )
        self.target_bar.setValue(int(paper.target_progress * 100))

        if paper.setup_checks:
            self.checks.setText("\n".join(
                f"[{'PASS' if c.passed else 'FAIL'}]  {c.name}"
                + (f"  —  observed {c.observed} vs required {c.threshold}" if c.observed else "")
                + (f"  ({c.reason})" if c.reason else "")
                for c in paper.setup_checks
            ))
        elif paper.empty_reason:
            self.checks.setText(paper.empty_reason)
        else:
            self.checks.setText("no setup evaluated yet")

        self.components.setText(" | ".join(
            f"{c.name}: {_HEALTH_TEXT[c.health]}" for c in snapshot.components
        ) or "no components reporting")
        self.live_lock.setText("LIVE LOCKED")
        self.next_action.setText(
            f"Blocker: {snapshot.blocker} — Next: {snapshot.next_action}"
            if snapshot.blocker else f"Next: {snapshot.next_action}",
        )


class _ListScreen(Screen):
    """A simple screen rendering captioned lines from the snapshot."""

    def __init__(self, object_name: str, title: str) -> None:
        """Build a compact titled screen with one retained body label."""
        super().__init__()
        self.setObjectName(object_name)
        layout = QVBoxLayout(self)
        heading = QLabel(title)
        heading.setProperty("role", "headline")
        layout.addWidget(heading)
        self.body = _value(f"{object_name}_body", "")
        self.body.setWordWrap(True)
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.body, 1)


class LiveOrderFlowScreen(_ListScreen):
    """Only measurements the recorded feed genuinely supports."""

    def __init__(self) -> None:
        """Build the order-flow screen."""
        super().__init__("screen_live_order_flow", "Live Order Flow")

    def render(self, snapshot: AppSnapshot) -> None:
        """Show real order-flow measures and label heuristics honestly."""
        m = snapshot.market
        lines = [
            f"Bid {_price(m.best_bid)}   Ask {_price(m.best_ask)}   Spread {_price(m.spread)}   Mid {_price(m.mid_price)}",
            f"CVD {m.cumulative_delta}   Aggressive buys {m.buy_volume}   Aggressive sells {m.sell_volume}",
            f"Tape rate {m.event_rate_per_second:,.0f} events/s   {m.provenance_text}",
            "",
            "Feed capabilities (a measurement is never shown as more than it is):",
        ]
        for name, capability, reason in snapshot.capabilities:
            lines.append(f"  • {name}: {capability.value.upper()} — {reason}")
        if not snapshot.capabilities:
            lines.append("  • not yet probed")
        lines += ["", "Setup checks:"]
        if snapshot.paper.setup_checks:
            for check in snapshot.paper.setup_checks:
                mark = "PASS" if check.passed else "FAIL"
                lines.append(f"  [{mark}] {check.name}: observed {check.observed} vs {check.threshold}")
        else:
            lines.append("  no setup evaluated yet")
        self.body.setText("\n".join(lines))


class PaperTradingScreen(_ListScreen):
    """Real delayed-stream / eligible-replay outcomes only."""

    def __init__(self) -> None:
        """Build the paper screen."""
        super().__init__("screen_paper_trading", "Paper Trading")

    def render(self, snapshot: AppSnapshot) -> None:
        """Show the account and an honest empty state when nothing qualified."""
        p = snapshot.paper
        lines = [
            f"Mode: {p.mode}   Profile: {p.profile_name}",
            f"Balance {_money(p.balance)} (start {_money(p.starting_balance)})   "
            f"Target {_money(p.profit_target)} — {p.target_progress:.0%}",
            f"Drawdown room {_money(p.drawdown_room)}   Net P&L {_money(p.net_pnl)}",
            f"Trades {p.trades}   Wins {p.wins}   Losses {p.losses}",
        ]
        if p.empty_reason:
            lines += ["", p.empty_reason]
        self.body.setText("\n".join(lines))


class SessionsReplayScreen(_ListScreen):
    """Eligibility with exact reasons; active recordings are never replayable."""

    def __init__(self) -> None:
        """Build the sessions screen."""
        super().__init__("screen_sessions_replay", "Sessions and Replay")

    def render(self, snapshot: AppSnapshot) -> None:
        """Show capture/session state."""
        c = snapshot.capture
        self.body.setText("\n".join([
            f"Current session: {c.session_id or 'none'}",
            f"Recording: {'yes' if c.recording else 'no'}   "
            f"Session drops: {c.current_session_drops:,}",
            "",
            "An actively recording session is never offered as finalized replay evidence.",
            "A session with any dropped event, overflow, or unclean shutdown stays ineligible.",
        ]))


class ResearchHealthScreen(_ListScreen):
    """Canonical evidence kept visibly separate from experimental candidates."""

    def __init__(self) -> None:
        """Build the research screen."""
        super().__init__("screen_research_health", "Research and Model Health")

    def render(self, snapshot: AppSnapshot) -> None:
        """Show worker state and the raw-vs-unique integrity accounting."""
        r = snapshot.research
        lines = [
            f"State: {r.state.upper()}   Workers {r.active_workers}/{r.requested_workers}",
            f"Jobs — queued {r.queued_jobs}, completed {r.completed_jobs}, failed {r.failed_jobs}",
            "",
            f"CANONICAL trades: {r.canonical_trades}",
            f"EXPERIMENTAL candidate trades: {r.experimental_trades} (never merged into canonical)",
            f"Unique underlying setups: {r.unique_setups}   duplicate overlap: {r.duplicate_overlap}",
            f"Independent trading days: {r.independent_days}",
        ]
        if r.throttle_reason:
            lines += ["", r.throttle_reason]
        if r.gpu_note:
            lines += ["", r.gpu_note]
        self.body.setText("\n".join(lines))


class RiskLucidScreen(_ListScreen):
    """The selected profile and every rejection rule."""

    def __init__(self) -> None:
        """Build the risk screen."""
        super().__init__("screen_risk_lucid", "Risk and Lucid Account")

    def render(self, snapshot: AppSnapshot) -> None:
        """Show account progress and rule state."""
        p, e = snapshot.paper, snapshot.execution
        self.body.setText("\n".join([
            f"Profile: {p.profile_name}",
            f"Balance {_money(p.balance)}   Target {_money(p.profit_target)} ({p.target_progress:.0%})",
            f"Drawdown room {_money(p.drawdown_room)}   Risk remaining {_money(p.risk_remaining)}",
            f"Consistency: {'—' if p.consistency_share is None else f'{p.consistency_share:.0%}'}",
            "",
            f"Prop rules verified: {'yes' if e.prop_rules_resolved else 'NO — broker arming blocked'}",
        ]))


class ExecutionScreen(_ListScreen):
    """PAPER / DEMO / locked LIVE, visually separated."""

    def __init__(self) -> None:
        """Build the execution screen."""
        super().__init__("screen_execution", "Execution")

    def render(self, snapshot: AppSnapshot) -> None:
        """Show environment and the exact unmet LIVE gates."""
        e = snapshot.execution
        lines = [
            f"Environment: {e.environment}",
            f"Connected: {'yes' if e.connected else 'no'}   DEMO armed: {'YES' if e.demo_armed else 'no'}",
            "",
            "LIVE: LOCKED. Unmet requirements:",
        ]
        lines += [f"  • {b}" for b in e.live_blockers] or ["  • (gate not evaluated)"]
        lines += ["", "Delayed data can never place a broker order."]
        self.body.setText("\n".join(lines))


class DiagnosticsScreen(_ListScreen):
    """Copyable diagnostics; no colour-only meaning."""

    def __init__(self) -> None:
        """Build the diagnostics screen."""
        super().__init__("screen_diagnostics", "Diagnostics and Settings")

    def render(self, snapshot: AppSnapshot) -> None:
        """Show queue/latency metrics and component transitions."""
        c = snapshot.capture
        self.body.setText("\n".join([
            f"Lifecycle: {snapshot.lifecycle_state}",
            f"Intake queue: {c.intake_occupancy}/{c.intake_capacity}",
            f"Recorder queue: {c.recorder_occupancy}/{c.recorder_capacity}",
            f"Recorder flush latency: {c.flush_latency_ms:.2f} ms",
            f"Persisted/s: {c.persisted_per_second:,.0f}",
            "",
            "Components:",
            *[f"  • {comp.name}: {_HEALTH_TEXT[comp.health]} — {comp.detail}" for comp in snapshot.components],
        ]))


def build_screens() -> dict[str, Screen]:
    """Construct every screen keyed by its sidebar destination."""
    return {
        OVERVIEW: OverviewScreen(),
        LIVE_ORDER_FLOW: LiveOrderFlowScreen(),
        PAPER_TRADING: PaperTradingScreen(),
        SESSIONS_REPLAY: SessionsReplayScreen(),
        RESEARCH_HEALTH: ResearchHealthScreen(),
        RISK_LUCID: RiskLucidScreen(),
        EXECUTION: ExecutionScreen(),
        DIAGNOSTICS: DiagnosticsScreen(),
    }
