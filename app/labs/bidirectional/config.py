"""Configuration for the Bidirectional Paper Trading Lab (paper-only).

Everything here is inert data. No object in this module can place, route, or
simulate an order by itself - the engine does, and the engine is paper-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal

# Trigger price sources and comparisons for an activation level.
SOURCES = ("last", "bid", "ask", "mid")
COMPARISONS = ("auto", "at_or_below", "at_or_above", "cross_down", "cross_up", "touch")
STRATEGY_MODES = ("paired", "oco")  # per-level; Mode A (paired) is the primary experiment


@dataclass(frozen=True, slots=True)
class AccountConfig:
    """Paper account and instrument assumptions. Costs are deliberately explicit."""

    starting_balance: Decimal = Decimal("100000")
    tick_size: Decimal = Decimal("0.25")
    tick_value: Decimal = Decimal("0.50")          # $ per tick per contract (MNQ)
    commission_per_contract: Decimal = Decimal("0.62")  # per side, per contract
    entry_slippage_ticks: Decimal = Decimal("0")   # adverse, added to the touch fill
    exit_slippage_ticks: Decimal = Decimal("0")    # adverse, on target/market exits
    stop_slippage_ticks: Decimal = Decimal("1")    # adverse, on stop-market fills
    max_gross_contracts: int = 100_000             # sanity cap on simulated exposure

    def __post_init__(self) -> None:
        if self.tick_size <= 0:
            raise ValueError("tick_size must be positive")
        if self.tick_value <= 0:
            raise ValueError("tick_value must be positive")
        for name in ("commission_per_contract", "entry_slippage_ticks",
                     "exit_slippage_ticks", "stop_slippage_ticks"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.max_gross_contracts <= 0:
            raise ValueError("max_gross_contracts must be positive")


@dataclass(frozen=True, slots=True)
class BreakEvenConfig:
    """Break-even stop management (all 0 = disabled)."""

    enabled: bool = False
    trigger_ticks: Decimal = Decimal("0")  # favourable move that arms break-even
    offset_ticks: Decimal = Decimal("0")   # where the stop locks vs entry (0 = entry)

    def __post_init__(self) -> None:
        if self.trigger_ticks < 0 or self.offset_ticks < 0:
            raise ValueError("break-even ticks must be non-negative")


@dataclass(frozen=True, slots=True)
class TrailConfig:
    """Trailing stop management (disabled unless enabled)."""

    enabled: bool = False
    immediate: bool = False                 # trail from entry, no activation gate
    activation_ticks: Decimal = Decimal("0")  # favourable move before trailing arms
    distance_ticks: Decimal = Decimal("0")    # how far the stop trails the best price
    step_ticks: Decimal = Decimal("0")        # only move the stop in >= step increments

    def __post_init__(self) -> None:
        for name in ("activation_ticks", "distance_ticks", "step_ticks"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.enabled and self.distance_ticks <= 0:
            raise ValueError("distance_ticks must be positive when trailing is enabled")


@dataclass(frozen=True, slots=True)
class ActivationSpec:
    """One pre-set activation level. Armed until price satisfies its condition.

    The level says WHEN to activate (price + source + comparison) and WHAT to
    create (long/short size, per-leg stop, break-even, trailing) plus re-arm
    rules so a level sitting exactly on price cannot fire every tick.
    """

    activation_id: str
    price: Decimal
    long_qty: int = 1
    short_qty: int = 1
    stop_ticks: Decimal = Decimal("2")
    source: str = "last"
    comparison: str = "auto"
    mode: str = "paired"
    break_even: BreakEvenConfig = field(default_factory=BreakEvenConfig)
    trailing: TrailConfig = field(default_factory=TrailConfig)
    enabled: bool = True
    one_shot: bool = True
    max_activations: int = 1
    rearm_cooldown_ns: int = 0
    require_leave_reenter: bool = False
    leave_distance_ticks: Decimal = Decimal("4")
    reenter_distance_ticks: Decimal = Decimal("0")  # 0 -> return to the level
    expire_after_ns: int = 0            # 0 = never expire
    cancel_if_moves_away_ticks: Decimal = Decimal("0")  # 0 = never cancel on distance

    def __post_init__(self) -> None:
        if self.price <= 0:
            raise ValueError("activation price must be positive")
        if self.long_qty < 0 or self.short_qty < 0:
            raise ValueError("quantities must be non-negative")
        if self.long_qty == 0 and self.short_qty == 0:
            raise ValueError("an activation must open at least one side")
        if self.stop_ticks <= 0:
            raise ValueError("stop_ticks must be positive (a tighter stop than one tick is impossible)")
        if self.source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}")
        if self.comparison not in COMPARISONS:
            raise ValueError(f"comparison must be one of {COMPARISONS}")
        if self.mode not in STRATEGY_MODES:
            raise ValueError(f"mode must be one of {STRATEGY_MODES}")
        if self.max_activations < 1:
            raise ValueError("max_activations must be >= 1")
        if self.rearm_cooldown_ns < 0 or self.expire_after_ns < 0:
            raise ValueError("time settings must be non-negative")
        for name in ("leave_distance_ticks", "reenter_distance_ticks", "cancel_if_moves_away_ticks"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")

    def with_id(self, activation_id: str) -> "ActivationSpec":
        """Return a copy with a new id (for duplication)."""
        return replace(self, activation_id=activation_id)
