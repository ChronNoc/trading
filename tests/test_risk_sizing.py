"""Tests for Decimal-based risk sizing."""

from decimal import Decimal

import pytest

from app.risk.sizing import (
    SizingInputs,
    calculate_contracts,
    calculate_daily_risk_budget,
    calculate_drawdown_based_risk,
    calculate_max_risk_per_trade,
    calculate_position_size,
    calculate_risk_per_contract,
)


def test_calculate_daily_risk_budget_uses_one_percent_when_smaller() -> None:
    """Daily risk budget is capped by one percent of account size."""
    result = calculate_daily_risk_budget(
        account_size=Decimal("10000"),
        remaining_allowable_drawdown=Decimal("10000"),
    )

    assert result == Decimal("100.00")


def test_calculate_daily_risk_budget_uses_drawdown_when_smaller() -> None:
    """Daily risk budget is capped by drawdown-based risk when it is smaller."""
    result = calculate_daily_risk_budget(
        account_size=Decimal("50000"),
        remaining_allowable_drawdown=Decimal("600"),
    )

    assert result == Decimal("90.00")


def test_calculate_drawdown_based_risk_uses_configurable_safety_fraction() -> None:
    """Drawdown-based risk uses the caller-provided safety fraction."""
    result = calculate_drawdown_based_risk(
        remaining_allowable_drawdown=Decimal("1000"),
        safety_fraction=Decimal("0.10"),
    )

    assert result == Decimal("100.00")


def test_calculate_max_risk_per_trade_divides_daily_budget_by_three() -> None:
    """Per-trade budget is one third of the daily risk budget."""
    result = calculate_max_risk_per_trade(Decimal("99"))

    assert result == Decimal("33")


def test_calculate_risk_per_contract_uses_tick_value_commission_and_slippage() -> None:
    """Contract risk is stop ticks times tick value plus costs."""
    result = calculate_risk_per_contract(
        stop_distance_ticks=Decimal("20"),
        tick_value=Decimal("0.50"),
        estimated_commission=Decimal("1.25"),
        estimated_slippage=Decimal("0.75"),
    )

    assert result == Decimal("12.00")


def test_calculate_contracts_floors_fractional_quantity() -> None:
    """Contract count is floored to a whole number."""
    result = calculate_contracts(
        max_risk_per_trade=Decimal("35"),
        risk_per_contract=Decimal("12"),
    )

    assert result == 2


def test_calculate_position_size_returns_complete_sizing_result() -> None:
    """The composed sizing function returns budgets, risk, and contracts."""
    result = calculate_position_size(
        SizingInputs(
            account_size=Decimal("10000"),
            remaining_allowable_drawdown=Decimal("600"),
            stop_distance_ticks=Decimal("10"),
            tick_value=Decimal("0.50"),
            estimated_commission=Decimal("1"),
            estimated_slippage=Decimal("1"),
        ),
    )

    assert result.daily_risk_budget == Decimal("90.00")
    assert result.max_risk_per_trade == Decimal("30.00")
    assert result.risk_per_contract == Decimal("7.00")
    assert result.contracts == 4


def test_calculate_risk_per_contract_rejects_unknown_or_zero_stop_distance() -> None:
    """A non-positive stop distance cannot produce a valid risk per contract."""
    with pytest.raises(ValueError, match="stop_distance_ticks"):
        calculate_risk_per_contract(
            stop_distance_ticks=Decimal("0"),
            tick_value=Decimal("0.50"),
            estimated_commission=Decimal("1"),
            estimated_slippage=Decimal("1"),
        )
