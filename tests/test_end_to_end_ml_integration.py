"""Real shadow-ML mechanics integration using synthetic fixtures only.

The generated sessions and event tape are deliberately synthetic and exist only
to prove that a genuinely walk-forward-validated artifact can be loaded, scored,
and can affect a recorded paper-shadow decision.  This is not evidence of market
edge, trading performance, or profitability.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import yaml

from app.database.recorder import MarketSessionRecorder
from app.machine_learning.challenger_pipeline import (
    DatasetBuildConfig,
    build_validated_challenger,
)
from app.machine_learning.feature_contract import ObserveOnlyFeatureSink
from app.machine_learning.session_training import SessionTrainingConfig
from app.machine_learning.shadow_predictor import ObserveOnlyModelLoader, PredictionJournal
from app.market.state import MarketState
from app.paper.streaming_engine import (
    DECISION_SOURCE_BLENDED,
    DECISION_SOURCE_ML,
    DelayedPaperEngine,
)
from app.research.episode_builder import EpisodeConfig

UTC = timezone.utc
SEC = 1_000_000_000
_REQUIRED_CAPABILITIES = "aggregated_depth,trades,aggressor_side,source_timestamps"


def _record_synthetic_training_session(raw_root: Path, day: int) -> None:
    """Record one synthetic mechanics fixture through the real session recorder."""
    start = datetime(2026, 7, day, 14, 30, tzinfo=UTC)
    recorder = MarketSessionRecorder(
        root_dir=raw_root,
        session_start_utc=start,
        compact_on_finalize=False,
    )
    recorder.record_control_event(
        {"type": "delayed_mode", "timestamp_ns": 1, "delay_minutes": 15}
    )
    recorder.record_control_event(
        {
            "type": "connected",
            "timestamp_ns": 2,
            "source_mode": "delayed",
            "protocol_version": "1.2",
            "provider": "pytest-synthetic-mechanics",
            "stream_id": f"synthetic-stream-{day}",
            "connection_id": f"synthetic-connection-{day}",
            "session_id": f"synthetic-bridge-session-{day}",
            "capabilities": _REQUIRED_CAPABILITIES,
            "handshake_accepted": True,
            "dropped_message_count": 0,
            "stream_sequence": 1,
        }
    )
    base_ns = int(start.timestamp() * SEC)
    sequence = 1

    def depth(timestamp_ns: int, side: str, price: str, previous: str, new: str) -> None:
        nonlocal sequence
        sequence += 1
        recorder.record(
            {
                "type": "depth_update",
                "timestamp": timestamp_ns,
                "symbol": "MNQ",
                "side": side,
                "price": price,
                "previous_size": previous,
                "new_size": new,
                "stream_sequence": sequence,
            }
        )

    def trade(timestamp_ns: int, price: str, aggressor_side: str) -> None:
        nonlocal sequence
        sequence += 1
        recorder.record(
            {
                "type": "trade",
                "timestamp_ns": timestamp_ns,
                "price": price,
                "size": "1",
                "aggressor_side": aggressor_side,
                "instrument": "MNQ",
                "sequence_id": sequence,
                "stream_sequence": sequence,
            }
        )

    depth(base_ns, "bid", "100", "0", "100")
    depth(base_ns + 1, "ask", "100.25", "0", "100")
    previous_bid = previous_ask = "100"
    for index in range(36):
        timestamp_ns = base_ns + (index + 1) * SEC
        upward_move = index % 2 == 0
        new_bid, new_ask = ("900", "50") if upward_move else ("50", "900")
        depth(timestamp_ns - 2, "bid", "100", previous_bid, new_bid)
        depth(timestamp_ns - 1, "ask", "100.25", previous_ask, new_ask)
        previous_bid, previous_ask = new_bid, new_ask
        side = "buy" if upward_move else "sell"
        trade(timestamp_ns, "100.125", side)
        trade(timestamp_ns + 1, "100.625" if upward_move else "99.625", side)

    recorder.finalize(clean_shutdown=True)


def _training_config() -> SessionTrainingConfig:
    return SessionTrainingConfig(
        target_ticks=Decimal("2"),
        stop_ticks=Decimal("2"),
        horizon_seconds=5.0,
        sample_interval_seconds=1.0,
        window_span_seconds=2.0,
        sample_interval_ms=0.0,
        warmup_seconds=0.5,
        directions=("long",),
    )


def _price(offset: str) -> str:
    return f"{Decimal(offset) + Decimal('29400'):.2f}"


def _depth_event(
    timestamp_ns: int,
    side: str,
    price: str,
    previous: str,
    new: str,
) -> dict[str, object]:
    return {
        "type": "depth_update",
        "timestamp": timestamp_ns,
        "symbol": "MNQ",
        "side": side,
        "price": price,
        "previous_size": previous,
        "new_size": new,
    }


def _trade_event(
    timestamp_ns: int,
    price: str,
    size: str,
    side: str,
    sequence: int,
) -> dict[str, object]:
    return {
        "type": "trade",
        "timestamp_ns": timestamp_ns,
        "instrument": "MNQ",
        "price": price,
        "size": size,
        "aggressor_side": side,
        "sequence_id": sequence,
    }


def _synthetic_absorption_events() -> list[dict[str, object]]:
    """Synthetic accepted-long mechanics tape; not a market-performance sample."""
    base_ns = int(datetime(2026, 7, 20, 14, 5, tzinfo=UTC).timestamp()) * SEC
    return [
        _depth_event(base_ns, "bid", _price("100.00"), "0", "120"),
        _depth_event(base_ns, "bid", _price("99.75"), "0", "70"),
        _depth_event(base_ns, "ask", _price("100.25"), "0", "100"),
        _depth_event(base_ns, "ask", _price("102.00"), "0", "120"),
        _trade_event(base_ns + SEC, _price("100.00"), "420", "sell", 1),
        _depth_event(base_ns + SEC, "bid", _price("100.00"), "120", "20"),
        _depth_event(base_ns + 2 * SEC, "bid", _price("100.00"), "20", "135"),
        _depth_event(base_ns + 3 * SEC, "ask", _price("100.25"), "100", "10"),
        _depth_event(base_ns + 4 * SEC, "ask", _price("100.25"), "10", "0"),
        _depth_event(base_ns + 4 * SEC, "ask", _price("100.75"), "0", "40"),
        _depth_event(base_ns + 4 * SEC, "bid", _price("100.50"), "0", "70"),
        _depth_event(base_ns + 4 * SEC, "bid", _price("100.00"), "135", "130"),
        _trade_event(base_ns + 5 * SEC, _price("100.75"), "80", "buy", 2),
        _depth_event(base_ns + 5 * SEC, "bid", _price("100.00"), "130", "140"),
        _trade_event(base_ns + 6 * SEC, _price("100.75"), "70", "buy", 3),
    ]


def test_real_validated_model_changes_shadow_decision_and_journals_identity(
    tmp_path: Path,
) -> None:
    """Prove synthetic mechanics only: real model output affects a shadow decision.

    The model is trained from causally labeled, recorder-produced synthetic sessions
    and passes the unmodified walk-forward gates.  The assertion proves integration
    mechanics, not market validity, future returns, or trading profitability.
    """
    raw_root = tmp_path / "raw"
    models_root = tmp_path / "models"
    for day in range(13, 19):
        _record_synthetic_training_session(raw_root, day)

    challenger = build_validated_challenger(
        raw_root,
        models_root,
        config=DatasetBuildConfig(_training_config()),
        target_ticks=2.0,
        stop_ticks=2.0,
        cost_ticks=0.1,
        min_train_days=2,
        min_oos_predictions=20,
    )
    assert challenger["status"] == "challenger"
    assert all(challenger["validation"]["gates"].values())

    approval_path = tmp_path / "synthetic_model_approval.yaml"
    approval_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "approved_artifact_id": challenger["artifact_id"],
                "approved_sha256": challenger["artifact_sha256"],
                "runtime_loading_enabled": False,
                "shadow_scoring_enabled": False,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    journal_path = tmp_path / "synthetic_predictions.jsonl"
    journal = PredictionJournal(journal_path, fsync=False)
    loader = ObserveOnlyModelLoader(
        models_root=models_root,
        model_approval_path=approval_path,
        journal=journal,
        refresh_interval_seconds=3600.0,
        current_date_provider=lambda: datetime(2026, 7, 19, tzinfo=UTC).date(),
    )
    assert loader.snapshot().state == "SCORING"

    session_id = "synthetic-e2e-shadow-session"
    feature_sink = ObserveOnlyFeatureSink(
        config=_training_config(),
        prediction_sink=loader,
    )
    feature_sink.bind_session(session_id)
    engine = DelayedPaperEngine(
        config=EpisodeConfig(
            warmup_events=1,
            decision_stride=1,
            warmup_span_seconds=0,
            evaluation_interval_ms=0,
            depth_sample_interval_ms=0,
            ml_decision_policy_enabled=True,
        ),
        is_synthetic_fixture=True,
        model_loader=loader,
    )
    engine.bind_session(session_id, "MNQU6")

    state = MarketState()
    for event in _synthetic_absorption_events():
        state = state.update(event)
        feature_sink.ingest(event, state)
        engine.ingest(event, state)

    influenced = [
        record
        for record in engine.recent_evaluations(limit=500)
        if record.decision_source in {DECISION_SOURCE_ML, DECISION_SOURCE_BLENDED}
    ]
    assert influenced, "a real scored prediction must affect a recorded shadow decision"
    decision = influenced[-1]
    assert decision.model_prediction_id
    assert decision.model_version == challenger["artifact_id"]
    assert decision.model_artifact_sha256 == challenger["artifact_sha256"]

    recovered = PredictionJournal.recover(journal_path)
    matching = [
        record
        for record in recovered.records
        if record["prediction_id"] == decision.model_prediction_id
    ]
    assert len(matching) == 1
    assert matching[0]["artifact_id"] == decision.model_version
    assert matching[0]["artifact_sha256"] == decision.model_artifact_sha256
    assert matching[0]["session_id"] == session_id
    assert matching[0]["direction"] == decision.direction
