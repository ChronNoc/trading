"""Replay engine for simulated strategy, risk, and fill behavior."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal, Protocol, TypeAlias

from app.market.state import MarketState
from app.simulator.fills import FillAssumptions, SimulatedOrder, decide_fill
from app.simulator.slippage import OrderSide, SlippageModel, apply_slippage, calculate_slippage

RawEvent: TypeAlias = Mapping[str, object]
OrderEventType: TypeAlias = Literal[
    "submitted",
    "acknowledged",
    "cancel_replace_requested",
    "cancel_replace_applied",
    "disconnect",
    "reconnect",
]


class StrategyEngine(Protocol):
    """Callable strategy interface used by the replay engine."""

    def __call__(
        self,
        market_state: MarketState,
        feature_window: tuple[MarketState, ...],
    ) -> "StrategySignal | None":
        """Return a new signal for the current state, or None."""


class RiskEngine(Protocol):
    """Callable risk interface used by the replay engine."""

    def __call__(self, signal: "StrategySignal", market_state: MarketState) -> "RiskDecision":
        """Return whether a strategy signal is allowed."""


@dataclass(frozen=True, slots=True)
class StrategySignal:
    """Strategy request for a simulated limit entry."""

    symbol: str
    side: OrderSide
    quantity: int
    requested_price: Decimal
    signal_timestamp_ns: int | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Risk decision for a strategy signal."""

    allowed: bool
    reason: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SimulatorConfig:
    """Configuration for replay delays, fill assumptions, and slippage."""

    acknowledgement_delay_ns: int = 0
    cancel_replace_delay_ns: int = 0
    feature_window_size: int = 50
    fill_assumptions: FillAssumptions = FillAssumptions()
    slippage_model: SlippageModel = SlippageModel(tick_size=Decimal("0.25"))
    short_term_volatility: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class SimulatedTrade:
    """One simulated fill event produced by the replay engine."""

    order_id: str
    symbol: str
    side: OrderSide
    requested_quantity: int
    filled_quantity: int
    remaining_quantity: int
    requested_price: Decimal
    base_fill_price: Decimal
    actual_fill_price: Decimal
    slippage: Decimal
    signal_timestamp_ns: int
    request_timestamp_ns: int
    broker_ack_timestamp_ns: int
    fill_timestamp_ns: int
    partial: bool
    reason: str


@dataclass(frozen=True, slots=True)
class RejectedOrder:
    """Rejected strategy signal recorded during replay."""

    order_id: str
    timestamp_ns: int
    signal: StrategySignal
    reason: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OrderEvent:
    """Lifecycle event for a simulated order or connection state."""

    timestamp_ns: int
    event_type: OrderEventType
    order_id: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class SimulationResult:
    """Complete result of a market-event replay."""

    trades: tuple[SimulatedTrade, ...]
    rejected_orders: tuple[RejectedOrder, ...]
    order_events: tuple[OrderEvent, ...]
    final_market_state: MarketState
    connected: bool


@dataclass(slots=True)
class _ActiveOrder:
    order: SimulatedOrder
    signal_timestamp_ns: int
    request_timestamp_ns: int
    acknowledgement_due_ns: int
    broker_ack_timestamp_ns: int | None = None
    pending_replace_price: Decimal | None = None
    replace_due_ns: int | None = None


def replay_market_events(
    events: Sequence[RawEvent],
    strategy_engine: StrategyEngine,
    risk_engine: RiskEngine,
    config: SimulatorConfig | None = None,
) -> SimulationResult:
    """Replay market events through strategy, risk, delay, fill, and slippage models."""
    replay_config = config or SimulatorConfig()
    _validate_config(replay_config)

    market_state = MarketState()
    feature_window: list[MarketState] = []
    active_order: _ActiveOrder | None = None
    connected = True
    order_sequence = 0
    trades: list[SimulatedTrade] = []
    rejected_orders: list[RejectedOrder] = []
    order_events: list[OrderEvent] = []

    for raw_event in _sort_events(events):
        timestamp_ns = _event_timestamp(raw_event)
        active_order = _acknowledge_if_due(active_order, timestamp_ns, order_events)
        active_order = _apply_replace_if_due(active_order, timestamp_ns, order_events)

        event_type = str(raw_event.get("type", "")).lower()
        market_updated = False
        if event_type == "disconnect":
            connected = False
            order_events.append(
                OrderEvent(timestamp_ns, "disconnect", None, "Disconnected; new entries halted."),
            )
        elif event_type == "reconnect":
            connected = True
            order_events.append(
                OrderEvent(timestamp_ns, "reconnect", None, "Reconnected; new entries may resume."),
            )
        else:
            market_state = market_state.update(raw_event)
            market_updated = True
            feature_window.append(market_state)
            if len(feature_window) > replay_config.feature_window_size:
                feature_window = feature_window[-replay_config.feature_window_size :]

        if (
            market_updated
            and active_order is not None
            and active_order.broker_ack_timestamp_ns is not None
        ):
            active_order = _try_fill_active_order(
                active_order=active_order,
                market_state=market_state,
                timestamp_ns=timestamp_ns,
                config=replay_config,
                trades=trades,
            )

        if not market_updated:
            continue

        signal = strategy_engine(market_state, tuple(feature_window))
        if signal is None:
            continue

        signal_timestamp_ns = signal.signal_timestamp_ns or timestamp_ns
        if not connected:
            order_sequence += 1
            rejected_orders.append(
                RejectedOrder(
                    order_id=_order_id(order_sequence),
                    timestamp_ns=timestamp_ns,
                    signal=signal,
                    reason=("Disconnected: new entries halted until reconnect.",),
                ),
            )
            continue

        if active_order is not None:
            active_order = _maybe_request_cancel_replace(
                active_order=active_order,
                signal=signal,
                timestamp_ns=timestamp_ns,
                risk_engine=risk_engine,
                market_state=market_state,
                config=replay_config,
                rejected_orders=rejected_orders,
                order_events=order_events,
            )
            continue

        risk_decision = risk_engine(signal, market_state)
        order_sequence += 1
        next_order_id = _order_id(order_sequence)
        if not risk_decision.allowed:
            rejected_orders.append(
                RejectedOrder(
                    order_id=next_order_id,
                    timestamp_ns=timestamp_ns,
                    signal=signal,
                    reason=risk_decision.reason or ("Risk engine rejected order.",),
                ),
            )
            continue

        active_order = _ActiveOrder(
            order=SimulatedOrder(
                order_id=next_order_id,
                symbol=signal.symbol,
                side=signal.side,
                quantity=signal.quantity,
                remaining_quantity=signal.quantity,
                limit_price=signal.requested_price,
                submitted_timestamp_ns=timestamp_ns,
            ),
            signal_timestamp_ns=signal_timestamp_ns,
            request_timestamp_ns=timestamp_ns,
            acknowledgement_due_ns=timestamp_ns + replay_config.acknowledgement_delay_ns,
        )
        order_events.append(
            OrderEvent(timestamp_ns, "submitted", next_order_id, "Order submitted to simulator."),
        )

    return SimulationResult(
        trades=tuple(trades),
        rejected_orders=tuple(rejected_orders),
        order_events=tuple(order_events),
        final_market_state=market_state,
        connected=connected,
    )


def _try_fill_active_order(
    *,
    active_order: _ActiveOrder,
    market_state: MarketState,
    timestamp_ns: int,
    config: SimulatorConfig,
    trades: list[SimulatedTrade],
) -> _ActiveOrder | None:
    fill_decision = decide_fill(active_order.order, market_state, config.fill_assumptions)
    if fill_decision.fill_price is None or fill_decision.filled_quantity == 0:
        return active_order

    slippage = calculate_slippage(config.slippage_model, config.short_term_volatility)
    actual_fill_price = apply_slippage(
        fill_decision.fill_price,
        active_order.order.side,
        config.slippage_model,
        config.short_term_volatility,
    )
    trades.append(
        SimulatedTrade(
            order_id=active_order.order.order_id,
            symbol=active_order.order.symbol,
            side=active_order.order.side,
            requested_quantity=active_order.order.quantity,
            filled_quantity=fill_decision.filled_quantity,
            remaining_quantity=fill_decision.remaining_quantity,
            requested_price=active_order.order.limit_price,
            base_fill_price=fill_decision.fill_price,
            actual_fill_price=actual_fill_price,
            slippage=slippage,
            signal_timestamp_ns=active_order.signal_timestamp_ns,
            request_timestamp_ns=active_order.request_timestamp_ns,
            broker_ack_timestamp_ns=active_order.broker_ack_timestamp_ns or timestamp_ns,
            fill_timestamp_ns=timestamp_ns,
            partial=fill_decision.partial,
            reason=fill_decision.reason,
        ),
    )

    if fill_decision.remaining_quantity == 0:
        return None

    active_order.order = replace(
        active_order.order,
        remaining_quantity=fill_decision.remaining_quantity,
    )
    return active_order


def _maybe_request_cancel_replace(
    *,
    active_order: _ActiveOrder,
    signal: StrategySignal,
    timestamp_ns: int,
    risk_engine: RiskEngine,
    market_state: MarketState,
    config: SimulatorConfig,
    rejected_orders: list[RejectedOrder],
    order_events: list[OrderEvent],
) -> _ActiveOrder:
    if signal.side != active_order.order.side or signal.symbol != active_order.order.symbol:
        rejected_orders.append(
            RejectedOrder(
                order_id=active_order.order.order_id,
                timestamp_ns=timestamp_ns,
                signal=signal,
                reason=("An active order already exists for another side or symbol.",),
            ),
        )
        return active_order

    if signal.requested_price == active_order.order.limit_price:
        return active_order

    if active_order.pending_replace_price is not None:
        return active_order

    risk_decision = risk_engine(signal, market_state)
    if not risk_decision.allowed:
        rejected_orders.append(
            RejectedOrder(
                order_id=active_order.order.order_id,
                timestamp_ns=timestamp_ns,
                signal=signal,
                reason=risk_decision.reason or ("Risk engine rejected cancel-replace.",),
            ),
        )
        return active_order

    active_order.pending_replace_price = signal.requested_price
    active_order.replace_due_ns = timestamp_ns + config.cancel_replace_delay_ns
    order_events.append(
        OrderEvent(
            timestamp_ns,
            "cancel_replace_requested",
            active_order.order.order_id,
            "Cancel-replace requested.",
        ),
    )
    return active_order


def _acknowledge_if_due(
    active_order: _ActiveOrder | None,
    timestamp_ns: int,
    order_events: list[OrderEvent],
) -> _ActiveOrder | None:
    if active_order is None:
        return None
    if active_order.broker_ack_timestamp_ns is not None:
        return active_order
    if timestamp_ns < active_order.acknowledgement_due_ns:
        return active_order

    active_order.broker_ack_timestamp_ns = timestamp_ns
    order_events.append(
        OrderEvent(
            timestamp_ns,
            "acknowledged",
            active_order.order.order_id,
            "Order acknowledged by simulator after configured delay.",
        ),
    )
    return active_order


def _apply_replace_if_due(
    active_order: _ActiveOrder | None,
    timestamp_ns: int,
    order_events: list[OrderEvent],
) -> _ActiveOrder | None:
    if active_order is None:
        return None
    if active_order.pending_replace_price is None or active_order.replace_due_ns is None:
        return active_order
    if timestamp_ns < active_order.replace_due_ns:
        return active_order

    active_order.order = replace(active_order.order, limit_price=active_order.pending_replace_price)
    active_order.pending_replace_price = None
    active_order.replace_due_ns = None
    order_events.append(
        OrderEvent(
            timestamp_ns,
            "cancel_replace_applied",
            active_order.order.order_id,
            "Cancel-replace applied after configured delay.",
        ),
    )
    return active_order


def _sort_events(events: Sequence[RawEvent]) -> tuple[RawEvent, ...]:
    return tuple(
        event
        for _timestamp, _index, event in sorted(
            (_event_timestamp(event), index, event) for index, event in enumerate(events)
        )
    )


def _event_timestamp(raw_event: RawEvent) -> int:
    timestamp = raw_event.get("timestamp_ns", raw_event.get("timestamp"))
    if timestamp is None:
        raise ValueError("market event is missing timestamp or timestamp_ns")
    timestamp_ns = int(timestamp)
    if timestamp_ns < 0:
        raise ValueError("event timestamp must be non-negative")
    return timestamp_ns


def _order_id(order_sequence: int) -> str:
    return f"order-{order_sequence:06d}"


def _validate_config(config: SimulatorConfig) -> None:
    if config.acknowledgement_delay_ns < 0:
        raise ValueError("acknowledgement_delay_ns must be non-negative")
    if config.cancel_replace_delay_ns < 0:
        raise ValueError("cancel_replace_delay_ns must be non-negative")
    if config.feature_window_size <= 0:
        raise ValueError("feature_window_size must be greater than 0")
    if config.short_term_volatility < Decimal("0"):
        raise ValueError("short_term_volatility must be non-negative")
