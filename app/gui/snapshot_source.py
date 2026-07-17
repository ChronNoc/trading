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

# What the current Bookmap bridge genuinely delivers. Anything the feed cannot
# support must say so rather than be silently rendered.
DEFAULT_CAPABILITIES: tuple[tuple[str, Capability, str], ...] = (
    ("Aggregated depth", Capability.AVAILABLE, "bridge forwards depth_update events"),
    ("Trade prints", Capability.AVAILABLE, "bridge forwards trade events"),
    ("Aggressor side", Capability.AVAILABLE,
     "TradeInfo.isBidAggressor verified against the installed Bookmap jars"),
    ("CVD / rolling delta", Capability.AVAILABLE, "derived from verified aggressor side"),
    ("Liquidity blocks / reloads / absorption", Capability.HEURISTIC,
     "derived from depth persistence - not native Bookmap indicator values"),
    ("MBO / native iceberg", Capability.UNAVAILABLE,
     "the bridge receives no order IDs; native iceberg data is not exposed"),
    ("Heatmap pixels", Capability.UNAVAILABLE,
     "Bookmap's visual heatmap is not exposed through the add-on API"),
    ("Broker execution", Capability.UNAVAILABLE, "delayed data may never place an order"),
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
    ) -> None:
        """Bind the live components; all are optional for headless/GUI-only use."""
        self._controller = controller
        self._pipeline = pipeline_holder
        self._research = research_service
        self._receiver_status = receiver_status
        self._market_state = market_state
        self._paper_engine = paper_engine
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
            execution=self._execution(),
            components=self._components(),
            capabilities=DEFAULT_CAPABILITIES,
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
        return CaptureSnapshot(
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
