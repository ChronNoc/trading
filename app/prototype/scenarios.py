"""Deterministic synthetic Bookmap-compatible prototype scenarios."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from app.market.state import MarketState
from app.strategy.setups import (
    LongAbsorptionReclaimContext,
    SetupEvaluationResult,
    evaluate_long_absorption_reclaim_setup,
)
from app.strategy.spec import (
    ContextRequirementsSpec,
    EntrySetupSpec,
    ExitSpec,
    ImportantLevel,
    InstrumentSpec,
    RiskSpec,
    SessionSpec,
    StrategySpec,
)
from bookmap_addon.events import format_depth_update, format_trade

PROTOTYPE_SYMBOL = "MNQ-PROTOTYPE"
SCENARIO_VERSION = "prototype-v1"
DEFAULT_SEED = 20260711
DEFAULT_START_UTC = datetime(2026, 7, 10, 13, 30, tzinfo=UTC)
DEFAULT_START_TIMESTAMP_NS = int(DEFAULT_START_UTC.timestamp()) * 1_000_000_000

ScenarioKind = Literal[
    "warmup",
    "clean_long_absorption_reclaim",
    "rejected_lookalike",
    "volatility_transition",
    "disconnect_reconnect",
]


@dataclass(frozen=True, slots=True)
class PrototypeScheduledEvent:
    """One timestamped prototype stream event."""

    event: dict[str, object]
    scenario: ScenarioKind
    description: str

    @property
    def timestamp_ns(self) -> int:
        """Return the event timestamp in nanoseconds."""
        if "timestamp_ns" in self.event:
            return int(self.event["timestamp_ns"])
        return int(self.event["timestamp"])


@dataclass(frozen=True, slots=True)
class PrototypeWindow:
    """A labeled setup window inside the prototype scenario."""

    kind: ScenarioKind
    start_timestamp_ns: int
    end_timestamp_ns: int
    important_level_price: Decimal
    description: str


@dataclass(frozen=True, slots=True)
class PrototypeScenario:
    """A complete deterministic prototype demonstration."""

    events: tuple[PrototypeScheduledEvent, ...]
    clean_window: PrototypeWindow
    rejected_window: PrototypeWindow
    seed: int
    scenario_version: str
    start_timestamp_ns: int
    symbol: str = PROTOTYPE_SYMBOL


@dataclass(frozen=True, slots=True)
class PrototypeDashboardSnapshot:
    """GUI-facing state for the synthetic prototype runtime."""

    banner: str
    runtime_state: str
    synthetic_status: str
    instrument: str
    source_mode: str
    scenario: str
    session: str
    regime: str
    profile: str
    warmup: str
    depth_events: int
    trade_events: int
    control_events: int
    current_setup: str
    decision: str
    explanations: tuple[str, ...]
    raw_features: str
    normalized_features: str
    shadow_order: str
    report_path: str
    playback_speed: int
    paused: bool
    last_trade_price: str = ""


def build_default_prototype_scenario(
    *,
    seed: int = DEFAULT_SEED,
    start_timestamp_ns: int = DEFAULT_START_TIMESTAMP_NS,
) -> PrototypeScenario:
    """Build the default 2-4 minute accelerated prototype demonstration."""
    rng = random.Random(seed)
    events: list[PrototypeScheduledEvent] = []
    sequence_id = 1

    _append_warmup(events, start_timestamp_ns, rng)
    clean_start = start_timestamp_ns + 160_000_000_000
    sequence_id = _append_absorption(
        events,
        start_timestamp_ns=clean_start,
        important_level_price=Decimal("100.00"),
        include_reload=True,
        include_reclaim=True,
        sequence_id=sequence_id,
        scenario="clean_long_absorption_reclaim",
    )
    clean_window = PrototypeWindow(
        kind="clean_long_absorption_reclaim",
        start_timestamp_ns=clean_start,
        end_timestamp_ns=clean_start + 80_000_000_000,
        important_level_price=Decimal("100.00"),
        description="Clean long absorption reclaim with sell pressure, reload, ask pull, and reclaim.",
    )

    rejected_start = start_timestamp_ns + 280_000_000_000
    sequence_id = _append_absorption(
        events,
        start_timestamp_ns=rejected_start,
        important_level_price=Decimal("99.00"),
        include_reload=False,
        include_reclaim=False,
        sequence_id=sequence_id,
        scenario="rejected_lookalike",
    )
    rejected_window = PrototypeWindow(
        kind="rejected_lookalike",
        start_timestamp_ns=rejected_start,
        end_timestamp_ns=rejected_start + 80_000_000_000,
        important_level_price=Decimal("99.00"),
        description="Lookalike with sell pressure but no meaningful bid reload or reclaim.",
    )

    sequence_id = _append_volatility_transition(events, start_timestamp_ns + 420_000_000_000, sequence_id)
    _append_disconnect_reconnect_controls(events, start_timestamp_ns + 560_000_000_000, seed)

    return PrototypeScenario(
        events=tuple(sorted(events, key=lambda item: item.timestamp_ns)),
        clean_window=clean_window,
        rejected_window=rejected_window,
        seed=seed,
        scenario_version=SCENARIO_VERSION,
        start_timestamp_ns=start_timestamp_ns,
    )


def prototype_strategy_spec() -> StrategySpec:
    """Return the deterministic strategy spec used by the prototype scenario."""
    return StrategySpec(
        instrument=InstrumentSpec(symbol="MNQ", allowed_contracts=1),
        session=SessionSpec(timezone="America/New_York", allowed_start="09:30", allowed_end="16:00"),
        context_requirements=ContextRequirementsSpec(
            trend_condition="prototype",
            important_levels=[ImportantLevel.OVERNIGHT_LOW, ImportantLevel.MANUALLY_DEFINED_LEVEL],
        ),
        entry_setup=EntrySetupSpec(
            liquidity_minimum=Decimal("90"),
            aggressive_volume_minimum=Decimal("400"),
            maximum_price_progress_ticks=Decimal("3"),
            reclaim_ticks=Decimal("1"),
            confirmation_window_ms=Decimal("5000"),
            min_reload_count=Decimal("1"),
            min_ask_pull_ratio=Decimal("0.50"),
        ),
        risk=RiskSpec(
            maximum_trades_per_day=3,
            daily_loss_fraction=Decimal("0.01"),
            maximum_open_positions=1,
        ),
        exit=ExitSpec(
            stop_method=Decimal("8"),
            target_method=Decimal("16"),
            break_even_rule=Decimal("8"),
            time_stop_seconds=Decimal("900"),
        ),
    )


def empty_prototype_dashboard_snapshot() -> PrototypeDashboardSnapshot:
    """Return a safe default prototype dashboard snapshot."""
    return PrototypeDashboardSnapshot(
        banner="PROTOTYPE DATA - NOT REAL MARKET DATA",
        runtime_state="not running",
        synthetic_status="waiting",
        instrument=PROTOTYPE_SYMBOL,
        source_mode="PROTOTYPE",
        scenario="default deterministic prototype",
        session="New York open synthetic session",
        regime="warming",
        profile="prototype",
        warmup="0 / warming",
        depth_events=0,
        trade_events=0,
        control_events=0,
        current_setup="waiting for synthetic feed",
        decision="none",
        explanations=(),
        raw_features="not available",
        normalized_features="not available",
        shadow_order="none",
        report_path="not written yet",
        playback_speed=5,
        paused=False,
        last_trade_price="",
    )


def evaluate_prototype_setups(scenario: PrototypeScenario) -> dict[str, SetupEvaluationResult]:
    """Evaluate clean and rejected windows through the existing strategy engine."""
    state = MarketState()
    snapshots: list[MarketState] = []
    states_by_time: list[tuple[int, MarketState]] = []
    for scheduled in scenario.events:
        if _is_market_event(scheduled.event):
            state = state.update(scheduled.event)
            snapshots.append(state)
            states_by_time.append((scheduled.timestamp_ns, state))

    return {
        "clean": _evaluate_window(states_by_time, scenario.clean_window),
        "rejected": _evaluate_window(states_by_time, scenario.rejected_window),
    }


def _evaluate_window(
    states_by_time: list[tuple[int, MarketState]],
    window: PrototypeWindow,
) -> SetupEvaluationResult:
    window_states = [
        state
        for timestamp_ns, state in states_by_time
        if window.start_timestamp_ns <= timestamp_ns <= window.end_timestamp_ns
    ]
    if not window_states:
        raise ValueError(f"prototype window {window.kind} has no market states")
    return evaluate_long_absorption_reclaim_setup(
        window_states[-1],
        window_states,
        prototype_strategy_spec(),
        LongAbsorptionReclaimContext(
            important_level=ImportantLevel.OVERNIGHT_LOW,
            important_level_price=window.important_level_price,
        ),
    )


def _append_warmup(events: list[PrototypeScheduledEvent], start_timestamp_ns: int, rng: random.Random) -> None:
    bid = Decimal("100.00")
    for index in range(24):
        timestamp_ns = start_timestamp_ns + index * 5_000_000_000
        drift = Decimal(rng.choice(["0", "0.25", "-0.25"]))
        bid = bid + drift
        ask = bid + Decimal("0.25")
        bid_size = Decimal(100 + rng.randrange(-10, 12))
        ask_size = Decimal(100 + rng.randrange(-10, 12))
        _append_depth(events, timestamp_ns, "bid", bid, Decimal("0"), bid_size, "warmup", "populate bid depth")
        _append_depth(events, timestamp_ns + 1, "ask", ask, Decimal("0"), ask_size, "warmup", "populate ask depth")
        _append_trade(
            events,
            timestamp_ns + 2,
            ask if index % 2 == 0 else bid,
            Decimal(2 + rng.randrange(0, 4)),
            "buy" if index % 2 == 0 else "sell",
            index + 1,
            "warmup",
            "ordinary warm-up trade",
        )


def _append_absorption(
    events: list[PrototypeScheduledEvent],
    *,
    start_timestamp_ns: int,
    important_level_price: Decimal,
    include_reload: bool,
    include_reclaim: bool,
    sequence_id: int,
    scenario: ScenarioKind,
) -> int:
    tick = Decimal("0.25")
    ask_price = important_level_price + tick
    _append_book_clear(events, start_timestamp_ns - 2_000_000_000, scenario, important_level_price)
    _append_depth(events, start_timestamp_ns, "bid", important_level_price, Decimal("0"), Decimal("120"), scenario, "defended bid appears")
    _append_depth(events, start_timestamp_ns + 1, "ask", ask_price, Decimal("0"), Decimal("100"), scenario, "ask depth appears")
    if include_reload:
        _append_depth(events, start_timestamp_ns + 10_000_000_000, "bid", important_level_price, Decimal("120"), Decimal("20"), scenario, "bid absorbs and drops")
        _append_depth(events, start_timestamp_ns + 20_000_000_000, "bid", important_level_price, Decimal("20"), Decimal("135"), scenario, "bid reloads")
    _append_depth(events, start_timestamp_ns + 25_000_000_000, "ask", ask_price, Decimal("100"), Decimal("10"), scenario, "ask liquidity pulls")
    _append_trade(events, start_timestamp_ns + 30_000_000_000, important_level_price, Decimal("420"), "sell", sequence_id, scenario, "aggressive selling")
    sequence_id += 1
    if include_reclaim:
        _append_depth(events, start_timestamp_ns + 55_000_000_000, "ask", ask_price, Decimal("10"), Decimal("0"), scenario, "near ask clears")
        _append_depth(events, start_timestamp_ns + 60_000_000_000, "bid", important_level_price + tick, Decimal("0"), Decimal("110"), scenario, "price reclaims")
    else:
        _append_depth(events, start_timestamp_ns + 60_000_000_000, "bid", important_level_price, Decimal("120"), Decimal("45"), scenario, "no bid reload")
    return sequence_id


def _append_book_clear(
    events: list[PrototypeScheduledEvent],
    start_timestamp_ns: int,
    scenario: ScenarioKind,
    center_price: Decimal,
) -> None:
    price = center_price - Decimal("2.00")
    index = 0
    while price <= center_price + Decimal("2.00"):
        _append_depth(events, start_timestamp_ns + index, "bid", price, Decimal("0"), Decimal("0"), scenario, "clear stale bid level")
        index += 1
        _append_depth(events, start_timestamp_ns + index, "ask", price, Decimal("0"), Decimal("0"), scenario, "clear stale ask level")
        index += 1
        price += Decimal("0.25")


def _append_volatility_transition(
    events: list[PrototypeScheduledEvent],
    start_timestamp_ns: int,
    sequence_id: int,
) -> int:
    price = Decimal("100.50")
    for index in range(10):
        timestamp_ns = start_timestamp_ns + index * 8_000_000_000
        price += Decimal("0.75") if index % 2 == 0 else Decimal("-1.00")
        _append_depth(events, timestamp_ns, "bid", price, Decimal("0"), Decimal("60"), "volatility_transition", "high-volatility bid")
        _append_depth(events, timestamp_ns + 1, "ask", price + Decimal("0.50"), Decimal("0"), Decimal("55"), "volatility_transition", "wide ask")
        _append_trade(events, timestamp_ns + 2, price, Decimal("20"), "buy" if index % 2 else "sell", sequence_id, "volatility_transition", "volatile trade")
        sequence_id += 1
    return sequence_id


def _append_disconnect_reconnect_controls(
    events: list[PrototypeScheduledEvent],
    start_timestamp_ns: int,
    seed: int,
) -> None:
    events.append(_control(start_timestamp_ns, "data_gap", "disconnect_reconnect", seed, reason="prototype data gap"))
    events.append(_control(start_timestamp_ns + 1, "disconnected", "disconnect_reconnect", seed, reason="prototype reconnect exercise"))
    events.append(_control(start_timestamp_ns + 15_000_000_000, "connected", "disconnect_reconnect", seed))
    events.append(_control(start_timestamp_ns + 15_000_000_001, "prototype_mode", "disconnect_reconnect", seed))
    events.append(_control(start_timestamp_ns + 30_000_000_000, "session_ended", "disconnect_reconnect", seed))


def _append_depth(
    events: list[PrototypeScheduledEvent],
    timestamp_ns: int,
    side: str,
    price: Decimal,
    previous_size: Decimal,
    new_size: Decimal,
    scenario: ScenarioKind,
    description: str,
) -> None:
    events.append(
        PrototypeScheduledEvent(
            event=format_depth_update(
                timestamp=timestamp_ns,
                symbol=PROTOTYPE_SYMBOL,
                side=side,
                price=format(price, "f"),
                previous_size=format(previous_size, "f"),
                new_size=format(new_size, "f"),
            ),
            scenario=scenario,
            description=description,
        ),
    )


def _append_trade(
    events: list[PrototypeScheduledEvent],
    timestamp_ns: int,
    price: Decimal,
    size: Decimal,
    aggressor_side: str,
    sequence_id: int,
    scenario: ScenarioKind,
    description: str,
) -> None:
    trade = format_trade(
        timestamp_ns=timestamp_ns,
        price=format(price, "f"),
        size=format(size, "f"),
        aggressor_side=aggressor_side,
        instrument=PROTOTYPE_SYMBOL,
        sequence_id=sequence_id,
    )
    trade["type"] = "trade"
    events.append(PrototypeScheduledEvent(event=trade, scenario=scenario, description=description))


def _control(
    timestamp_ns: int,
    event_type: str,
    scenario: ScenarioKind,
    seed: int,
    *,
    reason: str | None = None,
) -> PrototypeScheduledEvent:
    event: dict[str, object] = {
        "type": event_type,
        "timestamp_ns": timestamp_ns,
        "source_mode": "prototype",
        "alias": PROTOTYPE_SYMBOL,
        "symbol": PROTOTYPE_SYMBOL,
        "synthetic": True,
        "seed": seed,
        "scenario_version": SCENARIO_VERSION,
        "valid_for_real_training": False,
        "valid_for_analysis": "prototype_only",
    }
    if reason is not None:
        event["reason"] = reason
    return PrototypeScheduledEvent(event=event, scenario=scenario, description=event_type)


def scenario_with_controls(scenario: PrototypeScenario, *, playback_speed: int) -> tuple[PrototypeScheduledEvent, ...]:
    """Return the scenario with startup controls and playback-speed metadata."""
    startup = (
        _control(scenario.start_timestamp_ns - 2, "connected", "warmup", scenario.seed),
        _control(scenario.start_timestamp_ns - 1, "prototype_mode", "warmup", scenario.seed),
        _control(scenario.start_timestamp_ns, "realtime_started", "warmup", scenario.seed),
        _control(scenario.start_timestamp_ns + 60_000_000_000, "heartbeat", "warmup", scenario.seed),
    )
    result = []
    for event in (*startup, *scenario.events):
        event_dict = dict(event.event)
        if not _is_market_event(event_dict):
            event_dict["playback_speed"] = str(playback_speed)
        result.append(PrototypeScheduledEvent(event=event_dict, scenario=event.scenario, description=event.description))
    return tuple(sorted(result, key=lambda item: item.timestamp_ns))


def _is_market_event(event: dict[str, object]) -> bool:
    return event.get("type") == "depth_update" or {"timestamp_ns", "price", "size", "aggressor_side", "instrument", "sequence_id"}.issubset(event)
