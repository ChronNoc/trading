"""Build the GUI's immutable :class:`AppSnapshot` from live in-memory state.

This is the boundary that keeps the Qt thread clean. It reads only objects that
are already in memory (the runtime controller's health, the capture pipeline's
metrics, the research service's status, the selected account profile) and never
touches disk, network, or research from the caller's thread.

The profile is read once at construction, not per frame: it is the selected
account for the whole session.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Callable

from app.gui.view_models import (
    AppSnapshot,
    Capability,
    CaptureSnapshot,
    ComponentHealth,
    ExecutionSnapshot,
    Health,
    MarketSnapshot,
    PaperSnapshot,
    ResearchSnapshot,
)

# Capabilities that are STRUCTURAL - true regardless of what arrives on the wire,
# because the API cannot express them at all. Everything else is observed from the
# real stream (see app/market/capabilities.py), because a hardcoded
# "Trade prints: AVAILABLE" lied on the 71 of 200 recorded sessions that carried
# no trades whatsoever.
STRUCTURAL_CAPABILITIES: tuple[tuple[str, Capability, str], ...] = (
    ("Heatmap pixels", Capability.UNAVAILABLE,
     "Bookmap's visual heatmap is not exposed through the add-on API"),
    ("Broker execution", Capability.UNAVAILABLE, "delayed data may never place an order"),
)

# Kept for callers with no live engine attached: the honest pre-observation view.
DEFAULT_CAPABILITIES: tuple[tuple[str, Capability, str], ...] = (
    ("Aggregated depth", Capability.UNVERIFIED, "no depth update observed yet"),
    ("Trade prints", Capability.UNVERIFIED, "no trade observed yet"),
    ("Aggressor side", Capability.UNVERIFIED, "no trade observed yet"),
    ("CVD / rolling delta", Capability.UNVERIFIED, "no trade observed yet"),
    ("Liquidity blocks / reloads / absorption", Capability.HEURISTIC,
     "derived from depth persistence - not native Bookmap indicator values"),
    ("MBO / native iceberg", Capability.UNAVAILABLE,
     "the bridge receives no order IDs; native iceberg data is not exposed"),
    *STRUCTURAL_CAPABILITIES,
)


class SnapshotSource:
    """Assembles an AppSnapshot from live components."""

    def __init__(
        self,
        *,
        controller: object | None = None,
        pipeline_holder: object | None = None,
        research_service: object | None = None,
        receiver_status: Callable[[], object] | None = None,
        market_state: Callable[[], object] | None = None,
        paper_engine: object | None = None,
        analysis_feed: object | None = None,
    ) -> None:
        """Bind the live components; all are optional for headless/GUI-only use."""
        self._controller = controller
        self._pipeline = pipeline_holder
        self._research = research_service
        self._receiver_status = receiver_status
        self._market_state = market_state
        self._paper_engine = paper_engine
        self._analysis_feed = analysis_feed
        from app.risk.account_profile import load_selected_profile

        self._profile = load_selected_profile()

    def __call__(self) -> AppSnapshot:
        """Return the current immutable snapshot (safe to call from the GUI)."""
        return AppSnapshot(
            lifecycle_state=self._lifecycle_state(),
            market=self._market(),
            capture=self._capture(),
            paper=self._paper(),
            research=self._research_snapshot(),
            profitability=self._profitability_snapshot(),
            execution=self._execution(),
            components=self._components(),
            capabilities=self._capabilities(),
            next_action=self._next_action(),
            blocker=self._blocker(),
        )

    # -- sections -----------------------------------------------------------------

    def _runtime(self) -> object | None:
        if self._controller is None:
            return None
        try:
            return self._controller.snapshot()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - never let the GUI die on a bad read
            return None

    def _lifecycle_state(self) -> str:
        runtime = self._runtime()
        binding = self._binding()
        if runtime is None:
            return "STARTING"
        if not (binding and getattr(binding, "listening", False)):
            return "STARTING"
        if runtime.bookmap_status != "connected":
            return "WAITING_FOR_BOOKMAP"
        if not runtime.recording:
            return "VALIDATING_STREAM"
        capture = self._capture()
        if capture.current_session_drops:
            return "CAPTURE_INVALIDATED"
        return "RECORDING"

    def _binding(self) -> object | None:
        if self._receiver_status is None:
            return None
        try:
            return self._receiver_status()
        except Exception:  # noqa: BLE001
            return None

    def _market(self) -> MarketSnapshot:
        runtime = self._runtime()
        state = None
        if self._market_state is not None:
            try:
                state = self._market_state()
            except Exception:  # noqa: BLE001
                state = None
        metrics = self._metrics()
        delay = int(getattr(runtime, "data_delay_minutes", 0) or 0) if runtime else 15
        return MarketSnapshot(
            contract=self._contract(),
            last_price=getattr(state, "mid_price", None),
            best_bid=getattr(state, "best_bid", None),
            best_ask=getattr(state, "best_ask", None),
            spread=getattr(state, "spread", None),
            mid_price=getattr(state, "mid_price", None),
            cumulative_delta=getattr(state, "cumulative_volume_delta", Decimal("0")) or Decimal("0"),
            buy_volume=getattr(state, "executed_buy_volume", Decimal("0")) or Decimal("0"),
            sell_volume=getattr(state, "executed_sell_volume", Decimal("0")) or Decimal("0"),
            is_delayed=delay > 0,
            source_delay_minutes=delay,
            # Application processing age, NOT the source delay.
            processing_age_ms=int(getattr(metrics, "end_to_end_lag_ms", 0)) if metrics else None,
            event_rate_per_second=float(getattr(metrics, "ingress_events_per_second", 0.0)) if metrics else 0.0,
        )

    def _contract(self) -> str:
        """Resolve the display contract, falling back to the bound symbol."""
        runtime = self._runtime()
        resolved = getattr(runtime, "exact_contract", "") if runtime else ""
        if resolved and resolved != "unknown":
            return resolved
        if self._paper_engine is not None:
            try:
                bound = self._paper_engine.status().contract  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                bound = ""
            if bound:
                return f"{bound} (resolving contract month)"
        return "resolving"

    def _metrics(self) -> object | None:
        if self._pipeline is None:
            return None
        try:
            return self._pipeline.snapshot()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return None

    def _session_id(self) -> str:
        """Return the ACTIVE recorder session id, never 'unknown'."""
        if self._paper_engine is not None:
            try:
                bound = self._paper_engine.status().session_id  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                bound = ""
            if bound and bound != "pending":
                return bound
        runtime = self._runtime()
        return getattr(runtime, "session_date", "") if runtime else ""

    def _capture(self) -> CaptureSnapshot:
        runtime = self._runtime()
        binding = self._binding()
        metrics = self._metrics()
        intake = getattr(metrics, "intake", {}) or {} if metrics else {}
        recorder = getattr(metrics, "recorder", {}) or {} if metrics else {}
        feed_metrics = None
        if self._analysis_feed is not None:
            try:
                feed_metrics = self._analysis_feed.metrics()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                feed_metrics = None
        return CaptureSnapshot(
            analysis_offered=getattr(feed_metrics, "offered", 0),
            analysis_processed=getattr(feed_metrics, "processed", 0),
            analysis_skipped=getattr(feed_metrics, "skipped", 0),
            analysis_lag_ms=getattr(feed_metrics, "lag_ms", None),
            receiver_listening=bool(binding and getattr(binding, "listening", False)),
            bookmap_connected=bool(runtime and runtime.bookmap_status == "connected"),
            recording=bool(runtime and runtime.recording),
            session_id=self._session_id(),
            current_session_drops=int(getattr(runtime, "current_session_dropped_message_count", 0) or 0)
            if runtime else 0,
            lifetime_bridge_drops=int(getattr(runtime, "dropped_message_count", 0) or 0) if runtime else 0,
            intake_occupancy=int(intake.get("occupancy", 0)),
            intake_capacity=int(intake.get("capacity", 0)),
            recorder_occupancy=int(recorder.get("occupancy", 0)),
            recorder_capacity=int(recorder.get("capacity", 0)),
            flush_latency_ms=float(getattr(metrics, "last_flush_duration_ms", 0.0)) if metrics else 0.0,
            persisted_per_second=float(getattr(metrics, "persisted_events_per_second", 0.0)) if metrics else 0.0,
        )

    def _paper(self) -> PaperSnapshot:
        profile = self._profile
        base = PaperSnapshot(
            profile_name=profile.display_name,
            balance=profile.account_size,
            starting_balance=profile.account_size,
            profit_target=profile.profit_target,
            target_progress=profile.target_progress(profile.account_size),
            drawdown_room=profile.drawdown_room(profile.account_size, profile.account_size),
            risk_remaining=profile.max_loss_limit,
            open_position="flat",
            setup_name="none",
            # Real trades only ever arrive from the delayed-paper engine / ledger;
            # the GUI never invents them.
            trades=0,
        )
        if self._paper_engine is None:
            return base
        try:
            status = self._paper_engine.status()  # type: ignore[union-attr]
            recent = self._paper_engine.recent_evaluations(limit=1)  # type: ignore[union-attr]
            trades = self._paper_engine.recent_trades(limit=25)  # type: ignore[union-attr]
            risk_rejections = self._paper_engine.top_risk_rejections()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - never let the GUI die on a bad read
            return base

        from dataclasses import replace

        from app.gui.view_models import SetupCheck, TradeRow

        checks: tuple[SetupCheck, ...] = ()
        if recent:
            checks = tuple(
                SetupCheck(name=c.name, passed=c.passed, reason=c.message)
                for c in recent[-1].conditions
            )
        rows = tuple(
            TradeRow(
                direction=trade.direction.value, contracts=trade.contracts,
                entry=str(trade.entry_price), exit=str(trade.exit_price),
                net_pnl=f"{trade.net_pnl:+.2f}", close_reason=trade.close_reason.value,
                is_synthetic_fixture=trade.is_synthetic_fixture,
            )
            for trade in reversed(trades)  # newest first
        )
        # Account figures come from the EXECUTOR's real balance, not the profile
        # defaults: the panel must never show money the simulation does not hold.
        balance = Decimal(status.balance)
        return replace(
            base,
            mode=f"DELAYED PAPER — {status.state}",
            setup_name=status.last_setup or "none",
            setup_checks=checks,
            evaluations=status.evaluations,
            top_rejections=status.top_rejections,
            balance=balance,
            net_pnl=Decimal(status.realized_pnl),
            target_progress=profile.target_progress(balance),
            drawdown_room=profile.drawdown_room(balance, max(balance, profile.account_size)),
            trades=status.trades,
            wins=status.wins,
            losses=status.losses,
            open_position=status.open_position or "flat",
            pending_order=status.pending_order or "none",
            position_entry=status.position_entry,
            position_stop=status.position_stop,
            position_target=status.position_target,
            unrealized_pnl=Decimal(status.unrealized_pnl),
            candidates=status.candidates,
            risk_rejected=status.risk_rejected,
            risk_rejections=tuple(risk_rejections),
            recent_trades=rows,
            malformed_events=status.malformed_events,
            condition_stats=tuple(self._paper_engine.condition_stats())  # type: ignore[attr-defined]
            if hasattr(self._paper_engine, "condition_stats") else (),
            analysis_events_skipped=status.analysis_events_skipped,
            causality_breaks=status.causality_breaks,
            window_span_seconds=status.window_span_seconds,
        )

    def _capabilities(self) -> tuple[tuple[str, Capability, str], ...]:
        """Report what the feed has ACTUALLY delivered, plus structural limits.

        A hardcoded constant claimed "Trade prints: AVAILABLE" on the 71 of 200
        recorded sessions that contained no trades at all.
        """
        if self._paper_engine is None or not hasattr(self._paper_engine, "capabilities"):
            return DEFAULT_CAPABILITIES
        try:
            observed = self._paper_engine.capabilities().states()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - the GUI must never die on a read
            return DEFAULT_CAPABILITIES
        return (
            *((state.label, Capability(state.status.value), state.reason) for state in observed),
            *STRUCTURAL_CAPABILITIES,
        )

    def _profitability_snapshot(self) -> "ProfitabilitySnapshot":
        """Read the meter the research thread computed. Never computes it here.

        Computing this touches disk (session catalog + processed episodes), and
        doing that on the Qt timer is what starved capture and froze the app.
        """
        from app.gui.view_models import ProfitabilitySnapshot, ProgressGateRow

        cache = getattr(self._research, "progress_cache", None)
        if cache is None:
            return ProfitabilitySnapshot()
        try:
            progress = cache.snapshot()
            error = cache.error
        except Exception as exc:  # noqa: BLE001 - the GUI must never die on a read
            return ProfitabilitySnapshot(error=f"{type(exc).__name__}: {exc}")
        if progress is None:
            return ProfitabilitySnapshot(error=error)
        return ProfitabilitySnapshot(
            fraction=progress.fraction,
            headline=progress.headline,
            claim_supported=progress.profitable_claim_supported,
            computed=True,
            error=error,
            gates=tuple(
                ProgressGateRow(label=gate.label, status=gate.status,
                                observed=str(gate.observed), threshold=str(gate.threshold))
                for gate in progress.gates
            ),
        )

    def _research_snapshot(self) -> ResearchSnapshot:
        if self._research is None:
            return ResearchSnapshot()
        try:
            status = self._research.status()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return ResearchSnapshot()
        from app.research.auto_research import detect_hardware, gpu_workload_note

        return ResearchSnapshot(
            state=status.state, active_workers=status.active_workers,
            requested_workers=status.requested_workers, queued_jobs=status.queued_jobs,
            completed_jobs=status.completed_jobs, failed_jobs=status.failed_jobs,
            canonical_trades=status.canonical_trades,
            experimental_trades=status.experimental_trades,
            unique_setups=status.unique_setups, duplicate_overlap=status.duplicate_overlap,
            independent_days=status.independent_days, throttle_reason=status.throttle_reason,
            gpu_note=gpu_workload_note(detect_hardware(), gpu_enabled=False),
        )

    def _execution(self) -> ExecutionSnapshot:
        from pathlib import Path

        from app.execution.live_gate import LiveGateInputs, evaluate_live_gate, read_live_enabled

        decision = evaluate_live_gate(LiveGateInputs(
            live_enabled_in_config=read_live_enabled(Path("config/production_config.yaml")),
            prop_rules_resolved=self._profile.resolved,
        ))
        return ExecutionSnapshot(
            environment="PAPER", connected=False, demo_armed=False, live_armed=False,
            live_blockers=decision.failures, prop_rules_resolved=self._profile.resolved,
        )

    def _components(self) -> tuple[ComponentHealth, ...]:
        capture = self._capture()
        research = self._research_snapshot()
        return (
            ComponentHealth("capture", capture.health,
                            "recording" if capture.recording else "waiting for Bookmap"),
            ComponentHealth("recorder", Health.OK if capture.recording else Health.IDLE,
                            f"{capture.persisted_per_second:,.0f} events/s persisted"),
            ComponentHealth("paper", Health.IDLE, "awaiting a qualifying setup"),
            ComponentHealth("research", Health.OK if research.active_workers else Health.IDLE,
                            research.state),
            ComponentHealth("live", Health.LOCKED, "locked by the validation gate"),
        )

    def _blocker(self) -> str:
        capture = self._capture()
        if capture.current_session_drops:
            return f"{capture.current_session_drops:,} events dropped this session — segment invalidated"
        if not capture.receiver_listening:
            return "receiver is not listening"
        if not capture.bookmap_connected:
            return "no Bookmap add-on has connected"
        return ""

    def _next_action(self) -> str:
        capture = self._capture()
        if not capture.receiver_listening:
            return "Start the assistant (start_mnq_assistant.bat)."
        if not capture.bookmap_connected:
            return "In Bookmap: open an MNQ chart, connect the data provider, and enable the MNQ WebSocket Forwarder add-on."
        if not capture.recording:
            return "Validating the stream; recording begins once events pass validation."
        return "Recording is healthy; let it capture complete sessions."
