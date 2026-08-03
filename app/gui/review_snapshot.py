"""Deterministic, explicitly synthetic-free snapshots for GUI visual review.

Three data states are provided, matching what the running application can
honestly show at different points in its life -- never a fabricated trade
outcome, only states the real pipeline can occupy:

- :func:`build_empty_snapshot` -- freshly started, nothing observed yet
  (every field at its dataclass default).
- :func:`build_partial_snapshot` -- receiver listening and recording, but
  no setup has evaluated yet and no model/research evidence exists.
- :func:`build_review_snapshot` -- a fully populated mid-session state,
  including the offline model-registry evidence a challenger produces.
"""

from __future__ import annotations

from decimal import Decimal

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
    ProgressGateRow,
    ResearchSnapshot,
    SetupCheck,
)


def build_empty_snapshot() -> AppSnapshot:
    """Return the honest just-started state: every field at its default."""
    return AppSnapshot()


def build_partial_snapshot() -> AppSnapshot:
    """Return a mid-session state: recording underway, nothing evaluated yet.

    No setups have qualified, no research has run, and no model is
    registered -- this is what the GUI looks like between "Bookmap
    connected" and the first evaluated setup.
    """
    return AppSnapshot(
        lifecycle_state="RECORDING",
        market=MarketSnapshot(
            contract="MNQU6", last_price=Decimal("29250.00"),
            best_bid=Decimal("29249.75"), best_ask=Decimal("29250.25"),
            spread=Decimal("0.50"), mid_price=Decimal("29250.00"),
            cumulative_delta=Decimal("-4"), buy_volume=Decimal("62"),
            sell_volume=Decimal("66"), is_delayed=True,
            source_delay_minutes=15, processing_age_ms=11,
            event_rate_per_second=340.5,
        ),
        capture=CaptureSnapshot(
            receiver_listening=True, bookmap_connected=True, recording=True,
            session_id="session_20260728T093000Z", current_session_drops=0,
            lifetime_bridge_drops=1_031_435, intake_occupancy=2,
            intake_capacity=10_000, recorder_occupancy=6,
            recorder_capacity=50_000, flush_latency_ms=0.31,
            persisted_per_second=340.5, analysis_offered=900,
            analysis_processed=900, analysis_skipped=0, analysis_lag_ms=1.1,
        ),
        paper=PaperSnapshot(
            profile_name="LucidFlex 25K Evaluation", balance=Decimal("25000"),
            starting_balance=Decimal("25000"), profit_target=Decimal("1250"),
            target_progress=Decimal("0"), drawdown_room=Decimal("1000"),
            trades=0, evaluations=0, risk_remaining=Decimal("250"),
        ),
        research=ResearchSnapshot(state="idle", requested_workers=15),
        execution=ExecutionSnapshot(
            environment="PAPER",
            live_blockers=("live_enabled is not true", "prop rules unresolved"),
            prop_rules_resolved=False,
        ),
        components=(
            ComponentHealth("capture", Health.OK, "recording"),
            ComponentHealth("recorder", Health.OK, "flushing"),
            ComponentHealth("paper", Health.IDLE, "no evaluations yet"),
            ComponentHealth("research", Health.IDLE, "no pending jobs"),
            ComponentHealth("model", Health.IDLE, "no runtime model"),
        ),
        capabilities=(
            ("Aggregated depth", Capability.AVAILABLE, "provider supplies depth updates"),
            ("Aggressor side", Capability.AVAILABLE, "trade aggressor side observed"),
            ("MBO / native iceberg", Capability.UNAVAILABLE, "feed does not expose order IDs"),
            ("Liquidity blocks", Capability.HEURISTIC, "derived from depth persistence"),
        ),
        next_action="Recording is healthy; wait for a qualifying setup to evaluate.",
    )


def build_review_snapshot() -> AppSnapshot:
    """Return populated operational sample data with no fabricated trade outcomes."""
    history = tuple(
        MarketHistoryPoint(
            timestamp_ns=1_000_000_000 * index,
            price=Decimal("29266.00") + Decimal(index) / Decimal("8"),
            cumulative_delta=Decimal(index * 18 - 160),
            buy_volume=Decimal(900 + index * 11),
            sell_volume=Decimal(1050 + index * 8),
            event_rate_per_second=1500.0 + index * 6,
        )
        for index in range(20)
    )
    stages = tuple(
        PipelineStageRow(
            key=f"stage_{index}", label=label, status=status,
            detail=detail, next_action=next_action,
        )
        for index, (label, status, detail, next_action) in enumerate((
            ("Receiver", "ready", "listening on localhost", "keep running"),
            ("Bookmap", "ready", "forwarder connected", "keep chart open"),
            ("Recording", "running", "zero current-session loss", "complete session"),
            ("Session finalized", "blocked", "active recording", "wait for finalization"),
            ("Episodes", "not_started", "requires finalized session", "finalize cleanly"),
            ("Outcomes", "not_started", "no completed setup outcomes", "collect evidence"),
            ("Validation", "blocked", "insufficient evidence", "collect independent days"),
            ("DEMO gate", "locked", "read-only build", "separate authorization required"),
            ("LIVE gate", "locked", "LIVE false", "not authorized"),
        ))
    )
    return AppSnapshot(
        lifecycle_state="RECORDING",
        market=MarketSnapshot(
            contract="MNQU6", last_price=Decimal("29268.50"),
            best_bid=Decimal("29268.25"), best_ask=Decimal("29268.75"),
            spread=Decimal("0.50"), mid_price=Decimal("29268.50"),
            cumulative_delta=Decimal("-142"), buy_volume=Decimal("1204"),
            sell_volume=Decimal("1346"), is_delayed=True,
            source_delay_minutes=15, processing_age_ms=8,
            event_rate_per_second=1651.8, history=history,
        ),
        capture=CaptureSnapshot(
            receiver_listening=True, bookmap_connected=True, recording=True,
            session_id="session_20260717T140000Z", current_session_drops=0,
            lifetime_bridge_drops=1_031_435, intake_occupancy=12,
            intake_capacity=10_000, recorder_occupancy=127,
            recorder_capacity=50_000, flush_latency_ms=0.39,
            persisted_per_second=1651.8, analysis_offered=125_000,
            analysis_processed=124_993, analysis_skipped=7, analysis_lag_ms=4.2,
        ),
        paper=PaperSnapshot(
            profile_name="LucidFlex 25K Evaluation", balance=Decimal("25000"),
            starting_balance=Decimal("25000"), profit_target=Decimal("1250"),
            target_progress=Decimal("0"), drawdown_room=Decimal("1000"),
            trades=0, evaluations=20,
            top_rejections=(("durable_defending_block", 20), ("cvd_supports_direction", 20)),
            setup_checks=(
                SetupCheck("durable_defending_block", False, "0 blocks", ">=1 block", "no durable block"),
                SetupCheck("opening_observation_complete", True, "complete", "complete", ""),
            ),
            risk_remaining=Decimal("250"),
        ),
        research=ResearchSnapshot(
            state="idle", active_workers=0, requested_workers=15,
            canonical_trades=0, experimental_trades=0,
            unique_setups=0, independent_days=0,
            gpu_note="NVIDIA GPU present; deterministic replay remains CPU/I-O bound.",
        ),
        pipeline=PipelineSnapshot(stages=stages, computed=True),
        execution=ExecutionSnapshot(
            environment="PAPER",
            live_blockers=("live_enabled is not true", "prop rules unresolved"),
            prop_rules_resolved=False,
        ),
        model=ModelSnapshot(
            registry_state="CHALLENGER", artifact_id="challenger-abc",
            artifact_sha256="a" * 64, dataset_id="dataset-abc",
            model_type="logistic_regression", model_version="0.1.0",
            eligible_sessions=4, excluded_sessions=2, validation_state="PASSED",
            validation_detail="walk-forward gate passed", oos_predictions=120,
            brier_score=0.21, beats_baseline=True,
            approval_state="APPROVED_RUNTIME_DISABLED",
            approval_detail="exact artifact approved; runtime loading remains disabled",
            feature_parity_state="SHARED_BUILDER_RUNTIME_DISCONNECTED",
            feature_observation_state="OBSERVING",
            feature_observation_reason="feature vectors observed in memory; no model loaded or scored",
            feature_observations=14, feature_gap_resets=2,
            feature_session_resets=3, feature_skipped_events=19,
            runtime_loaded=False, shadow_predictions=0, decision_impact="none",
            shadow_loader_state="UNLOADED",
            shadow_loader_reason="observe-only model loader is not attached",
        ),
        challengers=(
            ChallengerSummary(
                artifact_id="challenger-abc", dataset_id="dataset-abc",
                model_type="logistic_regression", model_version="0.1.0",
                validation_state="PASSED", validation_detail="walk-forward gate passed",
                oos_predictions=120, brier_score=0.21, beats_baseline=True,
                included_sessions=4, excluded_sessions=2,
                approval_state="APPROVED_RUNTIME_DISABLED",
                approval_detail="exact artifact approved; runtime loading remains disabled",
            ),
            ChallengerSummary(
                artifact_id="challenger-xyz", dataset_id="dataset-xyz",
                model_type="gradient_boosting", model_version="0.2.0",
                validation_state="FAILED", validation_detail="brier worse than baseline",
                oos_predictions=80, brier_score=0.31, beats_baseline=False,
                included_sessions=3, excluded_sessions=1,
                approval_state="NOT_APPROVED",
                approval_detail="a different artifact is exactly approved",
            ),
        ),
        profitability=ProfitabilitySnapshot(
            fraction=0.067, computed=True, claim_supported=False,
            headline="Data-collection stage: zero setups have completed on real data yet.",
            gates=(
                ProgressGateRow(label="Data capture healthy", status="passed",
                                observed="1 clean session", threshold=">=1 clean session"),
                ProgressGateRow(label="Minimum completed-setup sample", status="blocked",
                                observed="0 completed", threshold=">= 100 completed setups"),
            ),
        ),
        components=(
            ComponentHealth("capture", Health.OK, "recording"),
            ComponentHealth("recorder", Health.OK, "flushing"),
            ComponentHealth("paper", Health.IDLE, "awaiting setup"),
            ComponentHealth("research", Health.IDLE, "no pending jobs"),
            ComponentHealth("model", Health.IDLE, "no runtime model"),
        ),
        capabilities=(
            ("Aggregated depth", Capability.AVAILABLE, "provider supplies depth updates"),
            ("Aggressor side", Capability.AVAILABLE, "trade aggressor side observed"),
            ("MBO / native iceberg", Capability.UNAVAILABLE, "feed does not expose order IDs"),
            ("Liquidity blocks", Capability.HEURISTIC, "derived from depth persistence"),
        ),
        next_action="Recording is healthy; let it capture complete sessions.",
    )
