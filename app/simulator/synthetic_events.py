"""Seeded synthetic MNQ market-event generation for simulator tests."""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, TypeAlias

from app.strategy.spec import ImportantLevel

RawMarketEvent: TypeAlias = dict[str, object]
PatternKind: TypeAlias = Literal["clean_long_absorption_reclaim", "lookalike_no_bid_reload"]


@dataclass(frozen=True, slots=True)
class SyntheticPatternWindow:
    """Timestamp window and context for one generated setup candidate."""

    kind: PatternKind
    start_timestamp_ns: int
    end_timestamp_ns: int
    important_level: ImportantLevel
    important_level_price: Decimal
    description: str


@dataclass(frozen=True, slots=True)
class SyntheticEventSequence:
    """Generated market events and labeled setup windows."""

    events: tuple[RawMarketEvent, ...]
    clean_window: SyntheticPatternWindow
    lookalike_window: SyntheticPatternWindow


def generate_mnq_absorption_reclaim_events(
    seed: int = 7,
    base_timestamp_ns: int = 0,
) -> SyntheticEventSequence:
    """Generate deterministic MNQ events with one clean setup and one lookalike."""
    if base_timestamp_ns < 0:
        raise ValueError("base_timestamp_ns must be non-negative")

    rng = random.Random(seed)
    events: list[RawMarketEvent] = []
    sequence_id = 1

    clean_start = base_timestamp_ns + 1_000
    sequence_id = _append_absorption_reclaim_pattern(
        events=events,
        sequence_id=sequence_id,
        start_timestamp_ns=clean_start,
        important_level_price=Decimal("100.00"),
        sell_volume=Decimal(410 + rng.randrange(0, 25)),
        include_bid_reload=True,
        rng=rng,
    )
    clean_window = SyntheticPatternWindow(
        kind="clean_long_absorption_reclaim",
        start_timestamp_ns=clean_start + 1,
        end_timestamp_ns=clean_start + 60,
        important_level=ImportantLevel.OVERNIGHT_LOW,
        important_level_price=Decimal("100.00"),
        description="Clean long absorption-reclaim with aggressive selling, bid reload, ask pull, and reclaim.",
    )

    reset_start = clean_start + 1_000
    _append_book_clear(
        events,
        reset_start,
        bid_prices=(Decimal("100.25"), Decimal("100.00")),
        ask_prices=(Decimal("100.50"), Decimal("100.25")),
    )

    lookalike_start = reset_start + 1_000
    _append_absorption_reclaim_pattern(
        events=events,
        sequence_id=sequence_id,
        start_timestamp_ns=lookalike_start,
        important_level_price=Decimal("99.00"),
        sell_volume=Decimal(410 + rng.randrange(0, 25)),
        include_bid_reload=False,
        rng=rng,
    )
    lookalike_window = SyntheticPatternWindow(
        kind="lookalike_no_bid_reload",
        start_timestamp_ns=lookalike_start + 1,
        end_timestamp_ns=lookalike_start + 60,
        important_level=ImportantLevel.OVERNIGHT_LOW,
        important_level_price=Decimal("99.00"),
        description="Lookalike with aggressive selling and reclaim but no bid liquidity reload.",
    )

    return SyntheticEventSequence(
        events=tuple(events),
        clean_window=clean_window,
        lookalike_window=lookalike_window,
    )


def _append_absorption_reclaim_pattern(
    *,
    events: list[RawMarketEvent],
    sequence_id: int,
    start_timestamp_ns: int,
    important_level_price: Decimal,
    sell_volume: Decimal,
    include_bid_reload: bool,
    rng: random.Random,
) -> int:
    tick = Decimal("0.25")
    bid_size = Decimal(12 + rng.randrange(0, 4))
    ask_size = Decimal(13 + rng.randrange(0, 4))
    dropped_bid_size = Decimal(3 + rng.randrange(0, 3))
    reloaded_bid_size = Decimal(12 + rng.randrange(0, 5))
    pulled_ask_size = Decimal(2 + rng.randrange(0, 3))
    reclaim_bid_size = Decimal(11 + rng.randrange(0, 4))
    reclaim_ask_size = Decimal(10 + rng.randrange(0, 4))

    ask_price = important_level_price + tick
    reclaim_bid_price = important_level_price + tick
    reclaim_ask_price = important_level_price + (tick * Decimal("2"))

    events.append(
        _depth_update(start_timestamp_ns, "bid", important_level_price, Decimal("0"), bid_size),
    )
    events.append(
        _depth_update(start_timestamp_ns + 1, "ask", ask_price, Decimal("0"), ask_size),
    )

    if include_bid_reload:
        events.append(
            _depth_update(
                start_timestamp_ns + 10,
                "bid",
                important_level_price,
                bid_size,
                dropped_bid_size,
            ),
        )
        events.append(
            _depth_update(
                start_timestamp_ns + 30,
                "bid",
                important_level_price,
                dropped_bid_size,
                reloaded_bid_size,
            ),
        )

    events.append(
        _depth_update(start_timestamp_ns + 35, "ask", ask_price, ask_size, pulled_ask_size),
    )
    events.append(
        _trade(
            timestamp_ns=start_timestamp_ns + 40,
            price=important_level_price,
            size=sell_volume,
            aggressor_side="sell",
            sequence_id=sequence_id,
        ),
    )
    sequence_id += 1

    events.append(
        _depth_update(start_timestamp_ns + 50, "ask", ask_price, pulled_ask_size, Decimal("0")),
    )
    events.append(
        _depth_update(start_timestamp_ns + 51, "ask", reclaim_ask_price, Decimal("0"), reclaim_ask_size),
    )
    events.append(
        _depth_update(
            start_timestamp_ns + 60,
            "bid",
            reclaim_bid_price,
            Decimal("0"),
            reclaim_bid_size,
        ),
    )
    return sequence_id


def _append_book_clear(
    events: list[RawMarketEvent],
    timestamp_ns: int,
    *,
    bid_prices: tuple[Decimal, ...],
    ask_prices: tuple[Decimal, ...],
) -> None:
    offset = 0
    for price in bid_prices:
        events.append(_depth_update(timestamp_ns + offset, "bid", price, Decimal("1"), Decimal("0")))
        offset += 1
    for price in ask_prices:
        events.append(_depth_update(timestamp_ns + offset, "ask", price, Decimal("1"), Decimal("0")))
        offset += 1


def _depth_update(
    timestamp_ns: int,
    side: str,
    price: Decimal,
    previous_size: Decimal,
    new_size: Decimal,
) -> RawMarketEvent:
    return {
        "type": "depth_update",
        "timestamp": timestamp_ns,
        "symbol": "MNQ",
        "side": side,
        "price": format(price, "f"),
        "previous_size": format(previous_size, "f"),
        "new_size": format(new_size, "f"),
    }


def _trade(
    *,
    timestamp_ns: int,
    price: Decimal,
    size: Decimal,
    aggressor_side: str,
    sequence_id: int,
) -> RawMarketEvent:
    return {
        "type": "trade",
        "timestamp_ns": timestamp_ns,
        "price": format(price, "f"),
        "size": format(size, "f"),
        "aggressor_side": aggressor_side,
        "instrument": "MNQ",
        "sequence_id": sequence_id,
    }
