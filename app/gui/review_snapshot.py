"""Deterministic, explicitly synthetic-free snapshot for GUI visual review."""

from __future__ import annotations

from decimal import Decimal

from app.gui.view_models import (
    AppSnapshot,
    Capability,
    CaptureSnapshot,
    ComponentHealth,
    ExecutionSnapshot,
    Health,
    MarketHistoryPoint,
    MarketSnapshot,
    PaperSnapshot,
    PipelineSnapshot,
    PipelineStageRow,
    ResearchSnapshot,
    SetupCheck,
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
