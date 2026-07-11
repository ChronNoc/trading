"""Tests for simulator slippage calculations."""

from decimal import Decimal

import pytest

from app.simulator.slippage import SlippageModel, apply_slippage, calculate_slippage


def test_calculate_slippage_combines_fixed_ticks_and_volatility() -> None:
    """Slippage is fixed ticks times tick size plus volatility-scaled price movement."""
    model = SlippageModel(
        tick_size=Decimal("0.25"),
        fixed_ticks=Decimal("2"),
        volatility_multiplier=Decimal("2"),
    )

    assert calculate_slippage(model, Decimal("0.10")) == Decimal("0.70")


def test_apply_slippage_worsens_buy_and_sell_prices() -> None:
    """Buy fills move up and sell fills move down by the modeled slippage."""
    model = SlippageModel(
        tick_size=Decimal("0.25"),
        fixed_ticks=Decimal("1"),
        volatility_multiplier=Decimal("0"),
    )

    assert apply_slippage(Decimal("100.00"), "buy", model) == Decimal("100.25")
    assert apply_slippage(Decimal("100.00"), "sell", model) == Decimal("99.75")


def test_calculate_slippage_rejects_negative_volatility() -> None:
    """Volatility input must be non-negative."""
    model = SlippageModel(tick_size=Decimal("0.25"))

    with pytest.raises(ValueError, match="short_term_volatility"):
        calculate_slippage(model, Decimal("-0.01"))
