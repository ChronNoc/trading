"""Observe-only analysis-thread feature construction safety contract."""

from __future__ import annotations

import ast
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.machine_learning.feature_contract import ObserveOnlyFeatureSink
from app.machine_learning.session_training import SessionTrainingConfig
from app.market.analysis_feed import AnalysisFeed
from app.market.state import MarketState


def _config() -> SessionTrainingConfig:
    return SessionTrainingConfig(
        target_ticks=Decimal("6"),
        stop_ticks=Decimal("6"),
        warmup_seconds=2.0,
        sample_interval_seconds=1.0,
        window_span_seconds=10.0,
        sample_interval_ms=0.0,
    )


def _states(base_ns: int, count: int) -> tuple[MarketState, ...]:
    state = MarketState()
    states: list[MarketState] = []
    for index in range(count):
        ts = base_ns + index * 1_000_000_000
        price = Decimal("29500.00") + Decimal(index % 3) * Decimal("0.25")
        state = state.update({
            "type": "depth_update", "timestamp": ts, "symbol": "MNQ",
            "side": "bid", "price": str(price), "previous_size": "0", "new_size": "10",
        })
        state = state.update({
            "type": "depth_update", "timestamp": ts, "symbol": "MNQ",
            "side": "ask", "price": str(price + Decimal("0.25")),
            "previous_size": "0", "new_size": "10",
        })
        state = state.update({
            "timestamp_ns": ts, "price": str(price), "size": "1",
            "aggressor_side": "buy" if index % 2 else "sell",
            "instrument": "MNQ", "sequence_id": index + 1,
        })
        states.append(state)
    return tuple(states)


def test_observer_warms_then_builds_both_direction_vectors() -> None:
    sink = ObserveOnlyFeatureSink(config=_config())
    sink.bind_session("session-one")
    base = int(datetime(2026, 7, 23, 14, 30, tzinfo=UTC).timestamp() * 1e9)

    for index, state in enumerate(_states(base, 5)):
        sink.ingest({"index": index}, state)

    snapshot = sink.snapshot()
    assert snapshot.state == "OBSERVING"
    assert snapshot.session_id == "session-one"
    assert snapshot.feature_observations == 6  # three cadence ticks, long + short
    assert snapshot.decision_impact == "none"


def test_gap_reset_discards_prefix_and_requires_full_rewarm() -> None:
    sink = ObserveOnlyFeatureSink(config=_config())
    sink.bind_session("session-one")
    base = int(datetime(2026, 7, 23, 14, 30, tzinfo=UTC).timestamp() * 1e9)
    states = _states(base, 8)
    for state in states[:4]:
        sink.ingest({}, state)
    assert sink.snapshot().feature_observations > 0

    sink.notify_causality_gap(17)
    sink.ingest({}, states[4])
    snapshot = sink.snapshot()
    assert snapshot.state == "WARMING"
    assert snapshot.gap_resets == 1
    assert snapshot.skipped_events == 17

    for state in states[5:]:
        sink.ingest({}, state)
    assert sink.snapshot().state == "OBSERVING"


def test_session_boundary_resets_prefix_without_resetting_evidence_counters() -> None:
    sink = ObserveOnlyFeatureSink(config=_config())
    sink.bind_session("session-one")
    base = int(datetime(2026, 7, 23, 14, 30, tzinfo=UTC).timestamp() * 1e9)
    for state in _states(base, 4):
        sink.ingest({}, state)
    observations = sink.snapshot().feature_observations

    sink.bind_session("session-two")
    snapshot = sink.snapshot()
    assert snapshot.state == "WARMING"
    assert snapshot.session_id == "session-two"
    assert snapshot.feature_observations == observations
    assert snapshot.session_resets == 2


def test_observer_runs_on_analysis_thread_and_offer_stays_non_blocking() -> None:
    sink = ObserveOnlyFeatureSink(config=_config())
    sink.bind_session("session-one")
    feed = AnalysisFeed(capacity=100)
    feed.add_sink(sink.ingest)
    feed.add_gap_sink(sink.notify_causality_gap)
    feed.start()
    base = int(datetime(2026, 7, 23, 14, 30, tzinfo=UTC).timestamp() * 1e9)
    states = _states(base, 5)
    try:
        started = time.perf_counter()
        for state in states:
            assert feed.offer({}, state) is True
        assert time.perf_counter() - started < 0.5
        deadline = time.monotonic() + 3
        while feed.metrics().processed < len(states) and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        feed.stop()

    assert sink.snapshot().thread_name == "mnq-analysis-feed"


def test_observer_has_no_model_loading_disk_or_execution_imports() -> None:
    source = Path("app/machine_learning/feature_contract.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    banned = ("joblib", "sklearn", "xgboost", "app.execution", "broker", "tradovate")
    assert not [name for name in imports if any(item in name.lower() for item in banned)]
    assert "predict_proba" not in source
    assert "would_submit" not in source


def test_production_wiring_uses_every_event_sink_and_gap_callback() -> None:
    source = Path("tools/start_assistant.py").read_text(encoding="utf-8")
    assert "feed.add_sink(feature_sink.ingest)" in source
    assert "feed.add_gap_sink(feature_sink.notify_causality_gap)" in source
    assert "feature_sink.bind_session(recorder.session_id)" in source
