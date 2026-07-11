"""Tests for the replay simulator engine."""

from __future__ import annotations

from decimal import Decimal

from app.market.state import MarketState
from app.simulator.engine import (
    RiskDecision,
    SimulatorConfig,
    StrategySignal,
    replay_market_events,
)
from app.simulator.fills import FillAssumptions
from app.simulator.slippage import SlippageModel


def test_replay_market_events_runs_synthetic_session_end_to_end() -> None:
    """A synthetic session models ack delay, replace delay, partial fills, and rejections."""
    strategy = TimestampStrategy(
        {
            100: StrategySignal("MNQ", "buy", 3, Decimal("100.25")),
            180: StrategySignal("MNQ", "buy", 3, Decimal("100.50")),
            500: StrategySignal("MNQ", "buy", 1, Decimal("100.50")),
            700: StrategySignal("MNQ", "buy", 1, Decimal("100.50")),
        },
    )
    risk = TimestampRiskEngine(rejected_timestamps={700})
    config = SimulatorConfig(
        acknowledgement_delay_ns=100,
        cancel_replace_delay_ns=100,
        fill_assumptions=FillAssumptions(queue_position_fraction=Decimal("0")),
        slippage_model=SlippageModel(
            tick_size=Decimal("0.25"),
            fixed_ticks=Decimal("1"),
            volatility_multiplier=Decimal("0"),
        ),
    )

    result = replay_market_events(
        events=_shuffled_session_events(),
        strategy_engine=strategy,
        risk_engine=risk,
        config=config,
    )

    assert [trade.fill_timestamp_ns for trade in result.trades] == [280, 350]
    assert [trade.filled_quantity for trade in result.trades] == [1, 2]
    assert result.trades[0].partial is True
    assert result.trades[0].requested_price == Decimal("100.50")
    assert result.trades[0].base_fill_price == Decimal("100.50")
    assert result.trades[0].actual_fill_price == Decimal("100.75")
    assert result.trades[1].partial is False
    assert result.trades[1].remaining_quantity == 0
    assert [event.event_type for event in result.order_events] == [
        "submitted",
        "cancel_replace_requested",
        "acknowledged",
        "cancel_replace_applied",
        "disconnect",
        "reconnect",
    ]
    assert [event.timestamp_ns for event in result.order_events[:4]] == [100, 180, 200, 280]
    assert [rejection.timestamp_ns for rejection in result.rejected_orders] == [500, 700]
    assert "Disconnected" in result.rejected_orders[0].reason[0]
    assert result.rejected_orders[1].reason == ("risk rejected at 700",)
    assert result.connected is True


def test_replay_market_events_replays_inputs_in_timestamp_order() -> None:
    """Out-of-order input events are applied in timestamp order."""
    strategy = RecordingStrategy()

    replay_market_events(
        events=[
            _depth_event(timestamp=300, side="ask", price="100.25", old="0", new="1"),
            _depth_event(timestamp=100, side="bid", price="100.00", old="0", new="1"),
            _trade_event(timestamp_ns=200, side="sell", size="1"),
        ],
        strategy_engine=strategy,
        risk_engine=AllowRiskEngine(),
    )

    assert strategy.timestamps == [100, 200, 300]


def test_replay_market_events_records_risk_rejection_without_active_order() -> None:
    """Risk rejection records a rejected order and never produces a fill."""
    strategy = TimestampStrategy({100: StrategySignal("MNQ", "buy", 1, Decimal("100.25"))})
    risk = TimestampRiskEngine(rejected_timestamps={100})

    result = replay_market_events(
        events=[
            _depth_event(timestamp=0, side="bid", price="100.00", old="0", new="1"),
            _depth_event(timestamp=0, side="ask", price="100.25", old="0", new="1"),
            _trade_event(timestamp_ns=100, side="sell", size="1"),
            _trade_event(timestamp_ns=200, side="sell", size="1"),
        ],
        strategy_engine=strategy,
        risk_engine=risk,
    )

    assert result.trades == ()
    assert len(result.rejected_orders) == 1
    assert result.rejected_orders[0].reason == ("risk rejected at 100",)


class TimestampStrategy:
    """Strategy test double that emits configured signals by state timestamp."""

    def __init__(self, signals_by_timestamp: dict[int, StrategySignal]) -> None:
        """Create a timestamp-keyed strategy."""
        self.signals_by_timestamp = signals_by_timestamp

    def __call__(
        self,
        market_state: MarketState,
        feature_window: tuple[MarketState, ...],
    ) -> StrategySignal | None:
        """Return the configured signal for the current timestamp."""
        signal = self.signals_by_timestamp.get(market_state.timestamp_ns)
        if signal is None:
            return None
        return StrategySignal(
            symbol=signal.symbol,
            side=signal.side,
            quantity=signal.quantity,
            requested_price=signal.requested_price,
            signal_timestamp_ns=market_state.timestamp_ns,
            reason=signal.reason,
        )


class RecordingStrategy:
    """Strategy test double that records call timestamps."""

    def __init__(self) -> None:
        """Create a recording strategy."""
        self.timestamps: list[int] = []

    def __call__(
        self,
        market_state: MarketState,
        feature_window: tuple[MarketState, ...],
    ) -> None:
        """Record the current timestamp and emit no signal."""
        self.timestamps.append(market_state.timestamp_ns)
        return None


class TimestampRiskEngine:
    """Risk test double that rejects configured timestamps."""

    def __init__(self, rejected_timestamps: set[int]) -> None:
        """Create a timestamp-keyed risk engine."""
        self.rejected_timestamps = rejected_timestamps

    def __call__(self, signal: StrategySignal, market_state: MarketState) -> RiskDecision:
        """Reject signals whose timestamp is configured for rejection."""
        if signal.signal_timestamp_ns in self.rejected_timestamps:
            return RiskDecision(False, (f"risk rejected at {signal.signal_timestamp_ns}",))
        return RiskDecision(True, ("allowed",))


class AllowRiskEngine:
    """Risk test double that allows every signal."""

    def __call__(self, signal: StrategySignal, market_state: MarketState) -> RiskDecision:
        """Allow every signal."""
        return RiskDecision(True, ("allowed",))


def _shuffled_session_events() -> list[dict[str, object]]:
    return [
        _depth_event(timestamp=0, side="ask", price="100.50", old="0", new="1"),
        _trade_event(timestamp_ns=100, side="sell", size="1"),
        {"type": "reconnect", "timestamp_ns": 600},
        _depth_event(timestamp=0, side="bid", price="100.00", old="0", new="10"),
        _trade_event(timestamp_ns=280, side="sell", size="1"),
        _trade_event(timestamp_ns=700, side="sell", size="1"),
        {"type": "disconnect", "timestamp_ns": 400},
        _trade_event(timestamp_ns=180, side="sell", size="1"),
        _trade_event(timestamp_ns=500, side="sell", size="1"),
        _trade_event(timestamp_ns=200, side="sell", size="1"),
        _depth_event(timestamp=350, side="ask", price="100.50", old="1", new="5"),
    ]


def _depth_event(
    *,
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


def _trade_event(
    *,
    timestamp_ns: int,
    side: str,
    size: str,
) -> dict[str, object]:
    return {
        "type": "trade",
        "timestamp_ns": timestamp_ns,
        "price": "100.25",
        "size": size,
        "aggressor_side": side,
        "instrument": "MNQ",
        "sequence_id": timestamp_ns,
    }
