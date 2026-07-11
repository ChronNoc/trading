"""Individually testable risk limit checks for entry decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Sequence

MNQ_SYMBOL = "MNQ"
DEFAULT_MAXIMUM_OPEN_POSITIONS = 1
DEFAULT_MAXIMUM_LOSING_TRADES_PER_DAY = 3
DEFAULT_MAXIMUM_ENTRIES_PER_DAY = 3


@dataclass(frozen=True, slots=True)
class RuleResult:
    """Result of one risk rule evaluation."""

    allowed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class InstrumentConfig:
    """Configured instrument identity for risk validation."""

    symbol: str


@dataclass(frozen=True, slots=True)
class EntryLimitState:
    """Complete state required to evaluate whether a new entry is allowed."""

    instrument_configs: tuple[InstrumentConfig, ...]
    open_positions: int
    losing_trades_today: int
    entries_today: int
    daily_realized_loss: Decimal
    daily_risk_budget: Decimal
    proposed_trade_risk: Decimal
    max_risk_per_trade: Decimal
    has_open_losing_position: bool
    adds_to_existing_position: bool
    stop_distance_ticks: Decimal | None
    connection_healthy: bool
    account_synchronized: bool
    unresolved_order_cancellation_pending: bool
    current_date: date
    daily_lock_date: date | None = None
    maximum_entries_per_day: int = DEFAULT_MAXIMUM_ENTRIES_PER_DAY


def enforce_single_mnq_instrument_config(
    instrument_configs: Sequence[InstrumentConfig],
    allowed_symbol: str = MNQ_SYMBOL,
) -> RuleResult:
    """Allow exactly one configured instrument, and require it to be MNQ."""
    if len(instrument_configs) != 1:
        return _reject(f"Expected exactly one instrument config for {allowed_symbol}.")

    configured_symbol = instrument_configs[0].symbol
    if configured_symbol != allowed_symbol:
        return _reject(f"Instrument {configured_symbol} is not allowed; only {allowed_symbol} is allowed.")

    return _allow(f"Instrument config is limited to {allowed_symbol}.")


def enforce_one_open_position_maximum(
    open_positions: int,
    maximum_open_positions: int = DEFAULT_MAXIMUM_OPEN_POSITIONS,
) -> RuleResult:
    """Allow no more than one open position."""
    if open_positions > maximum_open_positions:
        return _reject(
            f"Open position count {open_positions} exceeds maximum {maximum_open_positions}.",
        )

    return _allow("Open position count is within the maximum.")


def enforce_losing_trade_limit(
    losing_trades_today: int,
    maximum_losing_trades: int = DEFAULT_MAXIMUM_LOSING_TRADES_PER_DAY,
) -> RuleResult:
    """Reject new entries once the daily losing-trade limit has been hit."""
    if losing_trades_today >= maximum_losing_trades:
        return _reject(
            f"Losing trade count {losing_trades_today} has hit the daily maximum {maximum_losing_trades}.",
        )

    return _allow("Daily losing-trade count is below the maximum.")


def enforce_entry_count_limit(
    entries_today: int,
    maximum_entries_per_day: int = DEFAULT_MAXIMUM_ENTRIES_PER_DAY,
) -> RuleResult:
    """Reject new entries once the daily entry-count limit has been hit."""
    if entries_today >= maximum_entries_per_day:
        return _reject(
            f"Entry count {entries_today} has hit the daily maximum {maximum_entries_per_day}.",
        )

    return _allow("Daily entry count is below the maximum.")


def enforce_daily_loss_budget(daily_realized_loss: Decimal, daily_risk_budget: Decimal) -> RuleResult:
    """Reject new entries once realized daily loss reaches the daily risk budget."""
    if daily_realized_loss < Decimal("0"):
        return _reject("Daily realized loss must be a non-negative Decimal.")
    if daily_risk_budget <= Decimal("0"):
        return _reject("Daily risk budget must be greater than 0.")
    if daily_realized_loss >= daily_risk_budget:
        return _reject(
            f"Daily realized loss {daily_realized_loss} has hit budget {daily_risk_budget}.",
        )

    return _allow("Daily realized loss is below the risk budget.")


def enforce_per_trade_loss_budget(
    proposed_trade_risk: Decimal,
    max_risk_per_trade: Decimal,
) -> RuleResult:
    """Reject entries whose estimated loss exceeds the per-trade risk budget."""
    if proposed_trade_risk <= Decimal("0"):
        return _reject("Proposed trade risk must be greater than 0.")
    if max_risk_per_trade <= Decimal("0"):
        return _reject("Per-trade risk budget must be greater than 0.")
    if proposed_trade_risk > max_risk_per_trade:
        return _reject(
            f"Proposed trade risk {proposed_trade_risk} exceeds per-trade budget {max_risk_per_trade}.",
        )

    return _allow("Proposed trade risk is within the per-trade budget.")


def enforce_no_averaging_into_losing_position(
    has_open_losing_position: bool,
    adds_to_existing_position: bool,
) -> RuleResult:
    """Reject entries that would add size to an already losing position."""
    if has_open_losing_position and adds_to_existing_position:
        return _reject("Averaging into a losing position is not allowed.")

    return _allow("Entry is not averaging into a losing position.")


def enforce_known_stop_distance(stop_distance_ticks: Decimal | None) -> RuleResult:
    """Reject entries when the stop distance is unknown or invalid."""
    if stop_distance_ticks is None:
        return _reject("Stop distance is unknown.")
    if stop_distance_ticks <= Decimal("0"):
        return _reject("Stop distance must be greater than 0 ticks.")

    return _allow("Stop distance is known.")


def enforce_connection_health(connection_healthy: bool) -> RuleResult:
    """Reject entries when the connection health flag is false."""
    if not connection_healthy:
        return _reject("Connection health flag is false.")

    return _allow("Connection health flag is true.")


def enforce_account_synchronized(account_synchronized: bool) -> RuleResult:
    """Reject entries when account state is not synchronized."""
    if not account_synchronized:
        return _reject("Account state is not synchronized.")

    return _allow("Account state is synchronized.")


def enforce_no_pending_cancellation(unresolved_order_cancellation_pending: bool) -> RuleResult:
    """Reject entries while an unresolved order cancellation is pending."""
    if unresolved_order_cancellation_pending:
        return _reject("Unresolved order cancellation is pending.")

    return _allow("No unresolved order cancellation is pending.")


def enforce_daily_lock(current_date: date, daily_lock_date: date | None) -> RuleResult:
    """Reject entries when the stored daily lock date matches the current trading date."""
    if daily_lock_date == current_date:
        return _reject(f"Trading is locked for {current_date.isoformat()}.")

    return _allow("No daily lock is active for the current trading date.")


def evaluate_entry_limits(state: EntryLimitState) -> tuple[RuleResult, ...]:
    """Evaluate every entry limit and return each individual rule result."""
    return (
        enforce_single_mnq_instrument_config(state.instrument_configs),
        enforce_one_open_position_maximum(state.open_positions),
        enforce_losing_trade_limit(state.losing_trades_today),
        enforce_entry_count_limit(state.entries_today, state.maximum_entries_per_day),
        enforce_daily_loss_budget(state.daily_realized_loss, state.daily_risk_budget),
        enforce_per_trade_loss_budget(state.proposed_trade_risk, state.max_risk_per_trade),
        enforce_no_averaging_into_losing_position(
            state.has_open_losing_position,
            state.adds_to_existing_position,
        ),
        enforce_known_stop_distance(state.stop_distance_ticks),
        enforce_connection_health(state.connection_healthy),
        enforce_account_synchronized(state.account_synchronized),
        enforce_no_pending_cancellation(state.unresolved_order_cancellation_pending),
        enforce_daily_lock(state.current_date, state.daily_lock_date),
    )


def summarize_entry_permission(state: EntryLimitState) -> RuleResult:
    """Return a single entry decision with all rejection reasons joined for the GUI."""
    results = evaluate_entry_limits(state)
    rejections = tuple(result.reason for result in results if not result.allowed)
    if rejections:
        return _reject("; ".join(rejections))

    return _allow("All entry risk limits passed.")


def _allow(reason: str) -> RuleResult:
    return RuleResult(allowed=True, reason=reason)


def _reject(reason: str) -> RuleResult:
    return RuleResult(allowed=False, reason=reason)
