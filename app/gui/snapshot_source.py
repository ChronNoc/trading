"""Build the GUI's immutable :class:`AppSnapshot` from live in-memory state.

This is the boundary that keeps the Qt thread clean. It reads only objects that
are already in memory (the runtime controller's health, the capture pipeline's
metrics, the research service's status, the selected account profile) and never
touches disk, network, or research from the caller's thread.

The profile is read once at construction, not per frame: it is the selected
account for the whole session.
"""

from __future__ import annotations

from collections import deque
from decimal import Decimal
from pathlib import Path
from typing import Callable

from app.machine_learning.feature_contract import FEATURE_PARITY_STATE
from app.gui.view_models import (
    AppSnapshot,
    Capability,
    CaptureSnapshot,
    ChallengerSummary,
    ComponentHealth,
    ExecutionSnapshot,
    Health,
    MarketHistoryPoint,
    MarketSnapshot,
    ModelSnapshot,
    PaperSnapshot,
    PipelineSnapshot,
    PipelineStageRow,
    ProfitabilitySnapshot,
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
        feature_sink: object | None = None,
        demo_service: object | None = None,
        models_root: Path = Path("data/models"),
        model_approval_path: Path = Path("config/model_approval.yaml"),
        model_loader: object | None = None,
        outcome_tracker: object | None = None,
        raw_root: Path | None = None,
        paper_ledger_path: Path | None = None,
    ) -> None:
        """Bind live components and the lightweight offline model-state source."""
        self._controller = controller
        self._pipeline = pipeline_holder
        self._research = research_service
        self._receiver_status = receiver_status
        self._market_state = market_state
        self._paper_engine = paper_engine
        self._analysis_feed = analysis_feed
        self._feature_sink = feature_sink
        self._demo_service = demo_service
        self._models_root = models_root
        self._model_loader = model_loader
        self._outcome_tracker = outcome_tracker
        # Session catalog (Sessions & Replay). Disabled unless a raw_root is given
        # (the backend passes it), so tests never scan the real data/raw tree.
        self._raw_root = raw_root
        self._ledger_path = paper_ledger_path
        self._sessions_cache: tuple = ()
        self._sessions_total = 0
        self._sessions_scanned_at = -1.0
        self._model_approval_path = model_approval_path
        self._model_cache_key: tuple[object, ...] | None = None
        self._model_cache = ModelSnapshot()
        self._market_history: deque[MarketHistoryPoint] = deque(maxlen=300)
        self._history_session_id = ""
        self._history_timestamp_ns = -1
        self._frame_cache: dict[str, object | None] | None = None
        from app.risk.account_profile import load_selected_profile

        self._profile = load_selected_profile()

    def __call__(self) -> AppSnapshot:
        """Return one internally consistent immutable snapshot.

        Component holders are sampled once per frame.  Previously the source
        queried runtime/capture/research repeatedly while assembling one object,
        allowing the header to say ``FAILED`` while a screen said ``OK``.
        """
        self._frame_cache = {}
        try:
            market = self._market()
            capture = self._capture()
            paper = self._paper()
            research = self._research_snapshot()
            model = self._model_snapshot()
            profitability = self._profitability_snapshot()
            return AppSnapshot(
                lifecycle_state=self._lifecycle_state(),
                market=market,
                capture=capture,
                paper=paper,
                research=research,
                model=model,
                profitability=profitability,
                pipeline=self._pipeline_snapshot(capture, paper, profitability),
                execution=self._execution(),
                components=self._components(),
                capabilities=self._capabilities(),
                challengers=self._challengers(),
                sessions=self._sessions()[0],
                sessions_total=self._sessions()[1],
                next_action=self._next_action(),
                blocker=self._blocker(),
            )
        finally:
            self._frame_cache = None

    def _sessions(self, *, limit: int = 60, ttl_seconds: float = 30.0) -> tuple[tuple, int]:
        """Recorded capture sessions (newest first, bounded) + the full count.

        Disabled (empty) unless a raw_root was provided (the backend supplies it).
        The raw tree is scanned at most once every ``ttl_seconds`` and cached, so a
        per-frame snapshot never rescans hundreds of manifests; a read error keeps
        the last good cache.
        """
        if self._raw_root is None:
            return (), 0
        import time as _time

        now = _time.monotonic()
        if self._sessions_scanned_at >= 0 and now - self._sessions_scanned_at < ttl_seconds:
            return self._sessions_cache, self._sessions_total
        try:
            self._sessions_cache, self._sessions_total = self._build_sessions(limit)
        except Exception:  # noqa: BLE001 - a catalog read must never break the snapshot
            pass
        self._sessions_scanned_at = now
        return self._sessions_cache, self._sessions_total

    def _build_sessions(self, limit: int) -> tuple[tuple, int]:
        from app.gui.view_models import SessionRow
        from app.research.session_catalog import build_catalog

        entries = build_catalog(self._raw_root)  # type: ignore[arg-type]
        paper = self._paper_trades_by_session()
        ordered = sorted(entries, key=lambda entry: entry.session_id, reverse=True)  # newest first
        rows = tuple(
            SessionRow(
                session_id=entry.session_id,
                started_at=(entry.utc_start or "")[:19].replace("T", " "),
                provenance=entry.provenance,
                status=("order-flow eligible" if entry.eligible_for_order_flow_replay
                        else "recording" if entry.active
                        else (entry.reasons[0] if entry.reasons else "ineligible")),
                eligible=entry.eligible_for_order_flow_replay,
                depth_updates=entry.depth_updates,
                market_trades=entry.trades,
                paper_trades=paper.get(entry.session_id, 0),
            )
            for entry in ordered[:limit]
        )
        return rows, len(entries)

    def _paper_trades_by_session(self) -> dict[str, int]:
        """Count real (non-fixture) paper trades per session id, from the ledger."""
        import json
        from collections import Counter

        path = self._ledger_path
        counts: Counter[str] = Counter()
        if path is None or not path.is_file():
            return {}
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict) or row.get("is_synthetic_fixture"):
                    continue
                sid = str(row.get("session_id") or "")
                if sid:
                    counts[sid] += 1
        except (OSError, ValueError):
            return dict(counts)
        return dict(counts)

    def _cached(self, key: str, loader: Callable[[], object | None]) -> object | None:
        """Return one value per snapshot frame, or load directly outside a frame."""
        cache = self._frame_cache
        if cache is None:
            return loader()
        if key not in cache:
            cache[key] = loader()
        return cache[key]

    # -- sections -----------------------------------------------------------------

    def _runtime(self) -> object | None:
        def load() -> object | None:
            if self._controller is None:
                return None
            try:
                return self._controller.snapshot()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001 - never let the GUI die on a bad read
                return None

        return self._cached("runtime", load)

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
        def load() -> object | None:
            if self._receiver_status is None:
                return None
            try:
                return self._receiver_status()
            except Exception:  # noqa: BLE001
                return None

        return self._cached("binding", load)

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
        session_id = self._session_id()
        if session_id != self._history_session_id:
            self._market_history.clear()
            self._history_session_id = session_id
            self._history_timestamp_ns = -1

        timestamp_ns = int(getattr(state, "timestamp_ns", 0) or 0) if state is not None else 0
        price = getattr(state, "mid_price", None)
        cvd = getattr(state, "cumulative_volume_delta", None)
        buy_volume = getattr(state, "executed_buy_volume", None)
        sell_volume = getattr(state, "executed_sell_volume", None)
        event_rate = float(getattr(metrics, "ingress_events_per_second", 0.0)) if metrics else 0.0
        if timestamp_ns > self._history_timestamp_ns and timestamp_ns > 0:
            self._market_history.append(MarketHistoryPoint(
                timestamp_ns=timestamp_ns,
                price=price,
                cumulative_delta=cvd,
                buy_volume=buy_volume,
                sell_volume=sell_volume,
                event_rate_per_second=event_rate,
            ))
            self._history_timestamp_ns = timestamp_ns

        return MarketSnapshot(
            contract=self._contract(),
            last_price=price,
            best_bid=getattr(state, "best_bid", None),
            best_ask=getattr(state, "best_ask", None),
            spread=getattr(state, "spread", None),
            mid_price=price,
            cumulative_delta=cvd,
            buy_volume=buy_volume,
            sell_volume=sell_volume,
            is_delayed=delay > 0,
            source_delay_minutes=delay,
            processing_age_ms=int(getattr(metrics, "end_to_end_lag_ms", 0)) if metrics else None,
            event_rate_per_second=event_rate,
            history=tuple(self._market_history),
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
        def load() -> object | None:
            if self._pipeline is None:
                return None
            try:
                return self._pipeline.snapshot()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                return None

        return self._cached("metrics", load)

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
        cached = self._frame_cache.get("capture") if self._frame_cache is not None else None
        if isinstance(cached, CaptureSnapshot):
            return cached
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
        capture = CaptureSnapshot(
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
        if self._frame_cache is not None:
            self._frame_cache["capture"] = capture
        return capture

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
                SetupCheck(
                    name=c.name,
                    passed=c.passed,
                    observed=c.observed,
                    threshold=c.required,
                    reason=c.message,
                )
                for c in recent[-1].conditions
            )
        from datetime import datetime, timezone

        def _opened_at(ns: int) -> str:
            # The delayed MARKET time the position opened (UTC), exact to the second.
            if not ns:
                return ""
            return datetime.fromtimestamp(ns / 1_000_000_000, timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S")

        rows = tuple(
            TradeRow(
                direction=trade.direction.value, contracts=trade.contracts,
                entry=str(trade.entry_price), exit=str(trade.exit_price),
                net_pnl=f"{trade.net_pnl:+.2f}", close_reason=trade.close_reason.value,
                is_synthetic_fixture=trade.is_synthetic_fixture,
                opened_at=_opened_at(trade.opened_ts_ns),
            )
            for trade in reversed(trades)  # newest first
        )
        # Account figures come from the EXECUTOR's real balance, not the profile
        # defaults: the panel must never show money the simulation does not hold.
        balance = Decimal(status.balance)
        return replace(
            base,
            mode=(
                f"DELAYED PAPER [{getattr(status, 'instrument', 'MNQ')}] — {status.state}"
                + (" [RELAXED]" if getattr(status, "strategy_profile", "canonical") == "relaxed"
                   else "")
                + (" +MOMENTUM" if getattr(status, "momentum_enabled", False) else "")
            ),
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
            fraction=float(progress.fraction),
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
        cached = self._frame_cache.get("research") if self._frame_cache is not None else None
        if isinstance(cached, ResearchSnapshot):
            return cached
        if self._research is None:
            result = ResearchSnapshot()
            if self._frame_cache is not None:
                self._frame_cache["research"] = result
            return result
        try:
            status = self._research.status()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            result = ResearchSnapshot()
            if self._frame_cache is not None:
                self._frame_cache["research"] = result
            return result
        from app.research.auto_research import detect_hardware, gpu_workload_note

        result = ResearchSnapshot(
            state=status.state, active_workers=status.active_workers,
            requested_workers=status.requested_workers, queued_jobs=status.queued_jobs,
            completed_jobs=status.completed_jobs, failed_jobs=status.failed_jobs,
            canonical_trades=status.canonical_trades,
            experimental_trades=status.experimental_trades,
            unique_setups=status.unique_setups, duplicate_overlap=status.duplicate_overlap,
            independent_days=status.independent_days, throttle_reason=status.throttle_reason,
            gpu_note=gpu_workload_note(detect_hardware(), gpu_enabled=False),
        )
        if self._frame_cache is not None:
            self._frame_cache["research"] = result
        return result


    def _model_snapshot(self) -> ModelSnapshot:
        """Return one revalidated registry/feature observation per snapshot frame."""
        cached = self._cached("model", self._load_model_snapshot)
        assert isinstance(cached, ModelSnapshot)
        return cached


    def _load_model_snapshot(self) -> ModelSnapshot:
        """Read and revalidate registry/approval truth without loading a model."""
        try:
            from app.machine_learning.registry import (
                read_model_approval,
                read_registry,
                validate_explicit_approval,
            )

            state = validate_explicit_approval(
                read_registry(self._models_root),
                read_model_approval(self._model_approval_path),
            )
            record = state.record
            feature_state = (
                self._feature_sink.snapshot()  # type: ignore[attr-defined]
                if self._feature_sink is not None
                else None
            )
            loader_state = (
                self._model_loader.snapshot()  # type: ignore[attr-defined]
                if self._model_loader is not None
                else None
            )
            outcome_state = (
                self._outcome_tracker.snapshot()  # type: ignore[attr-defined]
                if self._outcome_tracker is not None
                else None
            )
            return ModelSnapshot(
                registry_state=state.registry_state,
                artifact_id=record.artifact_id if record else "",
                artifact_sha256=record.artifact_sha256 if record else "",
                dataset_id=record.dataset_id if record else "",
                model_type=record.model_type if record else "",
                model_version=record.model_version if record else "",
                eligible_sessions=record.included_sessions if record else 0,
                excluded_sessions=record.excluded_sessions if record else 0,
                validation_state=record.validation_state if record else "NOT_EVALUATED",
                validation_detail=record.validation_detail if record else state.detail,
                oos_predictions=record.oos_predictions if record else 0,
                brier_score=record.brier_score if record else 0.0,
                beats_baseline=record.beats_baseline if record else False,
                approval_state=state.approval_state,
                approval_detail=state.detail,
                feature_parity_state=(
                    FEATURE_PARITY_STATE if record else "NO_REGISTERED_CONTRACT"
                ),
                feature_observation_state=(
                    str(feature_state.state) if feature_state else "UNAVAILABLE"
                ),
                feature_observation_reason=(
                    str(feature_state.reason)
                    if feature_state else "observe-only feature sink is not attached"
                ),
                feature_observations=(
                    int(feature_state.feature_observations) if feature_state else 0
                ),
                feature_gap_resets=int(feature_state.gap_resets) if feature_state else 0,
                feature_session_resets=(
                    int(feature_state.session_resets) if feature_state else 0
                ),
                feature_skipped_events=(
                    int(feature_state.skipped_events) if feature_state else 0
                ),
                runtime_loaded=(
                    bool(loader_state.state == "SCORING") if loader_state else False
                ),
                shadow_predictions=(
                    int(loader_state.shadow_predictions) if loader_state else 0
                ),
                # Structural invariant, not conditional on loader state: this
                # module never reaches strategy, paper, risk, or execution.
                decision_impact="none",
                shadow_loader_state=(
                    str(loader_state.state) if loader_state else "UNLOADED"
                ),
                shadow_loader_reason=(
                    str(loader_state.reason)
                    if loader_state else "observe-only model loader is not attached"
                ),
                outcome_tracker_state=(
                    "TRACKING" if outcome_state is not None else "UNAVAILABLE"
                ),
                outcome_predictions_registered=(
                    int(outcome_state.predictions_registered) if outcome_state else 0
                ),
                outcome_resolved=int(outcome_state.resolved) if outcome_state else 0,
                outcome_resolved_target=(
                    int(outcome_state.resolved_target) if outcome_state else 0
                ),
                outcome_resolved_stop=(
                    int(outcome_state.resolved_stop) if outcome_state else 0
                ),
                outcome_resolved_timeout=(
                    int(outcome_state.resolved_timeout) if outcome_state else 0
                ),
                outcome_dropped_unresolved=(
                    int(outcome_state.dropped_unresolved) if outcome_state else 0
                ),
                outcome_dropped_overflow=(
                    int(outcome_state.dropped_overflow) if outcome_state else 0
                ),
                outcome_gap_tainted_resolutions=(
                    int(outcome_state.gap_tainted_resolutions) if outcome_state else 0
                ),
                outcome_pending=int(outcome_state.pending) if outcome_state else 0,
            )
        except Exception as error:  # noqa: BLE001 - surface corrupt registry truth
            return ModelSnapshot(
                registry_state="INVALID",
                validation_state="INVALID",
                validation_detail=f"{type(error).__name__}: {error}",
                approval_state="INVALID",
                approval_detail="registry or approval data is malformed",
            )

    def _challengers(self) -> tuple[ChallengerSummary, ...]:
        """Return every registered challenger, unfiltered by single-record selection.

        ``_model_snapshot()`` only ever exposes the one record
        ``validate_explicit_approval`` selects (or none, if zero or several are
        unapproved). This lists the full ``read_registry()`` result so the GUI
        can show all registered artifacts side by side - still pure evidence
        display with no runtime effect.
        """
        cached = self._cached("challengers", self._load_challengers)
        assert isinstance(cached, tuple)
        return cached

    def _load_challengers(self) -> tuple[ChallengerSummary, ...]:
        try:
            from app.machine_learning.registry import read_model_approval, read_registry

            records = read_registry(self._models_root)
            approval = read_model_approval(self._model_approval_path)
            summaries = []
            for record in records:
                approval_state, approval_detail = self._challenger_approval(record, approval)
                summaries.append(ChallengerSummary(
                    artifact_id=record.artifact_id,
                    dataset_id=record.dataset_id,
                    model_type=record.model_type,
                    model_version=record.model_version,
                    validation_state=record.validation_state,
                    validation_detail=record.validation_detail,
                    oos_predictions=record.oos_predictions,
                    brier_score=record.brier_score,
                    beats_baseline=record.beats_baseline,
                    included_sessions=record.included_sessions,
                    excluded_sessions=record.excluded_sessions,
                    approval_state=approval_state,
                    approval_detail=approval_detail,
                ))
            return tuple(summaries)
        except Exception:  # noqa: BLE001 - a bad registry must never kill the GUI
            return ()

    @staticmethod
    def _challenger_approval(record: object, approval: object) -> tuple[str, str]:
        """Mirror ``validate_explicit_approval``'s per-record verdict, read-only."""
        approved_artifact_id = approval.approved_artifact_id  # type: ignore[attr-defined]
        approved_sha256 = approval.approved_sha256  # type: ignore[attr-defined]
        if approved_artifact_id is None and approved_sha256 is None:
            return "NOT_APPROVED", "no exact artifact approval is configured"
        if not approved_artifact_id or not approved_sha256:
            return "INVALID", "approval requires both artifact id and SHA"
        if record.artifact_id != approved_artifact_id:  # type: ignore[attr-defined]
            return "NOT_APPROVED", "a different artifact is exactly approved"
        if record.artifact_sha256 != approved_sha256:  # type: ignore[attr-defined]
            return "INVALID", "approved artifact SHA does not match registry"
        if record.validation_state != "PASSED":  # type: ignore[attr-defined]
            return "INVALID", "failed validation cannot be approved"
        if approval.runtime_loading_enabled or approval.shadow_scoring_enabled:  # type: ignore[attr-defined]
            return "INVALID", "runtime loading and shadow scoring are not implemented in this cycle"
        return "APPROVED_RUNTIME_DISABLED", "exact artifact approved; runtime loading remains disabled"

    def _pipeline_snapshot(
        self,
        capture: CaptureSnapshot,
        paper: PaperSnapshot,
        profitability: ProfitabilitySnapshot,
    ) -> PipelineSnapshot:
        """Build the pure nine-stage evidence pipeline from in-memory facts."""
        try:
            from app.research.pipeline_status import PipelineInputs, compute_pipeline

            status = compute_pipeline(PipelineInputs(
                receiver_listening=capture.receiver_listening,
                bookmap_connected=capture.bookmap_connected,
                recording=capture.recording,
                data_stale=(
                    capture.bookmap_connected
                    and (self._market().processing_age_ms or 0) > 5_000
                ),
                current_session_drops=capture.current_session_drops,
                evaluations=paper.evaluations,
                accepted_setups=paper.candidates,
                completed_outcomes=paper.trades,
                validation_passed=profitability.claim_supported,
                validation_stage_label=(
                    profitability.headline
                    if profitability.computed and profitability.headline
                    else "insufficient evidence"
                ),
            ))
            return PipelineSnapshot(
                stages=tuple(PipelineStageRow(
                    key=stage.key,
                    label=stage.label,
                    status=stage.status,
                    blocker=stage.blocker,
                    next_action=stage.next_action,
                    detail=stage.detail,
                ) for stage in status.stages),
                computed=True,
            )
        except Exception as error:  # noqa: BLE001 - status must fail visibly
            return PipelineSnapshot(error=f"{type(error).__name__}: {error}")

    def _execution(self) -> ExecutionSnapshot:
        from pathlib import Path

        from app.execution.live_gate import LiveGateInputs, evaluate_live_gate, read_live_enabled

        decision = evaluate_live_gate(LiveGateInputs(
            live_enabled_in_config=read_live_enabled(Path("config/production_config.yaml")),
            prop_rules_resolved=self._profile.resolved,
        ))
        demo = None
        if self._demo_service is not None:
            try:
                demo = self._demo_service.status()  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001 - a broker fault must not break the GUI
                demo = None
        if demo is None:
            from app.execution.demo_service import credential_checklist

            return ExecutionSnapshot(
                environment="PAPER", connected=False, demo_armed=False, live_armed=False,
                live_blockers=decision.failures, prop_rules_resolved=self._profile.resolved,
                demo_credential_checklist=credential_checklist(),
            )
        return ExecutionSnapshot(
            environment="PAPER", connected=bool(demo.connected), demo_armed=False,
            live_armed=False, live_blockers=decision.failures,
            prop_rules_resolved=self._profile.resolved,
            demo_state=str(demo.state),
            demo_account=str(demo.selected_account),
            demo_balance=str(demo.balance),
            demo_position_net=int(demo.position_net),
            demo_working_orders=int(demo.working_orders),
            demo_contract=str(demo.contract),
            demo_sync_age_seconds=demo.sync_age_seconds,
            demo_orphan_orders=int(demo.orphan_orders),
            demo_reconnects=int(demo.reconnects),
            demo_last_error=str(demo.last_error),
            demo_last_command_result=str(demo.last_command_result),
            demo_credential_checklist=tuple(demo.credential_checklist),
            demo_arming_blockers=tuple(demo.arming_blockers),
        )

    def _components(self) -> tuple[ComponentHealth, ...]:
        capture = self._capture()
        research = self._research_snapshot()
        model = self._model_snapshot()
        model_health = (
            Health.FAIL if model.registry_state == "INVALID"
            else Health.IDLE if model.registry_state == "NOT_REGISTERED"
            else Health.OK if model.validation_state == "PASSED"
            else Health.WARN
        )
        model_detail = (
            model.validation_detail if model.registry_state != "NOT_REGISTERED"
            else "no offline challenger registered"
        )
        if capture.current_session_drops:
            return (
                ComponentHealth("capture", Health.INVALIDATED,
                                f"{capture.current_session_drops:,} source event(s) lost"),
                ComponentHealth("recorder", Health.INVALIDATED,
                                "process alive, but this segment is not research-eligible"),
                ComponentHealth("paper", Health.PAUSED,
                                "new entries blocked because causal input is incomplete"),
                ComponentHealth("research", Health.PAUSED,
                                "invalid active segment cannot enter canonical research"),
                ComponentHealth("model", model_health, model_detail),
                ComponentHealth("live", Health.LOCKED, "locked by the validation gate"),
            )
        recorder_health = Health.OK if capture.recording else Health.IDLE
        paper_health = Health.IDLE
        paper_detail = "awaiting a qualifying setup"
        if capture.recording and capture.bookmap_connected:
            paper_health = Health.WARMING_UP
            paper_detail = "causal evaluation active / warming up"
        if research.throttle_reason:
            research_health = Health.THROTTLED
            research_detail = research.throttle_reason
        else:
            research_health = Health.OK if research.active_workers else Health.IDLE
            research_detail = research.state
        return (
            ComponentHealth("capture", capture.health,
                            "recording" if capture.recording else "waiting for Bookmap"),
            ComponentHealth("recorder", recorder_health,
                            f"{capture.persisted_per_second:,.0f} events/s persisted"),
            ComponentHealth("paper", paper_health, paper_detail),
            ComponentHealth("research", research_health, research_detail),
            ComponentHealth("model", model_health, model_detail),
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
