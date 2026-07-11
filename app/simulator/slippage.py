"""Configurable Decimal slippage model for simulated fills."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, TypeAlias

OrderSide: TypeAlias = Literal["buy", "sell"]


@dataclass(frozen=True, slots=True)
class SlippageModel:
    """Fixed-tick plus volatility-scaled slippage configuration."""

    tick_size: Decimal
    fixed_ticks: Decimal = Decimal("0")
    volatility_multiplier: Decimal = Decimal("0")


def calculate_slippage(
    model: SlippageModel,
    short_term_volatility: Decimal = Decimal("0"),
) -> Decimal:
    """Calculate price slippage from fixed ticks and volatility."""
    _require_non_negative("fixed_ticks", model.fixed_ticks)
    _require_non_negative("volatility_multiplier", model.volatility_multiplier)
    _require_positive("tick_size", model.tick_size)
    _require_non_negative("short_term_volatility", short_term_volatility)
    return (model.fixed_ticks * model.tick_size) + (
        short_term_volatility * model.volatility_multiplier
    )


def apply_slippage(
    price: Decimal,
    side: OrderSide,
    model: SlippageModel,
    short_term_volatility: Decimal = Decimal("0"),
) -> Decimal:
    """Apply directional slippage to a simulated fill price."""
    slippage = calculate_slippage(model, short_term_volatility)
    if side == "buy":
        return price + slippage
    if side == "sell":
        return price - slippage

    raise ValueError("side must be buy or sell")


def _require_non_negative(name: str, value: Decimal) -> None:
    if value < Decimal("0"):
        raise ValueError(f"{name} must be non-negative")


def _require_positive(name: str, value: Decimal) -> None:
    if value <= Decimal("0"):
        raise ValueError(f"{name} must be greater than 0")
