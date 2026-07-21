"""Per-session causal triple-barrier labeling and training."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.database.recorder import MarketSessionRecorder
from app.machine_learning.session_training import (
    FEATURE_COLUMNS,
    SessionTrainingConfig,
    build_session_training_rows,
    train_session_model,
)
from app.machine_learning.train import FEATURE_COLUMNS as TRAIN_FEATURE_COLUMNS


def _oscillating_session(raw_root: Path) -> Path:
    """Record a session whose price sweeps up and down so both barriers hit."""
    rec = MarketSessionRecorder(root_dir=raw_root,
                                session_start_utc=datetime(2026, 7, 15, 14, 30, tzinfo=UTC))
    rec.record_control_event({"type": "delayed_mode", "timestamp_ns": 1, "delay_minutes": 15})
    base = 1_752_590_000_000_000_000
    seq = 0
    # ~12 minutes, a triangle wave of +-12 ticks; both long and short reach
    # their +12/-8 barriers at different points -> mixed labels.
    for i in range(1440):  # 0.5s steps -> 720s
        phase = i % 120
        ticks = phase if phase <= 60 else 120 - phase  # 0..60..0 ramp
        price = Decimal("29500.00") + Decimal("0.25") * ticks
        ts = base + i * 500_000_000
        side = "bid" if i % 2 else "ask"
        rec.record({"type": "depth_update", "timestamp": ts, "symbol": "MNQ", "side": side,
                    "price": f"{price:.2f}", "previous_size": "0", "new_size": str(i % 30 + 5)})
        seq += 1
        rec.record({"timestamp_ns": ts + 1, "price": f"{price:.2f}", "size": "2",
                    "aggressor_side": "buy" if i % 2 else "sell", "instrument": "MNQ",
                    "sequence_id": seq})
    rec.finalize(clean_shutdown=True)
    return next(raw_root.rglob("session_manifest.json")).parent


def _config() -> SessionTrainingConfig:
    return SessionTrainingConfig(target_ticks=Decimal("6"), stop_ticks=Decimal("6"),
                                 horizon_seconds=60.0, sample_interval_seconds=10.0,
                                 warmup_seconds=20.0)


def test_feature_columns_match_the_training_contract() -> None:
    assert FEATURE_COLUMNS == TRAIN_FEATURE_COLUMNS


def test_triple_barrier_produces_both_label_classes(tmp_path: Path) -> None:
    session = _oscillating_session(tmp_path / "raw")
    rows, summary = build_session_training_rows(session, config=_config())

    assert summary.rows >= 2
    assert summary.wins >= 1 and summary.losses >= 1, "both barriers must be reached"
    assert summary.trainable is True
    labels = {int(r["label"]) for r in rows}
    assert labels == {0, 1}
    # Every row carries the full training contract.
    for row in rows:
        for column in (*FEATURE_COLUMNS, "timestamp_ns", "label"):
            assert column in row


def test_incomplete_horizons_are_dropped_not_guessed(tmp_path: Path) -> None:
    # A session that goes FLAT after a while: samples taken during the flat
    # tail never reach a barrier, so with a long horizon they cannot complete
    # before session end and are DROPPED, never assigned a fabricated outcome.
    raw = tmp_path / "raw"
    rec = MarketSessionRecorder(root_dir=raw,
                                session_start_utc=datetime(2026, 7, 15, 14, 30, tzinfo=UTC))
    rec.record_control_event({"type": "delayed_mode", "timestamp_ns": 1, "delay_minutes": 15})
    base = 1_752_590_000_000_000_000
    for i in range(600):
        ticks = min(30, i // 5) if i < 300 else 30  # rise, then dead-flat plateau
        price = Decimal("29500.00") + Decimal("0.25") * ticks
        ts = base + i * 500_000_000
        rec.record({"type": "depth_update", "timestamp": ts, "symbol": "MNQ",
                    "side": "bid" if i % 2 else "ask", "price": f"{price:.2f}",
                    "previous_size": "0", "new_size": "10"})
        rec.record({"timestamp_ns": ts + 1, "price": f"{price:.2f}", "size": "2",
                    "aggressor_side": "buy", "instrument": "MNQ", "sequence_id": i + 1})
    rec.finalize(clean_shutdown=True)
    session = next(raw.rglob("session_manifest.json")).parent

    cfg = SessionTrainingConfig(target_ticks=Decimal("6"), stop_ticks=Decimal("6"),
                                horizon_seconds=100_000.0, sample_interval_seconds=10.0,
                                warmup_seconds=20.0)
    rows, summary = build_session_training_rows(session, config=cfg)
    assert summary.dropped_incomplete > 0, "flat-tail samples must be dropped, not guessed"
    assert summary.rows == len(rows)


def test_train_session_writes_models_and_honest_report(tmp_path: Path) -> None:
    session = _oscillating_session(tmp_path / "raw")
    out = tmp_path / "models"
    summary, report = train_session_model(session, out, config=_config(), version="0.1.0")

    assert summary.trainable is True
    assert report["trained"] is True
    session_out = out / summary.session_id
    assert (session_out / "dataset.jsonl").is_file()
    assert (session_out / "report.json").is_file()
    # Two versioned model artifacts exist.
    assert (session_out / "logistic_regression_model_v0.1.0.joblib").is_file()
    assert (session_out / "xgboost_model_v0.1.0.joblib").is_file()
    # The report never claims profitability and states its narrow scope.
    text = (session_out / "report.json").read_text(encoding="utf-8").lower()
    assert "profitability" in text and "no profitability claim" in text
    assert "in-sample" in text


def test_unlabelable_session_reports_honestly_without_models(tmp_path: Path) -> None:
    """A flat session reaches no barriers: a report, but no model, no fabrication."""
    raw = tmp_path / "raw"
    rec = MarketSessionRecorder(root_dir=raw,
                                session_start_utc=datetime(2026, 7, 15, 14, 30, tzinfo=UTC))
    rec.record_control_event({"type": "delayed_mode", "timestamp_ns": 1, "delay_minutes": 15})
    base = 1_752_590_000_000_000_000
    for i in range(400):  # dead-flat price -> no barrier ever hit
        ts = base + i * 500_000_000
        rec.record({"type": "depth_update", "timestamp": ts, "symbol": "MNQ",
                    "side": "bid" if i % 2 else "ask", "price": "29500.00",
                    "previous_size": "0", "new_size": "10"})
        rec.record({"timestamp_ns": ts + 1, "price": "29500.00", "size": "1",
                    "aggressor_side": "buy", "instrument": "MNQ", "sequence_id": i + 1})
    rec.finalize(clean_shutdown=True)
    session = next(raw.rglob("session_manifest.json")).parent

    out = tmp_path / "models"
    summary, report = train_session_model(session, out, config=_config(), version="0.1.0")
    assert report["trained"] is False
    assert summary.trainable is False
    session_out = out / summary.session_id
    assert (session_out / "report.json").is_file()
    assert not list(session_out.glob("*.joblib")), "no model when data is not trainable"


def test_config_validation_rejects_impossible_values() -> None:
    import pytest

    with pytest.raises(ValueError):
        SessionTrainingConfig(target_ticks=Decimal("0")).validate()
    with pytest.raises(ValueError):
        SessionTrainingConfig(horizon_seconds=0).validate()
