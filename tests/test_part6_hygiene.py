"""Part 6 testing hygiene: sizing properties, replay fixtures, load, and chaos."""

from __future__ import annotations

import asyncio
import json
import random
import time
from decimal import Decimal
from pathlib import Path

import pytest

from app.market.feed_guard import FeedGuard, FeedGuardConfig
from app.market.receiver import consume_market_stream
from app.risk.kill_switch import KillSwitchState, evaluate_kill_switch
from app.risk.limits import EntryLimitState, InstrumentConfig
from app.risk.sizing import SizingInputs, calculate_position_size

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "replay_session.jsonl"


# --- Item 46: property-based tests for sizing.py Decimal edge cases ---------


def _inputs(**overrides: object) -> SizingInputs:
    base: dict[str, object] = {
        "account_size": Decimal("50000"),
        "remaining_allowable_drawdown": Decimal("2000"),
        "stop_distance_ticks": Decimal("8"),
        "tick_value": Decimal("0.50"),
    }
    base.update(overrides)
    return SizingInputs(**base)  # type: ignore[arg-type]


def test_sizing_property_contracts_never_negative_across_random_inputs() -> None:
    """Seeded property sweep: contracts are never negative, never infinite."""
    rng = random.Random(20260713)
    for _ in range(200):
        inputs = _inputs(
            account_size=Decimal(rng.randrange(1, 10_000_000)),
            remaining_allowable_drawdown=Decimal(rng.randrange(0, 100_000)),
            stop_distance_ticks=Decimal(rng.randrange(1, 200)),
            tick_value=Decimal(rng.randrange(1, 500)) / Decimal("100"),
            estimated_commission=Decimal(rng.randrange(0, 500)) / Decimal("100"),
            estimated_slippage=Decimal(rng.randrange(0, 500)) / Decimal("100"),
        )
        result = calculate_position_size(inputs)
        assert result.contracts >= 0
        assert result.daily_risk_budget >= 0
        assert result.risk_per_contract > 0
        assert Decimal(result.contracts) * result.risk_per_contract <= result.max_risk_per_trade


def test_sizing_property_monotonic_in_stop_distance() -> None:
    """Widening the stop can never increase the contract count."""
    previous = None
    for ticks in (1, 2, 4, 8, 16, 32, 64):
        contracts = calculate_position_size(_inputs(stop_distance_ticks=Decimal(ticks))).contracts
        if previous is not None:
            assert contracts <= previous
        previous = contracts


def test_sizing_edge_zero_stop_distance_is_rejected() -> None:
    """A zero or negative stop distance is an explicit error, not a huge size."""
    with pytest.raises(ValueError):
        calculate_position_size(_inputs(stop_distance_ticks=Decimal("0")))
    with pytest.raises(ValueError):
        calculate_position_size(_inputs(stop_distance_ticks=Decimal("-1")))


def test_sizing_edge_tiny_and_huge_accounts() -> None:
    """A tiny account sizes to zero contracts; a huge one stays finite and capped."""
    tiny = calculate_position_size(
        _inputs(account_size=Decimal("100"), remaining_allowable_drawdown=Decimal("10")),
    )
    assert tiny.contracts == 0

    huge = calculate_position_size(
        _inputs(account_size=Decimal("1000000000"), remaining_allowable_drawdown=Decimal("100000000")),
    )
    assert huge.contracts >= 0
    assert huge.daily_risk_budget == min(
        Decimal("1000000000") * Decimal("0.01"),
        Decimal("100000000") * Decimal("0.15"),
    )


# --- Item 47: integration against a Bookmap Replay fixture -------------------


class _ListStream:
    def __init__(self, payloads: list[str]) -> None:
        self._payloads = payloads

    def __aiter__(self):
        async def generate():
            for payload in self._payloads:
                yield payload

        return generate()


def test_replay_fixture_flows_through_receiver_and_guard() -> None:
    """A recorded-style Bookmap Replay fixture drives the real pipeline."""
    payloads = FIXTURE_PATH.read_text(encoding="utf-8").strip().splitlines()
    guard = FeedGuard(FeedGuardConfig(source_mode="replay"))
    control_events: list[str] = []

    result = asyncio.run(
        consume_market_stream(
            _ListStream(payloads),
            state_store=None,
            on_control_event=lambda event: (
                guard.handle_control_event(event),
                control_events.append(str(event["type"])),
            ),
            event_filter=lambda event: guard.ingest_market_event(event)[0],
            on_schema_error=guard.record_malformed,
        ),
    )

    assert result.events_processed >= 6
    assert "connected" in control_events
    assert "session_ended" in control_events
    status = guard.status()
    assert status.malformed_events == 0
    assert status.sequence_gaps == 0
    assert result.final_state.best_bid is not None


# --- Item 49: load test under rapid tick bursts ------------------------------


def test_bridge_burst_load_processes_thousands_of_ticks_quickly() -> None:
    """A 6,000-event burst is fully processed without drops in bounded time."""
    payloads: list[str] = []
    base_ns = 1_752_000_000_000_000_000
    for index in range(3000):
        payloads.append(
            json.dumps(
                {
                    "type": "depth_update",
                    "timestamp": base_ns + index * 1_000_000,
                    "symbol": "MNQ",
                    "side": "bid" if index % 2 == 0 else "ask",
                    "price": f"{100 + (index % 40) * 0.25:.2f}",
                    "previous_size": "0",
                    "new_size": str(10 + index % 90),
                },
            ),
        )
        payloads.append(
            json.dumps(
                {
                    "timestamp_ns": base_ns + index * 1_000_000 + 500,
                    "price": f"{100 + (index % 40) * 0.25:.2f}",
                    "size": "2",
                    "aggressor_side": "buy" if index % 2 == 0 else "sell",
                    "instrument": "MNQ",
                    "sequence_id": index + 1,
                },
            ),
        )

    guard = FeedGuard(FeedGuardConfig(source_mode="replay"))
    guard.handle_control_event({"type": "connected"})
    started = time.monotonic()
    result = asyncio.run(
        consume_market_stream(
            _ListStream(payloads),
            state_store=None,
            event_filter=lambda event: guard.ingest_market_event(event)[0],
            on_schema_error=guard.record_malformed,
        ),
    )
    elapsed = time.monotonic() - started

    assert result.events_processed == 6000
    status = guard.status()
    assert status.sequence_gaps == 0
    assert status.malformed_events == 0
    assert elapsed < 30, f"burst took {elapsed:.1f}s - bridge path too slow"


# --- Item 50: chaos test - mid-session disconnect fails safe -----------------


def test_chaos_mid_session_disconnect_fails_safe() -> None:
    """A dead feed mid-session blocks risk and demands flatten, unambiguously."""
    guard = FeedGuard(FeedGuardConfig(source_mode="replay"))
    guard.handle_control_event({"type": "connected"})
    guard.ingest_market_event(
        {
            "type": "depth_update",
            "timestamp": 1_000,
            "symbol": "MNQ",
            "side": "bid",
            "price": "100.00",
            "previous_size": "0",
            "new_size": "10",
        },
    )

    guard.handle_control_event({"type": "disconnected"})
    status = guard.status()

    assert status.connection_healthy is False
    assert status.risk_connection_ok is False
    assert status.mode == "offline"

    from datetime import date

    limits_state = EntryLimitState(
        instrument_configs=(InstrumentConfig(symbol="MNQ"),),
        open_positions=1,
        losing_trades_today=0,
        entries_today=1,
        daily_realized_loss=Decimal("0"),
        daily_risk_budget=Decimal("500"),
        proposed_trade_risk=Decimal("20"),
        max_risk_per_trade=Decimal("200"),
        has_open_losing_position=False,
        adds_to_existing_position=False,
        stop_distance_ticks=Decimal("8"),
        connection_healthy=status.risk_connection_ok,
        account_synchronized=True,
        unresolved_order_cancellation_pending=False,
        current_date=date(2026, 7, 13),
    )
    decision = evaluate_kill_switch(KillSwitchState(entry_limits=limits_state))

    assert decision.flatten_now is True
    assert "connection" in decision.reason.lower() or "risk limit" in decision.reason.lower()
