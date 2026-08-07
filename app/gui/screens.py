"""Eight snapshot-only screens for the trustworthy operations dashboard."""

from __future__ import annotations

from decimal import Decimal

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.gui.autonomous_view import (
    DEFAULT_REPORTS_ROOT,
    DEFAULT_STORE_ROOT,
    AutonomousSnapshot,
    read_autonomous_snapshot,
)
from app.gui.charts import AggressorBar, ChartPanel, HistoryChart
from app.gui.view_models import AppSnapshot, Health
from app.gui.widgets import (
    Card,
    EvidenceTable,
    MetricMeter,
    StatTile,
    StatusBadge,
    stat_grid,
)

OVERVIEW = "Overview"
LIVE_ORDER_FLOW = "Live Order Flow"
PAPER_TRADING = "Paper Trading"
SESSIONS_REPLAY = "Sessions and Replay"
RESEARCH_HEALTH = "Research and Model Health"
RISK_LUCID = "Risk and Lucid Account"
EXECUTION = "Execution"
DIAGNOSTICS = "Diagnostics and Settings"
AUTONOMOUS = "Autonomous Intelligence"
REPORTS = "Reports"
SCREEN_ORDER = (
    OVERVIEW, LIVE_ORDER_FLOW, PAPER_TRADING, SESSIONS_REPLAY,
    RESEARCH_HEALTH, RISK_LUCID, EXECUTION, DIAGNOSTICS,
    AUTONOMOUS, REPORTS,
)

_HEALTH_TEXT = {
    Health.OK: "OK", Health.WARN: "WARNING", Health.FAIL: "FAILED",
    Health.IDLE: "IDLE", Health.LOCKED: "LOCKED",
    Health.WARMING_UP: "WARMING UP", Health.DEGRADED: "DEGRADED",
    Health.THROTTLED: "THROTTLED", Health.INVALIDATED: "INVALIDATED",
    Health.PAUSED: "PAUSED", Health.STOPPING: "STOPPING",
    Health.UNAVAILABLE: "UNAVAILABLE",
}


def _value(object_name: str, initial: str = "—") -> QLabel:
    label = QLabel(initial)
    label.setObjectName(object_name)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _money(amount: Decimal | None) -> str:
    return "—" if amount is None else f"${amount:,.2f}"


def _price(amount: Decimal | None) -> str:
    return "—" if amount is None else f"{amount:,.2f}"


def _heading(title: str, subtitle: str) -> tuple[QLabel, QLabel]:
    from app.gui import design_tokens as dt
    
    heading = QLabel(title)
    heading.setProperty("role", "headline")
    heading.setStyleSheet(
        f"""
        font-size: {dt.FONT_SIZE_XL}px;
        font-weight: {dt.FONT_WEIGHT_BOLD};
        color: {dt.COLOR_TEXT_PRIMARY};
        """
    )
    detail = QLabel(subtitle)
    detail.setProperty("role", "muted")
    detail.setWordWrap(True)
    detail.setStyleSheet(
        f"""
        color: {dt.COLOR_TEXT_SECONDARY};
        font-size: {dt.FONT_SIZE_SM}px;
        """
    )
    return heading, detail


def _state(value: bool, yes: str, no: str) -> tuple[str, str]:
    return (yes, "ok") if value else (no, "neutral")


class Screen(QWidget):
    """Base screen with retained widgets and an immutable render contract."""

    def render(self, snapshot: AppSnapshot) -> None:
        """Update retained widgets from an immutable snapshot."""
        raise NotImplementedError


class DashboardScreen(Screen):
    """Scrollable screen shell with a compatibility text summary for diagnostics."""

    def __init__(self, object_name: str, title: str, subtitle: str) -> None:
        super().__init__()
        self.setObjectName(object_name)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        heading, detail = _heading(title, subtitle)
        outer.addWidget(heading)
        outer.addWidget(detail)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 8, 2, 8)
        self.content_layout.setSpacing(10)
        self.scroll.setWidget(self.content)
        outer.addWidget(self.scroll, 1)
        self.body = _value(f"{object_name}_body", "")
        self.body.setParent(self)
        self.body.hide()

    def set_summary(self, lines: list[str]) -> None:
        """Keep a selectable plain-text equivalent for tests and diagnostics."""
        self.body.setText("\n".join(lines))
        self.setAccessibleDescription(self.body.text())


class OverviewScreen(Screen):
    """High-priority operating truth, composed for a 1366x768 viewport."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("screen_overview")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)
        header = QHBoxLayout()
        self.state_label = _value("overview_state", "Starting…")
        self.state_label.setProperty("role", "headline")
        self.provenance = StatusBadge("PROVENANCE UNKNOWN", "overview_provenance", "neutral")
        self.live_lock = StatusBadge("LIVE LOCKED", "overview_live_locked", "locked")
        header.addWidget(self.state_label)
        header.addStretch(1)
        # Provenance and LIVE safety are persistent in the window trust strip.
        # Retain these labels for compatibility without duplicating the badges.
        self.provenance.hide()
        self.live_lock.hide()
        layout.addLayout(header)

        hero = QHBoxLayout()
        market = Card("Market now", "overview_market_card")
        market_stats = QHBoxLayout()
        self.contract_tile = StatTile("Contract", "overview_contract_tile")
        self.last_tile = StatTile("Last", "overview_last_tile")
        self.spread_tile = StatTile("Spread", "overview_spread_tile")
        for tile in (self.contract_tile, self.last_tile, self.spread_tile):
            market_stats.addWidget(tile)
        market.body.addLayout(market_stats)
        self.price_chart = ChartPanel(HistoryChart("Observed midpoint", "overview_price_chart", "price"))
        market.body.addWidget(self.price_chart)
        hero.addWidget(market, 3)

        integrity = Card("Capture integrity", "overview_capture_card")
        self.receiver_badge = StatusBadge("NOT LISTENING", "overview_receiver_badge")
        self.bookmap_badge = StatusBadge("WAITING", "overview_bookmap_badge")
        badge_row = QHBoxLayout()
        badge_row.addWidget(self.receiver_badge)
        badge_row.addWidget(self.bookmap_badge)
        integrity.body.addLayout(badge_row)
        self.queue_meter = MetricMeter("Queue pressure", "overview_queue_meter")
        self.drop_meter = MetricMeter("Current-session loss", "overview_drop_meter")
        integrity.body.addWidget(self.queue_meter)
        integrity.body.addWidget(self.drop_meter)
        self.capture_detail = _value("overview_capture_detail")
        self.capture_detail.setWordWrap(True)
        integrity.body.addWidget(self.capture_detail)
        hero.addWidget(integrity, 2)
        layout.addLayout(hero, 3)

        lower = QHBoxLayout()
        decision = Card("Current setup decision", "overview_decision_card")
        self.setup_name = _value("overview_setup")
        self.setup_name.setProperty("role", "metric")
        self.decision = StatusBadge("AWAITING", "overview_decision", "neutral")
        setup_row = QHBoxLayout()
        setup_row.addWidget(self.setup_name)
        setup_row.addStretch(1)
        setup_row.addWidget(self.decision)
        decision.body.addLayout(setup_row)
        self.checks = _value("overview_setup_checks", "no setup evaluated yet")
        self.checks.setWordWrap(True)
        self.checks.setAlignment(Qt.AlignmentFlag.AlignTop)
        decision.body.addWidget(self.checks, 1)
        lower.addWidget(decision, 3)

        account = Card("Delayed paper account", "overview_account_card")
        self.balance_tile = StatTile("Balance", "overview_balance_tile")
        self.risk_tile = StatTile("Risk remaining", "overview_risk_tile")
        account.body.addLayout(stat_grid((self.balance_tile, self.risk_tile), 2))
        self.target_meter = MetricMeter("Profit target", "overview_target_meter")
        self.target_meter.bar.setObjectName("overview_target_progress")
        self.drawdown_meter = MetricMeter("Drawdown room", "overview_drawdown_meter")
        account.body.addWidget(self.target_meter)
        account.body.addWidget(self.drawdown_meter)
        lower.addWidget(account, 2)
        layout.addLayout(lower, 2)

        self.components = _value("overview_components")
        self.components.setWordWrap(True)
        self.next_action = _value("overview_next_action")
        self.next_action.setWordWrap(True)
        footer = Card("System truth and next action", "overview_footer_card")
        footer.body.addWidget(self.components)
        footer.body.addWidget(self.next_action)
        layout.addWidget(footer)

        # Compatibility fields retained for existing integrations and tests.
        def legacy_label(name: str) -> QLabel:
            label = _value(name)
            label.setParent(self)
            label.hide()
            return label

        self.contract = legacy_label("overview_contract")
        self.last_price = legacy_label("overview_last_price")
        self.bid = legacy_label("overview_bid")
        self.ask = legacy_label("overview_ask")
        self.spread = legacy_label("overview_spread")
        self.mid = legacy_label("overview_mid")
        self.receiver = legacy_label("overview_receiver")
        self.bookmap = legacy_label("overview_bookmap")
        self.recording = legacy_label("overview_recording")
        self.event_rate = legacy_label("overview_event_rate")
        self.drops = legacy_label("overview_drops")
        self.queue = legacy_label("overview_queue")
        self.processing_age = legacy_label("overview_processing_age")
        self.position = legacy_label("overview_position")
        self.risk_remaining = legacy_label("overview_risk_remaining")
        self.profile = legacy_label("overview_profile")
        self.balance = legacy_label("overview_balance")
        self.drawdown_room = legacy_label("overview_drawdown_room")
        self.consistency = legacy_label("overview_consistency")
        self.target_bar = self.target_meter.bar

    def render(self, snapshot: AppSnapshot) -> None:
        market, capture, paper = snapshot.market, snapshot.capture, snapshot.paper
        self.state_label.setText(snapshot.plain_state)
        self.provenance.set_status(market.provenance_text, "warn" if market.is_delayed else "ok")
        self.contract_tile.set_value(f"{market.symbol} {market.contract}")
        self.last_tile.set_value(_price(market.last_price), f"Bid {_price(market.best_bid)} / Ask {_price(market.best_ask)}")
        self.spread_tile.set_value(_price(market.spread), f"Mid {_price(market.mid_price)}")
        self.price_chart.set_history(market.history)
        receiver_text, receiver_state = _state(capture.receiver_listening, "RECEIVER LISTENING", "RECEIVER OFFLINE")
        bookmap_text, bookmap_state = _state(capture.bookmap_connected, "BOOKMAP CONNECTED", "WAITING FOR BOOKMAP")
        self.receiver_badge.set_status(receiver_text, receiver_state)
        self.bookmap_badge.set_status(bookmap_text, bookmap_state)
        self.queue_meter.set_fraction(capture.queue_pressure, f"{capture.queue_pressure:.0%} · {_HEALTH_TEXT[capture.health]}")
        self.drop_meter.set_fraction(1.0 if capture.current_session_drops else 0.0, f"{capture.current_session_drops:,} this session")
        age = "—" if market.processing_age_ms is None else f"{market.processing_age_ms} ms"
        self.capture_detail.setText(f"{market.event_rate_per_second:,.0f} events/s · processing age {age} (source delay excluded)")
        self.setup_name.setText(paper.setup_name)
        failed = [check for check in paper.setup_checks if not check.passed]
        decision_text = "ACCEPTED" if paper.setup_checks and not failed else (f"REJECTED · {failed[0].name}" if failed else "AWAITING EVALUATION")
        self.decision.set_status(decision_text, "ok" if decision_text == "ACCEPTED" else ("fail" if failed else "neutral"))
        if paper.setup_checks:
            checks_text = "\n".join(
                f"[{'PASS' if check.passed else 'FAIL'}]  {check.name}"
                + (f"  —  observed {check.observed} vs required {check.threshold}" if check.observed else "")
                + (f"  ({check.reason})" if check.reason else "")
                for check in paper.setup_checks
            )
        else:
            checks_text = paper.empty_reason or "no setup evaluated yet"
        self.checks.setText(checks_text)
        self.balance_tile.set_value(_money(paper.balance), paper.profile_name or "profile unavailable")
        self.risk_tile.set_value(_money(paper.risk_remaining), paper.open_position)
        self.target_meter.set_fraction(float(paper.target_progress), f"{paper.target_progress:.0%} · target {_money(paper.profit_target)}")
        drawdown_fraction = float(paper.drawdown_room / paper.starting_balance) if paper.starting_balance else 0.0
        self.drawdown_meter.set_fraction(drawdown_fraction, _money(paper.drawdown_room))
        self.components.setText(" · ".join(f"{component.name.upper()} {_HEALTH_TEXT[component.health]}" for component in snapshot.components) or "No components reporting")
        self.next_action.setText((f"Blocker: {snapshot.blocker}  ·  " if snapshot.blocker else "") + f"Next: {snapshot.next_action}")

        self.contract.setText(f"{market.symbol} {market.contract}"); self.last_price.setText(_price(market.last_price))
        self.bid.setText(_price(market.best_bid)); self.ask.setText(_price(market.best_ask)); self.spread.setText(_price(market.spread)); self.mid.setText(_price(market.mid_price))
        self.receiver.setText("listening" if capture.receiver_listening else "not listening"); self.bookmap.setText("connected" if capture.bookmap_connected else "waiting")
        self.recording.setText("yes" if capture.recording else "no"); self.event_rate.setText(f"{market.event_rate_per_second:,.0f}")
        self.drops.setText(f"{capture.current_session_drops:,} this session" + (f" (lifetime {capture.lifetime_bridge_drops:,})" if capture.lifetime_bridge_drops != capture.current_session_drops else ""))
        self.queue.setText(f"{capture.queue_pressure:.0%} [{_HEALTH_TEXT[capture.health]}]")
        self.processing_age.setText("—" if market.processing_age_ms is None else f"{market.processing_age_ms} ms (excludes source delay)")
        self.position.setText(paper.open_position); self.risk_remaining.setText(_money(paper.risk_remaining)); self.profile.setText(paper.profile_name or "—")
        self.balance.setText(_money(paper.balance)); self.drawdown_room.setText(_money(paper.drawdown_room)); self.consistency.setText("—" if paper.consistency_share is None else f"{paper.consistency_share:.0%}")
        self.target_bar.setValue(int(paper.target_progress * 100))


class LiveOrderFlowScreen(DashboardScreen):
    """Observed order-flow context with explicit capability boundaries."""

    def __init__(self) -> None:
        super().__init__("screen_live_order_flow", "Live Order Flow", "What the current feed actually delivered — no native Bookmap pixels or MBO claims.")
        stats = Card("Current observed market", "flow_market_card")
        self.bid = StatTile("Best bid", "flow_bid"); self.ask = StatTile("Best ask", "flow_ask"); self.spread = StatTile("Spread", "flow_spread"); self.rate = StatTile("Tape rate", "flow_rate")
        stats.body.addLayout(stat_grid((self.bid, self.ask, self.spread, self.rate), 4))
        self.content_layout.addWidget(stats)
        charts = QHBoxLayout()
        price_card = Card("Observed midpoint history", "flow_price_card"); self.price_chart = ChartPanel(HistoryChart("Midpoint", "flow_price_chart", "price")); price_card.body.addWidget(self.price_chart)
        cvd_card = Card("Cumulative delta history", "flow_cvd_card"); self.cvd_chart = ChartPanel(HistoryChart("CVD", "flow_cvd_chart", "cumulative_delta")); cvd_card.body.addWidget(self.cvd_chart)
        charts.addWidget(price_card); charts.addWidget(cvd_card); self.content_layout.addLayout(charts)
        aggression = Card("Aggressive volume", "flow_aggression_card"); self.aggressor = AggressorBar("flow_aggressor_bar"); aggression.body.addWidget(self.aggressor); self.content_layout.addWidget(aggression)
        capabilities = Card("Feed capability matrix", "flow_capability_card"); self.capability_table = EvidenceTable(("Measurement", "State", "Why"), "flow_capability_table"); capabilities.body.addWidget(self.capability_table); self.content_layout.addWidget(capabilities)
        checks = Card("Current setup evidence", "flow_checks_card"); self.check_table = EvidenceTable(("State", "Condition", "Observed / required", "Reason"), "flow_check_table"); checks.body.addWidget(self.check_table); self.content_layout.addWidget(checks)

    def render(self, snapshot: AppSnapshot) -> None:
        market = snapshot.market
        self.bid.set_value(_price(market.best_bid)); self.ask.set_value(_price(market.best_ask)); self.spread.set_value(_price(market.spread)); self.rate.set_value(f"{market.event_rate_per_second:,.0f}/s", market.provenance_text)
        self.price_chart.set_history(market.history); self.cvd_chart.set_history(market.history); self.aggressor.set_values(market.buy_volume, market.sell_volume)
        self.capability_table.set_rows(tuple((name, capability.value.upper(), reason) for name, capability, reason in snapshot.capabilities))
        self.check_table.set_rows(tuple(("PASS" if check.passed else "FAIL", check.name, f"{check.observed} / {check.threshold}", check.reason) for check in snapshot.paper.setup_checks))
        lines = [f"Bid {_price(market.best_bid)}   Ask {_price(market.best_ask)}   Spread {_price(market.spread)}   Mid {_price(market.mid_price)}", f"CVD {market.cumulative_delta if market.cumulative_delta is not None else '—'}   Aggressive buys {market.buy_volume if market.buy_volume is not None else '—'}   Aggressive sells {market.sell_volume if market.sell_volume is not None else '—'}", f"Tape rate {market.event_rate_per_second:,.0f} events/s   {market.provenance_text}", "", "Feed capabilities (a measurement is never shown as more than it is):"]
        lines += [f"  • {name}: {capability.value.upper()} — {reason}" for name, capability, reason in snapshot.capabilities] or ["  • not yet probed"]
        lines += ["", "Setup checks:"] + ([f"  [{'PASS' if check.passed else 'FAIL'}] {check.name}: {check.reason or f'observed {check.observed} vs {check.threshold}'}" for check in snapshot.paper.setup_checks] or ["  no setup evaluated yet"])
        self.set_summary(lines)


class PaperTradingScreen(DashboardScreen):
    """Causal delayed-paper account, position, evidence, and closed trades."""

    def __init__(self) -> None:
        super().__init__("screen_paper_trading", "Paper Trading", "Simulation only. Every position and outcome comes from the causal delayed-paper engine.")
        account = Card("Account and evaluation", "paper_account_card")
        self.balance = StatTile("Balance", "paper_balance"); self.pnl = StatTile("Net P&L", "paper_pnl"); self.trades = StatTile("Trades", "paper_trades"); self.evaluations = StatTile("Evaluations", "paper_evaluations")
        account.body.addLayout(stat_grid((self.balance, self.pnl, self.trades, self.evaluations), 4)); self.target = MetricMeter("Profit target", "paper_target"); account.body.addWidget(self.target); self.content_layout.addWidget(account)
        position = Card("Simulated position and bracket", "paper_position_card"); self.position_text = _value("paper_position_text"); self.position_text.setWordWrap(True); position.body.addWidget(self.position_text); self.content_layout.addWidget(position)
        funnel = Card("Evaluation funnel and causal integrity", "paper_funnel_card"); self.funnel_text = _value("paper_funnel_text"); self.funnel_text.setWordWrap(True); funnel.body.addWidget(self.funnel_text); self.content_layout.addWidget(funnel)
        evidence = Card("Condition evidence", "paper_evidence_card"); self.evidence = EvidenceTable(("Condition", "Pass", "Fail", "Latest evidence"), "paper_evidence_table"); evidence.body.addWidget(self.evidence); self.content_layout.addWidget(evidence)
        recent = Card("Closed simulated trades", "paper_trades_card"); self.trade_table = EvidenceTable(("Time (UTC)", "Direction", "Qty", "Entry", "Exit", "Net P&L", "Reason"), "paper_trade_table"); recent.body.addWidget(self.trade_table); self.content_layout.addWidget(recent)

    def render(self, snapshot: AppSnapshot) -> None:
        paper = snapshot.paper
        self.balance.set_value(_money(paper.balance), f"Start {_money(paper.starting_balance)}"); self.pnl.set_value(_money(paper.net_pnl)); self.trades.set_value(str(paper.trades), f"{paper.wins} wins / {paper.losses} losses"); self.evaluations.set_value(str(paper.evaluations), f"{paper.candidates} candidates")
        self.target.set_fraction(float(paper.target_progress), f"{paper.target_progress:.0%} · {_money(paper.profit_target)}")
        if paper.flat:
            position_lines = [f"flat · pending order: {paper.pending_order}"]
        else:
            position_lines = [f"{paper.open_position} @ {paper.position_entry}", f"Stop {paper.position_stop}   Target {paper.position_target}   Unrealized {_money(paper.unrealized_pnl)}"]
        self.position_text.setText("\n".join(position_lines))
        blocked_by = "   ".join(f"{name}: {count:,}" for name, count in paper.risk_rejections[:3])
        self.funnel_text.setText(f"{paper.evaluations} evaluated  →  {paper.candidates} qualified  →  {paper.risk_rejected} blocked by risk  →  {paper.trades} closed" + (f"\nBlocked by:  {blocked_by}" if blocked_by else "") + f"\nAnalysis window {paper.window_span_seconds:.0f}s · causality gaps {paper.causality_breaks} · analysis skips {paper.analysis_events_skipped:,}\n{paper.empty_reason}")
        self.evidence.set_rows(tuple((name, f"{passes:,}", f"{failures:,}", evidence) for name, passes, failures, evidence in paper.condition_stats))
        self.trade_table.set_rows(tuple((row.opened_at, row.direction + (" [FIXTURE]" if row.is_synthetic_fixture else ""), str(row.contracts), row.entry, row.exit, row.net_pnl, row.close_reason) for row in paper.recent_trades))
        lines = [f"Mode: {paper.mode}   Profile: {paper.profile_name}", f"Balance {_money(paper.balance)} (start {_money(paper.starting_balance)})   Target {_money(paper.profit_target)} — {paper.target_progress:.0%}", f"Drawdown room {_money(paper.drawdown_room)}   Net P&L {_money(paper.net_pnl)}", f"Trades {paper.trades}   Wins {paper.wins}   Losses {paper.losses}", "", "Position:", *[f"  {line}" for line in position_lines], "", f"Analysis window: {paper.window_span_seconds:.0f}s of market time"]
        if paper.causality_breaks: lines.append(f"CAUSALITY: {paper.causality_breaks} gap(s), {paper.analysis_events_skipped:,} event(s) skipped for analysis - entries blocked until re-warmed")
        if paper.condition_stats: lines += ["", "Condition evidence (worst failures first):", *[f"  {name}: {passes:,} pass / {failures:,} fail — {evidence}" for name, passes, failures, evidence in paper.condition_stats[:8]]]
        lines += ["", f"Candidates {paper.candidates}   Blocked by risk {paper.risk_rejected}", *[f"  • {name}: {count}" for name, count in paper.risk_rejections[:3]], "", "Closed trades (newest first):", *([f"  {row.label}" for row in paper.recent_trades[:10]] or ["  none"])]
        if paper.malformed_events: lines += ["", f"WARNING: {paper.malformed_events} market event(s) could not be parsed and were dropped. Downstream numbers are incomplete."]
        if paper.empty_reason: lines += ["", paper.empty_reason]
        self.set_summary(lines)


class SessionsReplayScreen(DashboardScreen):
    """Truthful current-session state and explicit replay integration boundary."""

    def __init__(self) -> None:
        super().__init__("screen_sessions_replay", "Sessions and Replay", "Recorded evidence is immutable. Active or damaged sessions are never offered as replay truth.")
        current = Card("Current capture session", "sessions_current_card"); self.session = StatTile("Session ID", "sessions_id"); self.recording = StatusBadge("NOT RECORDING", "sessions_recording"); self.drops = StatTile("Session drops", "sessions_drops"); row = QHBoxLayout(); row.addWidget(self.session, 2); row.addWidget(self.drops); row.addWidget(self.recording); current.body.addLayout(row); self.content_layout.addWidget(current)
        catalog = Card("Recorded session catalog", "sessions_catalog_card"); self.catalog_summary = _value("sessions_catalog_summary"); self.catalog_summary.setWordWrap(True); catalog.body.addWidget(self.catalog_summary); self.catalog_table = EvidenceTable(("Session", "Started (UTC)", "Provenance", "Status", "Depth", "Market trades", "Paper trades"), "sessions_catalog_table"); catalog.body.addWidget(self.catalog_table); self.content_layout.addWidget(catalog)
        rules = Card("Eligibility rules", "sessions_rules_card"); rules_text = QLabel("Finalized + clean shutdown + zero sequence loss + supported provenance.\nAny overflow, dropped event, incompatible schema, or active writer keeps a session ineligible."); rules_text.setWordWrap(True); rules.body.addWidget(rules_text); self.content_layout.addWidget(rules)

    def render(self, snapshot: AppSnapshot) -> None:
        capture = snapshot.capture
        self.session.set_value(capture.session_id or "none"); self.drops.set_value(f"{capture.current_session_drops:,}", "current segment"); self.recording.set_status("RECORDING" if capture.recording else "NOT RECORDING", "ok" if capture.recording else "neutral")
        self.catalog_summary.setText(f"{snapshot.sessions_total:,} recorded session(s); showing newest {len(snapshot.sessions)} (newest first)." if snapshot.sessions_total else "No recorded sessions found yet.")
        self.catalog_table.set_rows(tuple((s.session_id, s.started_at, s.provenance, s.status, f"{s.depth_updates:,}", f"{s.market_trades:,}", str(s.paper_trades)) for s in snapshot.sessions))
        self.set_summary([f"Current session: {capture.session_id or 'none'}", f"Recording: {'yes' if capture.recording else 'no'}   Session drops: {capture.current_session_drops:,}", "", f"Recorded catalog: {snapshot.sessions_total:,} session(s).", *[f"  {s.session_id}  {s.started_at}  {s.status}  · {s.paper_trades} paper trades" for s in snapshot.sessions[:10]], "", "An actively recording session is never offered as finalized replay evidence."])


class ResearchHealthScreen(DashboardScreen):
    """Learning and Evidence Center with no runtime model claims."""

    def __init__(self) -> None:
        super().__init__("screen_research_health", "Learning and Evidence Center", "Offline challenger evidence, feature observation, and workflow gates. No model affects decisions.")
        pipeline = Card("Operating and learning evidence pipeline", "research_pipeline_card"); self.pipeline_table = EvidenceTable(("Stage", "State", "Evidence", "Next action"), "research_pipeline_table"); pipeline.body.addWidget(self.pipeline_table); self.content_layout.addWidget(pipeline)
        evidence = Card("Canonical research evidence", "research_evidence_card"); self.canonical = StatTile("Canonical trades", "research_canonical"); self.experimental = StatTile("Experimental trades", "research_experimental"); self.days = StatTile("Independent days", "research_days"); self.workers = StatTile("Workers", "research_workers"); evidence.body.addLayout(stat_grid((self.canonical, self.experimental, self.days, self.workers), 4)); self.content_layout.addWidget(evidence)
        model = Card("Offline challenger · NO RUNTIME EFFECT", "research_model_card"); self.model_table = EvidenceTable(("Field", "Verified state", "Meaning"), "research_model_table"); model.body.addWidget(self.model_table); self.content_layout.addWidget(model)
        registry = Card("All registered challengers · NO RUNTIME EFFECT", "research_registry_card"); self.registry_table = EvidenceTable(("Artifact", "Model", "Validation", "Brier", "OOS preds", "Beats baseline", "Approval"), "research_registry_table"); registry.body.addWidget(self.registry_table); self.content_layout.addWidget(registry)
        profitability = Card("Profitability evidence ladder — not a forecast", "research_profitability_card"); self.profitability_summary = _value("research_profitability_summary"); self.profitability_summary.setWordWrap(True); self.profitability_table = EvidenceTable(("Gate", "State", "Observed", "Required"), "research_profitability_table"); profitability.body.addWidget(self.profitability_summary); profitability.body.addWidget(self.profitability_table); self.content_layout.addWidget(profitability)

    def render(self, snapshot: AppSnapshot) -> None:
        research, model, profitability = snapshot.research, snapshot.model, snapshot.profitability
        self.pipeline_table.set_rows(tuple((stage.label, stage.status.upper(), stage.detail or stage.blocker, stage.next_action) for stage in snapshot.pipeline.stages))
        self.canonical.set_value(str(research.canonical_trades), "source of truth"); self.experimental.set_value(str(research.experimental_trades), "never merged into canonical"); self.days.set_value(str(research.independent_days), f"{research.unique_setups} unique setups"); self.workers.set_value(f"{research.active_workers}/{research.requested_workers}", research.throttle_reason or research.state)
        self.model_table.set_rows((
            ("Registry", model.registry_state, model.artifact_id or "no challenger registered"),
            ("Validation", model.validation_state, model.validation_detail),
            ("Approval", model.approval_state, model.approval_detail),
            ("Feature parity", model.feature_parity_state, "shared offline/online contract"),
            ("Feature observer", model.feature_observation_state, model.feature_observation_reason),
            ("Runtime model", model.shadow_loader_state, model.shadow_loader_reason),
            ("Outcome journal", model.outcome_tracker_state, f"resolved {model.outcome_resolved} (target {model.outcome_resolved_target}/stop {model.outcome_resolved_stop}/timeout {model.outcome_resolved_timeout}), pending {model.outcome_pending}, dropped {model.outcome_dropped_unresolved + model.outcome_dropped_overflow}"),
            ("Outcome coverage", f"{model.outcome_coverage_fraction:.0%}", f"{model.outcome_predictions_registered} predictions registered; abstention {model.abstention_rate:.0%}; gap-tainted {model.outcome_gap_tainted_resolutions}"),
            ("Decision impact", model.decision_impact, "No effect on strategy, paper, risk, or execution"),
        ))
        self.registry_table.set_rows(tuple(
            (
                challenger.artifact_id,
                f"{challenger.model_type} {challenger.model_version}".strip(),
                challenger.validation_state,
                f"{challenger.brier_score:.4f}",
                str(challenger.oos_predictions),
                "yes" if challenger.beats_baseline else "no",
                f"{challenger.approval_state} — {challenger.approval_detail}",
            )
            for challenger in snapshot.challengers
        ) or (("no challenger registered", "—", "—", "—", "—", "—", "—"),))
        self.profitability_summary.setText(profitability.summary + (f"\n{profitability.headline}" if profitability.headline else "")); self.profitability_table.set_rows(tuple((gate.label, gate.status.upper(), gate.observed, gate.threshold) for gate in profitability.gates))
        lines = [f"State: {research.state.upper()}   Workers {research.active_workers}/{research.requested_workers}", f"Jobs — queued {research.queued_jobs}, completed {research.completed_jobs}, failed {research.failed_jobs}", "", f"CANONICAL trades: {research.canonical_trades}", f"EXPERIMENTAL candidate trades: {research.experimental_trades} (never merged into canonical)", f"Unique underlying setups: {research.unique_setups}   duplicate overlap: {research.duplicate_overlap}", f"Independent trading days: {research.independent_days}"]
        if research.throttle_reason: lines += ["", research.throttle_reason]
        if research.gpu_note: lines += ["", research.gpu_note]
        lines += ["", "OFFLINE MODEL EVIDENCE (NO RUNTIME EFFECT)", f"Registry: {model.registry_state}   Validation: {model.validation_state}", f"Challenger: {model.artifact_id or 'none'}   Dataset: {model.dataset_id or 'none'}", f"Model: {model.model_type or 'none'} {model.model_version}", f"Eligible sessions: {model.eligible_sessions}   Excluded: {model.excluded_sessions}", f"OOS predictions: {model.oos_predictions}   Brier: {model.brier_score:.4f}   Beats baseline: {model.beats_baseline}", f"Approval: {model.approval_state} — {model.approval_detail}", f"Feature parity: {model.feature_parity_state}", f"Feature observer: {model.feature_observation_state} — {model.feature_observation_reason}", f"Feature observations: {model.feature_observations}   Gap resets: {model.feature_gap_resets}   Session resets: {model.feature_session_resets}   Skipped: {model.feature_skipped_events}", f"Runtime model: {model.shadow_loader_state} — {model.shadow_loader_reason}", f"Runtime loaded: {model.runtime_loaded}   Shadow predictions: {model.shadow_predictions}", f"Outcome journal: {model.outcome_tracker_state}   Registered {model.outcome_predictions_registered}   Resolved {model.outcome_resolved} (target {model.outcome_resolved_target}/stop {model.outcome_resolved_stop}/timeout {model.outcome_resolved_timeout})   Pending {model.outcome_pending}   Dropped unresolved {model.outcome_dropped_unresolved}   Dropped overflow {model.outcome_dropped_overflow}   Gap-tainted {model.outcome_gap_tainted_resolutions}", f"Outcome coverage: {model.outcome_coverage_fraction:.0%}   Abstention rate: {model.abstention_rate:.0%}", f"Decision impact: {model.decision_impact} — no effect on strategy, paper, risk, or execution", "", f"ALL REGISTERED CHALLENGERS ({len(snapshot.challengers)}) — NO RUNTIME EFFECT"]
        if snapshot.challengers:
            lines += [f"  {c.artifact_id}: {c.model_type} {c.model_version}  validation={c.validation_state}  brier={c.brier_score:.4f}  beats_baseline={c.beats_baseline}  approval={c.approval_state}" for c in snapshot.challengers]
        else:
            lines += ["  no challenger registered"]
        lines += ["", profitability.summary]
        if profitability.headline: lines += ["", profitability.headline]
        if profitability.gates: lines += ["", "Evidence gates:", *[f"  [{gate.status.upper():>21}] {gate.label}\n                          observed {gate.observed} vs {gate.threshold}" for gate in profitability.gates]]
        self.set_summary(lines)


class RiskLucidScreen(DashboardScreen):
    """Read-only account profile, target, drawdown, and safety truth."""

    def __init__(self) -> None:
        super().__init__("screen_risk_lucid", "Risk and Lucid Account", "The strategy can request an action; it cannot bypass these independent constraints.")
        account = Card("Selected account profile", "risk_account_card"); self.profile = StatTile("Profile", "risk_profile"); self.balance = StatTile("Balance", "risk_balance"); self.room = StatTile("Drawdown room", "risk_room"); self.remaining = StatTile("Risk remaining", "risk_remaining"); account.body.addLayout(stat_grid((self.profile, self.balance, self.room, self.remaining), 4)); self.target = MetricMeter("Profit target", "risk_target"); account.body.addWidget(self.target); self.content_layout.addWidget(account)
        gate = Card("Broker and prop-rule gate", "risk_gate_card"); self.gate = StatusBadge("UNRESOLVED", "risk_gate_badge", "locked"); self.gate_reason = _value("risk_gate_reason"); self.gate_reason.setWordWrap(True); gate.body.addWidget(self.gate); gate.body.addWidget(self.gate_reason); self.content_layout.addWidget(gate)

    def render(self, snapshot: AppSnapshot) -> None:
        paper, execution = snapshot.paper, snapshot.execution
        self.profile.set_value(paper.profile_name or "—"); self.balance.set_value(_money(paper.balance)); self.room.set_value(_money(paper.drawdown_room)); self.remaining.set_value(_money(paper.risk_remaining)); self.target.set_fraction(float(paper.target_progress), f"{paper.target_progress:.0%} · {_money(paper.profit_target)}")
        resolved = execution.prop_rules_resolved; self.gate.set_status("PROP RULES VERIFIED" if resolved else "PROP RULES UNRESOLVED", "ok" if resolved else "locked"); self.gate_reason.setText("Broker arming remains blocked." if not resolved else "Profile rules are verified; this build still remains disarmed.")
        self.set_summary([f"Profile: {paper.profile_name}", f"Balance {_money(paper.balance)}   Target {_money(paper.profit_target)} ({paper.target_progress:.0%})", f"Drawdown room {_money(paper.drawdown_room)}   Risk remaining {_money(paper.risk_remaining)}", f"Consistency: {'—' if paper.consistency_share is None else f'{paper.consistency_share:.0%}'}", "", f"Prop rules verified: {'yes' if resolved else 'NO — broker arming blocked'}"])


class ExecutionScreen(DashboardScreen):
    """PAPER, read-only DEMO status, and permanently separated LIVE blockers."""

    def __init__(self) -> None:
        super().__init__("screen_execution", "Execution", "PAPER operates automatically. Tradovate DEMO is read-only. LIVE remains locked.")
        self._commander = None
        self._message_box = QMessageBox
        status = Card("Tradovate DEMO · read-only", "execution_demo_card"); self.demo_state = StatusBadge("DISCONNECTED", "execution_demo_state"); self.demo_table = EvidenceTable(("Field", "Backend-confirmed value"), "execution_demo_table"); status.body.addWidget(self.demo_state); status.body.addWidget(self.demo_table)
        buttons = QHBoxLayout(); self.connect_button = QPushButton("Connect DEMO (read-only)"); self.connect_button.setObjectName("execution_connect_demo"); self.connect_button.setProperty("variant", "primary"); self.connect_button.clicked.connect(lambda: self._submit("connect_readonly")); self.sync_button = QPushButton("Sync now"); self.sync_button.setObjectName("execution_sync_now"); self.sync_button.setProperty("variant", "secondary"); self.sync_button.clicked.connect(lambda: self._submit("sync_now")); self.disconnect_button = QPushButton("Disconnect"); self.disconnect_button.setObjectName("execution_disconnect"); self.disconnect_button.setProperty("variant", "danger"); self.disconnect_button.clicked.connect(self._disconnect_clicked)
        for button in (self.connect_button, self.sync_button, self.disconnect_button): button.setEnabled(False); buttons.addWidget(button)
        buttons.addStretch(1); status.body.addLayout(buttons); self.content_layout.addWidget(status)
        credentials = Card("Credential presence · values never shown", "execution_credentials_card"); self.credentials = EvidenceTable(("Variable", "Presence"), "execution_credentials_table"); credentials.body.addWidget(self.credentials); self.content_layout.addWidget(credentials)
        gates = Card("Order arming and LIVE safety gates", "execution_gates_card"); self.demo_blockers = _value("execution_demo_blockers"); self.demo_blockers.setWordWrap(True); self.live_badge = StatusBadge("LIVE LOCKED", "execution_live_locked", "locked"); self.live_blockers = _value("execution_live_blockers"); self.live_blockers.setWordWrap(True); gates.body.addWidget(self.live_badge); gates.body.addWidget(self.demo_blockers); gates.body.addWidget(self.live_blockers); self.content_layout.addWidget(gates)

    def set_commander(self, commander: object | None) -> None:
        self._commander = commander

    def _submit(self, name: str) -> None:
        if self._commander is not None: self._commander.submit(name)

    def _disconnect_clicked(self) -> None:
        if self._message_box.question(self, "Disconnect Tradovate DEMO", "Disconnect the read-only DEMO session?") == self._message_box.StandardButton.Yes: self._submit("disconnect")

    def render(self, snapshot: AppSnapshot) -> None:
        execution = snapshot.execution; connected = execution.demo_state == "CONNECTED_READONLY"; have_commander = self._commander is not None
        self.connect_button.setEnabled(have_commander and not connected); self.sync_button.setEnabled(have_commander and connected); self.disconnect_button.setEnabled(have_commander and connected)
        self.demo_state.set_status(execution.demo_state, "ok" if connected else ("fail" if execution.demo_state == "ERROR" else "neutral"))
        sync_age = "never" if execution.demo_sync_age_seconds is None else f"{execution.demo_sync_age_seconds:.0f}s ago"
        self.demo_table.set_rows((("Account", execution.demo_account or "—"), ("Balance", execution.demo_balance), ("Position", f"{execution.demo_position_net:+d}"), ("Working orders", str(execution.demo_working_orders)), ("Broker contract", execution.demo_contract or "—"), ("Bookmap contract", snapshot.market.contract), ("Last sync", sync_age), ("Reconnects", str(execution.demo_reconnects)), ("Orphan orders", str(execution.demo_orphan_orders))))
        self.credentials.set_rows(tuple((name, "set" if present else "MISSING") for name, present in execution.demo_credential_checklist))
        self.demo_blockers.setText("DEMO order arming is blocked because:\n" + "\n".join(f"• {reason}" for reason in execution.demo_arming_blockers or ("blockers not evaluated",)))
        self.live_blockers.setText("LIVE unmet requirements:\n" + "\n".join(f"• {reason}" for reason in execution.live_blockers or ("gate not evaluated",)) + "\nDelayed data can never place a broker order.")
        lines = ["PAPER: automatic delayed-paper simulation (always on).", "", f"TRADOVATE DEMO: {execution.demo_state}   armed: {'YES' if execution.demo_armed else 'no (never persists)'}", f"  Account: {execution.demo_account or '—'}   Balance: {execution.demo_balance}", f"  Position: {execution.demo_position_net:+d}   Working orders: {execution.demo_working_orders}", f"  Broker contract: {execution.demo_contract or '—'}   Bookmap: {snapshot.market.contract}", f"  Last sync: {sync_age}   Reconnects: {execution.demo_reconnects}   Orphan orders: {execution.demo_orphan_orders}", "", "  Credentials (values are never shown or stored):", *[f"    [{'set' if present else 'MISSING'}] {name}" for name, present in execution.demo_credential_checklist], "", "  DEMO order arming is blocked because:", *[f"    • {reason}" for reason in execution.demo_arming_blockers or ("blockers not evaluated",)], "", "LIVE: LOCKED. Unmet requirements:", *[f"  • {reason}" for reason in execution.live_blockers or ("gate not evaluated",)], "", "Delayed data can never place a broker order."]
        self.set_summary(lines)


class DiagnosticsScreen(DashboardScreen):
    """Copyable component, queue, conservation, and latency diagnostics."""

    def __init__(self) -> None:
        super().__init__("screen_diagnostics", "Diagnostics and Settings", "Backend-confirmed health and conservation metrics. Source delay remains separate from app processing lag.")
        metrics = Card("Capture and analysis metrics", "diagnostics_metrics_card"); self.persisted = StatTile("Persisted/s", "diagnostics_persisted"); self.flush = StatTile("Flush latency", "diagnostics_flush"); self.offered = StatTile("Analysis offered", "diagnostics_offered"); self.skipped = StatTile("Analysis skipped", "diagnostics_skipped"); metrics.body.addLayout(stat_grid((self.persisted, self.flush, self.offered, self.skipped), 4)); self.intake = MetricMeter("Intake queue", "diagnostics_intake"); self.recorder = MetricMeter("Recorder queue", "diagnostics_recorder"); metrics.body.addWidget(self.intake); metrics.body.addWidget(self.recorder); self.content_layout.addWidget(metrics)
        health = Card("Supervised components", "diagnostics_health_card"); self.health_table = EvidenceTable(("Component", "State", "Detail"), "diagnostics_health_table"); health.body.addWidget(self.health_table); self.content_layout.addWidget(health)
        settings = Card("Interface settings", "diagnostics_settings_card"); note = QLabel("Theme, accessibility zoom, and layout reset are available through the window API. They do not alter backend or trading state."); note.setWordWrap(True); settings.body.addWidget(note); self.content_layout.addWidget(settings)

    def render(self, snapshot: AppSnapshot) -> None:
        capture = snapshot.capture; self.persisted.set_value(f"{capture.persisted_per_second:,.0f}"); self.flush.set_value(f"{capture.flush_latency_ms:.2f} ms"); self.offered.set_value(f"{capture.analysis_offered:,}", f"{capture.analysis_processed:,} processed"); self.skipped.set_value(f"{capture.analysis_skipped:,}", "paper-only analysis skips")
        intake_fraction = capture.intake_occupancy / capture.intake_capacity if capture.intake_capacity else 0.0; recorder_fraction = capture.recorder_occupancy / capture.recorder_capacity if capture.recorder_capacity else 0.0
        self.intake.set_fraction(intake_fraction, f"{capture.intake_occupancy}/{capture.intake_capacity}"); self.recorder.set_fraction(recorder_fraction, f"{capture.recorder_occupancy}/{capture.recorder_capacity}")
        self.health_table.set_rows(tuple((component.name, _HEALTH_TEXT[component.health], component.detail) for component in snapshot.components))
        lag = "n/a" if capture.analysis_lag_ms is None else f"{capture.analysis_lag_ms:.1f} ms"
        self.set_summary([f"Lifecycle: {snapshot.lifecycle_state}", f"Intake queue: {capture.intake_occupancy}/{capture.intake_capacity}", f"Recorder queue: {capture.recorder_occupancy}/{capture.recorder_capacity}", f"Recorder flush latency: {capture.flush_latency_ms:.2f} ms", f"Persisted/s: {capture.persisted_per_second:,.0f}", "", "Analysis feed (conservation — every event accounted):", f"  offered {capture.analysis_offered:,}   processed {capture.analysis_processed:,}   skipped {capture.analysis_skipped:,} (paper-only)", f"  pipeline lag {lag} (excludes the intentional source delay)", "", "Components:", *[f"  • {component.name}: {_HEALTH_TEXT[component.health]} — {component.detail}" for component in snapshot.components]])


class _AutonomousReader(QObject):
    """Reads autonomous state + reports OFF the Qt thread and signals the view.

    File I/O runs on a global thread-pool worker; the immutable
    ``AutonomousSnapshot`` is delivered to the GUI thread via a queued signal.
    Never blocks capture or the paint loop.
    """

    ready = Signal(object)

    def __init__(self, store_root: object, reports_root: object) -> None:
        super().__init__()
        self._store_root = store_root
        self._reports_root = reports_root
        self._busy = False

    def request(self) -> None:
        """Schedule one off-thread read unless one is already running."""
        if self._busy:
            return
        self._busy = True
        QThreadPool.globalInstance().start(_ReadTask(self))


class _ReadTask(QRunnable):
    def __init__(self, reader: "_AutonomousReader") -> None:
        super().__init__()
        self._reader = reader
        # Capture roots now: the reader QObject may be destroyed (screen closed)
        # before or during this background run.
        self._store_root = reader._store_root
        self._reports_root = reader._reports_root

    def run(self) -> None:  # executes on a worker thread
        view: AutonomousSnapshot | None = None
        try:
            view = read_autonomous_snapshot(self._store_root, self._reports_root)
        except Exception:  # noqa: BLE001 - a bad read must never crash the GUI
            view = None
        try:
            self._reader._busy = False
            if view is not None:
                self._reader.ready.emit(view)
        except RuntimeError:
            # The reader (and its screen) was deleted while we ran; the queued
            # signal has nowhere to go. Drop it silently - never crash the GUI.
            return


class AutonomousIntelligenceScreen(DashboardScreen):
    """Real, read-only view of the governed shadow-only autonomous system."""

    REFRESH_MS = 5000

    def __init__(self, store_root: object = DEFAULT_STORE_ROOT,
                 reports_root: object = DEFAULT_REPORTS_ROOT) -> None:
        super().__init__(
            "screen_autonomous", "Autonomous Intelligence",
            "Governed, shadow-only candidate research. No autonomous change ever reaches "
            "runtime, risk, or the broker; LIVE stays locked.")
        status = Card("Autonomous system status", "autonomous_status_card")
        self.service = StatTile("Service", "autonomous_service")
        self.candidates_tile = StatTile("Candidates", "autonomous_candidates")
        self.completed_tile = StatTile("Completed", "autonomous_completed")
        self.disk_tile = StatTile("Store size", "autonomous_disk")
        status.body.addLayout(stat_grid(
            (self.service, self.candidates_tile, self.completed_tile, self.disk_tile), 4))
        self.safety = StatusBadge("SHADOW-ONLY · LIVE LOCKED", "autonomous_safety", "locked")
        status.body.addWidget(self.safety)
        self.note = _value("autonomous_note")
        self.note.setWordWrap(True)
        status.body.addWidget(self.note)
        self.content_layout.addWidget(status)

        board = Card("Governed task board (candidate lifecycle)", "autonomous_board_card")
        self.board_table = EvidenceTable(("Lifecycle state", "Candidates"), "autonomous_board_table")
        board.body.addWidget(self.board_table)
        self.content_layout.addWidget(board)

        candidates = Card("Candidate proposals · shadow-only, no runtime effect",
                          "autonomous_candidates_card")
        self.candidates_table = EvidenceTable(
            ("ID", "Kind", "State", "Hypothesis", "Attempts", "Last error"),
            "autonomous_candidates_table")
        candidates.body.addWidget(self.candidates_table)
        self.content_layout.addWidget(candidates)

        timeline = Card("Autonomous activity timeline", "autonomous_timeline_card")
        self.timeline_table = EvidenceTable(("When", "Event", "Detail"), "autonomous_timeline_table")
        timeline.body.addWidget(self.timeline_table)
        self.content_layout.addWidget(timeline)

        self._reader = _AutonomousReader(store_root, reports_root)
        self._reader.ready.connect(self.apply_view)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._reader.request)
        # Deterministic empty state on construction; the timer loads real data.
        self.apply_view(AutonomousSnapshot(
            False, False, 0, 0, note="Autonomous intelligence has not reported yet."))

    def showEvent(self, event: object) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)  # type: ignore[arg-type]
        # Drive reads only from the timer (interval > 0). It cannot fire during a
        # single synchronous processEvents(), so offscreen captures stay
        # deterministic while the live app refreshes off-thread.
        self._timer.start(self.REFRESH_MS)

    def hideEvent(self, event: object) -> None:  # noqa: N802 - Qt API
        self._timer.stop()
        super().hideEvent(event)  # type: ignore[arg-type]

    def render(self, snapshot: AppSnapshot) -> None:
        """Autonomous content is loaded off-thread on a timer; nothing per-frame."""
        return None

    def apply_view(self, view: AutonomousSnapshot) -> None:
        """Update widgets from an immutable autonomous snapshot (Qt thread, no I/O)."""
        self.service.set_value("RUNNING" if view.service_running else "IDLE",
                               "shadow-only orchestration" if view.service_running
                               else "not running")
        self.candidates_tile.set_value(str(view.candidate_count), "tracked")
        self.completed_tile.set_value(str(view.completed_count), "checkpointed")
        self.disk_tile.set_value(f"{view.disk_bytes / 1024:.0f} KB" if view.disk_bytes else "0 KB",
                                 "append-only evidence")
        self.note.setText(view.note)
        self.board_table.set_rows(tuple(
            (state, str(count)) for state, count in view.counts_by_state.items())
            or (("no candidates proposed yet", "0"),))
        self.candidates_table.set_rows(tuple(
            (c.candidate_id, c.kind, c.state, c.hypothesis[:60], str(c.attempts), c.last_error[:40])
            for c in view.candidates) or (("—", "—", "—", "no candidates yet", "—", "—"),))
        self.timeline_table.set_rows(tuple(
            (e.when, e.event, e.detail) for e in view.recent_activity)
            or (("—", "no activity recorded", "—"),))
        self.set_summary([
            f"Autonomous service: {'RUNNING' if view.service_running else 'IDLE'}",
            f"Candidates: {view.candidate_count}   Completed: {view.completed_count}",
            "Shadow-only — no runtime authority; LIVE locked.",
            view.note,
            "",
            "Task board:",
            *[f"  {state}: {count}" for state, count in view.counts_by_state.items()],
        ])


class ReportsScreen(DashboardScreen):
    """Live index of every generated report, with provenance and covered range."""

    REFRESH_MS = 15000
    MAX_ROWS = 120

    def __init__(self, store_root: object = DEFAULT_STORE_ROOT,
                 reports_root: object = DEFAULT_REPORTS_ROOT) -> None:
        super().__init__(
            "screen_reports", "Reports",
            "Every generated report, read live from data/reports. Provenance is labelled; "
            "reports never state that profitability has been proven.")
        summary = Card("Report library", "reports_summary_card")
        self.total_tile = StatTile("Total reports", "reports_total")
        self.categories_tile = StatTile("Categories", "reports_categories")
        self.delayed_tile = StatTile("Delayed", "reports_delayed")
        self.synthetic_tile = StatTile("Synthetic", "reports_synthetic")
        summary.body.addLayout(stat_grid(
            (self.total_tile, self.categories_tile, self.delayed_tile, self.synthetic_tile), 4))
        self.showing = _value("reports_showing")
        summary.body.addWidget(self.showing)
        self.content_layout.addWidget(summary)

        table = Card("Reports (newest first)", "reports_table_card")
        self.table = EvidenceTable(
            ("Category", "Title", "Covered", "Provenance", "Modified", "Path"), "reports_table")
        table.body.addWidget(self.table)
        self.content_layout.addWidget(table)

        self._reader = _AutonomousReader(store_root, reports_root)
        self._reader.ready.connect(self.apply_view)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._reader.request)
        # Deterministic empty state on construction; the timer loads real reports.
        self.apply_view(AutonomousSnapshot(
            False, False, 0, 0, note="Reports load shortly."))

    def showEvent(self, event: object) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)  # type: ignore[arg-type]
        # Timer-only refresh keeps offscreen captures deterministic (see the
        # Autonomous screen) while the live app reads reports off-thread.
        self._timer.start(self.REFRESH_MS)

    def hideEvent(self, event: object) -> None:  # noqa: N802 - Qt API
        self._timer.stop()
        super().hideEvent(event)  # type: ignore[arg-type]

    def render(self, snapshot: AppSnapshot) -> None:
        """Reports are loaded off-thread on a timer; nothing per-frame."""
        return None

    def apply_view(self, view: AutonomousSnapshot) -> None:
        reports = view.reports
        categories = {report.category for report in reports}
        delayed = sum(1 for report in reports if report.provenance == "DELAYED")
        synthetic = sum(1 for report in reports if report.provenance == "SYNTHETIC")
        self.total_tile.set_value(str(len(reports)), "read live from disk")
        self.categories_tile.set_value(str(len(categories)), "report types")
        self.delayed_tile.set_value(str(delayed), "delayed-data provenance")
        self.synthetic_tile.set_value(str(synthetic), "clearly synthetic")
        shown = reports[:self.MAX_ROWS]
        self.showing.setText(
            f"Showing newest {len(shown)} of {len(reports)} reports."
            if len(reports) > len(shown) else f"Showing all {len(reports)} reports.")
        self.table.set_rows(tuple(
            (r.category, r.title, r.covered, r.provenance, r.modified, r.path) for r in shown)
            or (("—", "no reports found", "—", "—", "—", "—"),))
        self.set_summary([
            f"Reports: {len(reports)} across {len(categories)} categories",
            f"Delayed provenance: {delayed}   Synthetic: {synthetic}",
            "Reports never state that profitability has been proven.",
            "",
            *[f"  [{r.category}] {r.title} ({r.covered}, {r.provenance})" for r in shown[:40]],
        ])


def build_screens() -> dict[str, Screen]:
    """Construct every retained screen keyed by its navigation destination."""
    return {
        OVERVIEW: OverviewScreen(), LIVE_ORDER_FLOW: LiveOrderFlowScreen(),
        PAPER_TRADING: PaperTradingScreen(), SESSIONS_REPLAY: SessionsReplayScreen(),
        RESEARCH_HEALTH: ResearchHealthScreen(), RISK_LUCID: RiskLucidScreen(),
        EXECUTION: ExecutionScreen(), DIAGNOSTICS: DiagnosticsScreen(),
        AUTONOMOUS: AutonomousIntelligenceScreen(), REPORTS: ReportsScreen(),
    }
