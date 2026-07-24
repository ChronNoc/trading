"""Golden offline/online parity tests for the shared causal feature contract."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.machine_learning.feature_contract import (
    FEATURE_COLUMNS,
    FEATURE_CONTRACT_VERSION,
    CausalFeaturePipeline,
    feature_contract_descriptor,
    feature_contract_sha256,
    validate_feature_record,
)
from app.market.state import MarketState

TICK_SIZE = Decimal("0.25")
BASE_TIMESTAMP_NS = 1_784_836_800_000_000_000  # 2026-07-23 20:00:00 UTC / 16:00 EDT
WINDOW_SPAN_SECONDS = 180.0
SAMPLE_INTERVAL_MS = 250.0
STOP_TICKS = Decimal("8")
TARGET_TICKS = Decimal("12")


def test_identical_event_prefix_is_byte_equivalent_offline_and_online() -> None:
    states = _states_from_prefix(_event_prefix())

    offline = CausalFeaturePipeline(
        window_span_seconds=WINDOW_SPAN_SECONDS,
        sample_interval_ms=SAMPLE_INTERVAL_MS,
        tick_size=TICK_SIZE,
    )
    online = CausalFeaturePipeline(
        window_span_seconds=WINDOW_SPAN_SECONDS,
        sample_interval_ms=SAMPLE_INTERVAL_MS,
        tick_size=TICK_SIZE,
    )
    for event_index, state in enumerate(states, start=1):
        offline.observe(state, event_index=event_index)
        online.observe(state, event_index=event_index)

    offline_bytes = offline.feature_vector(
        direction="long",
        stop_distance=STOP_TICKS,
        target_distance=TARGET_TICKS,
    ).canonical_bytes()
    online_bytes = online.feature_vector(
        direction="long",
        stop_distance=STOP_TICKS,
        target_distance=TARGET_TICKS,
    ).canonical_bytes()
    assert online_bytes == offline_bytes
    assert offline_bytes == (
        b'["-0.1707317073170731707317073171","4","0","12","6","0",'
        b'"0.5","0.0","4","1","0","16:00:01","0","0",'
        b'"long","8","12"]'
    )


def test_feature_vector_record_has_exact_order_and_rejects_schema_drift() -> None:
    pipeline = CausalFeaturePipeline(
        window_span_seconds=WINDOW_SPAN_SECONDS,
        sample_interval_ms=SAMPLE_INTERVAL_MS,
        tick_size=TICK_SIZE,
    )
    for state in _states_from_prefix(_event_prefix()):
        pipeline.observe(state)
    record = pipeline.feature_vector(
        direction="short",
        stop_distance=STOP_TICKS,
        target_distance=TARGET_TICKS,
    ).as_record()

    assert tuple(record) == FEATURE_COLUMNS
    assert validate_feature_record(record).as_record() == record
    with pytest.raises(ValueError, match="schema mismatch"):
        validate_feature_record({**record, "unexpected": "1"})


def test_contract_hash_covers_formula_window_encoding_and_missing_data_rules() -> None:
    descriptor = feature_contract_descriptor(
        window_span_seconds=WINDOW_SPAN_SECONDS,
        sample_interval_ms=SAMPLE_INTERVAL_MS,
        tick_size=TICK_SIZE,
    )
    digest = feature_contract_sha256(
        window_span_seconds=WINDOW_SPAN_SECONDS,
        sample_interval_ms=SAMPLE_INTERVAL_MS,
        tick_size=TICK_SIZE,
    )

    assert descriptor["version"] == FEATURE_CONTRACT_VERSION
    assert descriptor["columns"] == list(FEATURE_COLUMNS)
    assert len(descriptor["formulas"]) == len(FEATURE_COLUMNS)
    assert descriptor["runtime_effect"] == "none; feature construction only"
    assert len(digest) == 64
    assert digest != feature_contract_sha256(
        window_span_seconds=60.0,
        sample_interval_ms=SAMPLE_INTERVAL_MS,
        tick_size=TICK_SIZE,
    )
    json.dumps(descriptor, sort_keys=True)


def test_pipeline_rejects_scoring_before_any_observation() -> None:
    pipeline = CausalFeaturePipeline(
        window_span_seconds=WINDOW_SPAN_SECONDS,
        sample_interval_ms=SAMPLE_INTERVAL_MS,
        tick_size=TICK_SIZE,
    )

    with pytest.raises(ValueError, match="before observing"):
        pipeline.feature_vector(
            direction="long",
            stop_distance=STOP_TICKS,
            target_distance=TARGET_TICKS,
        )


def _states_from_prefix(events: tuple[dict[str, object], ...]) -> tuple[MarketState, ...]:
    state = MarketState()
    states: list[MarketState] = []
    for event in events:
        state = state.update(event)
        states.append(state)
    return tuple(states)


def _event_prefix() -> tuple[dict[str, object], ...]:
    return (
        _depth_event(BASE_TIMESTAMP_NS, "bid", "100.00", "0", "10"),
        _depth_event(BASE_TIMESTAMP_NS, "bid", "99.75", "0", "5"),
        _depth_event(BASE_TIMESTAMP_NS, "ask", "100.25", "0", "10"),
        _depth_event(BASE_TIMESTAMP_NS, "ask", "100.50", "0", "10"),
        _depth_event(BASE_TIMESTAMP_NS + 500_000_000, "bid", "100.00", "10", "4"),
        _depth_event(BASE_TIMESTAMP_NS + 1_000_000_000, "bid", "100.00", "4", "12"),
        _depth_event(BASE_TIMESTAMP_NS + 1_000_000_000, "ask", "100.25", "10", "14"),
        {
            "timestamp_ns": BASE_TIMESTAMP_NS + 1_000_000_000,
            "price": "100.25",
            "size": "4",
            "aggressor_side": "buy",
            "instrument": "MNQ",
            "sequence_id": 1,
        },
    )


def _depth_event(
    timestamp: int,
    side: str,
    price: str,
    old: str,
    new: str,
) -> dict[str, object]:
    return {
        "type": "depth_update",
        "timestamp": timestamp,
        "symbol": "MNQ",
        "side": side,
        "price": price,
        "previous_size": old,
        "new_size": new,
    }
