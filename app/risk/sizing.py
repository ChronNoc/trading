"""Decimal-based risk sizing for MNQ futures entries."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR

DEFAULT_SAFETY_FRACTION = Decimal("0.15")
DEFAULT_TRADES_PER_DAILY_BUDGET = Decimal("3")


@dataclass(frozen=True, slots=True)
class SizingInputs:
    """Inputs required to calculate risk budget and contract quantity."""

    account_size: Decimal
    remaining_allowable_drawdown: Decimal
    stop_distance_ticks: Decimal
    tick_value: Decimal
    estimated_commission: Decimal = Decimal("0")
    estimated_slippage: Decimal = Decimal("0")
    safety_fraction: Decimal = DEFAULT_SAFETY_FRACTION


@dataclass(frozen=True, slots=True)
class SizingResult:
    """Calculated risk budgets and contract quantity."""

    daily_risk_budget: Decimal
    max_risk_per_trade: Decimal
    risk_per_contract: Decimal
    contracts: int


def calculate_drawdown_based_risk(
    remaining_allowable_drawdown: Decimal,
    safety_fraction: Decimal = DEFAULT_SAFETY_FRACTION,
) -> Decimal:
    """Calculate the drawdown-limited daily risk budget component."""
    _require_non_negative("remaining_allowable_drawdown", remaining_allowable_drawdown)
    _require_positive("safety_fraction", safety_fraction)
    return remaining_allowable_drawdown * safety_fraction


def calculate_daily_risk_budget(
    account_size: Decimal,
    remaining_allowable_drawdown: Decimal,
    safety_fraction: Decimal = DEFAULT_SAFETY_FRACTION,
) -> Decimal:
    """Calculate the daily risk budget from account size and remaining drawdown."""
    _require_non_negative("account_size", account_size)
    drawdown_based_risk = calculate_drawdown_based_risk(
        remaining_allowable_drawdown,
        safety_fraction,
    )
    one_percent_account_risk = account_size * Decimal("0.01")
    return min(one_percent_account_risk, drawdown_based_risk)


def calculate_max_risk_per_trade(daily_risk_budget: Decimal) -> Decimal:
    """Calculate the maximum risk allowed for one trade."""
    _require_non_negative("daily_risk_budget", daily_risk_budget)
    return daily_risk_budget / DEFAULT_TRADES_PER_DAILY_BUDGET


def calculate_risk_per_contract(
    stop_distance_ticks: Decimal,
    tick_value: Decimal,
    estimated_commission: Decimal,
    estimated_slippage: Decimal,
) -> Decimal:
    """Calculate total estimated risk for one contract."""
    _require_positive("stop_distance_ticks", stop_distance_ticks)
    _require_positive("tick_value", tick_value)
    _require_non_negative("estimated_commission", estimated_commission)
    _require_non_negative("estimated_slippage", estimated_slippage)
    return (stop_distance_ticks * tick_value) + estimated_commission + estimated_slippage


def calculate_contracts(max_risk_per_trade: Decimal, risk_per_contract: Decimal) -> int:
    """Calculate the floored number of contracts allowed by risk."""
    _require_non_negative("max_risk_per_trade", max_risk_per_trade)
    _require_positive("risk_per_contract", risk_per_contract)
    return int((max_risk_per_trade / risk_per_contract).to_integral_value(rounding=ROUND_FLOOR))


def calculate_position_size(inputs: SizingInputs) -> SizingResult:
    """Calculate all risk budgets and the allowed contract quantity."""
    daily_risk_budget = calculate_daily_risk_budget(
        account_size=inputs.account_size,
        remaining_allowable_drawdown=inputs.remaining_allowable_drawdown,
        safety_fraction=inputs.safety_fraction,
    )
    max_risk_per_trade = calculate_max_risk_per_trade(daily_risk_budget)
    risk_per_contract = calculate_risk_per_contract(
        stop_distance_ticks=inputs.stop_distance_ticks,
        tick_value=inputs.tick_value,
        estimated_commission=inputs.estimated_commission,
        estimated_slippage=inputs.estimated_slippage,
    )
    contracts = calculate_contracts(max_risk_per_trade, risk_per_contract)
    return SizingResult(
        daily_risk_budget=daily_risk_budget,
        max_risk_per_trade=max_risk_per_trade,
        risk_per_contract=risk_per_contract,
        contracts=contracts,
    )


def _require_non_negative(name: str, value: Decimal) -> None:
    if value < Decimal("0"):
        raise ValueError(f"{name} must be non-negative")


def _require_positive(name: str, value: Decimal) -> None:
    if value <= Decimal("0"):
        raise ValueError(f"{name} must be greater than 0")
