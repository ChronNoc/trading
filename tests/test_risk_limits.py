"""Tests for individual risk limit rules."""

from datetime import date
from decimal import Decimal

from app.risk.limits import (
    EntryLimitState,
    InstrumentConfig,
    enforce_account_synchronized,
    enforce_connection_health,
    enforce_daily_lock,
    enforce_daily_loss_budget,
    enforce_entry_count_limit,
    enforce_known_stop_distance,
    enforce_losing_trade_limit,
    enforce_no_averaging_into_losing_position,
    enforce_no_pending_cancellation,
    enforce_one_open_position_maximum,
    enforce_per_trade_loss_budget,
    enforce_single_mnq_instrument_config,
    evaluate_entry_limits,
    summarize_entry_permission,
)


def test_single_mnq_instrument_config_rule_passes_for_one_mnq_config() -> None:
    """The instrument rule passes for exactly one MNQ config."""
    result = enforce_single_mnq_instrument_config((InstrumentConfig(symbol="MNQ"),))

    assert result.allowed is True


def test_single_mnq_instrument_config_rule_rejects_wrong_or_multiple_configs() -> None:
    """The instrument rule rejects non-MNQ or multiple configs."""
    wrong_symbol = enforce_single_mnq_instrument_config((InstrumentConfig(symbol="ES"),))
    multiple_configs = enforce_single_mnq_instrument_config(
        (InstrumentConfig(symbol="MNQ"), InstrumentConfig(symbol="MES")),
    )

    assert wrong_symbol.allowed is False
    assert "only MNQ" in wrong_symbol.reason
    assert multiple_configs.allowed is False
    assert "exactly one" in multiple_configs.reason


def test_one_open_position_rule_passes_at_one_position() -> None:
    """The open-position rule passes when one position is open."""
    result = enforce_one_open_position_maximum(open_positions=1)

    assert result.allowed is True


def test_one_open_position_rule_rejects_more_than_one_position() -> None:
    """The open-position rule rejects more than one open position."""
    result = enforce_one_open_position_maximum(open_positions=2)

    assert result.allowed is False
    assert "exceeds maximum" in result.reason


def test_losing_trade_rule_passes_below_three_losses() -> None:
    """The losing-trade rule passes below the daily maximum."""
    result = enforce_losing_trade_limit(losing_trades_today=2)

    assert result.allowed is True


def test_losing_trade_rule_rejects_at_three_losses() -> None:
    """The losing-trade rule rejects when the daily maximum is hit."""
    result = enforce_losing_trade_limit(losing_trades_today=3)

    assert result.allowed is False
    assert "daily maximum 3" in result.reason


def test_entry_count_rule_passes_below_configured_limit() -> None:
    """The entry-count rule passes below the configured maximum."""
    result = enforce_entry_count_limit(entries_today=4, maximum_entries_per_day=5)

    assert result.allowed is True


def test_entry_count_rule_rejects_at_configured_limit() -> None:
    """The entry-count rule rejects when the configured maximum is hit."""
    result = enforce_entry_count_limit(entries_today=5, maximum_entries_per_day=5)

    assert result.allowed is False
    assert "daily maximum 5" in result.reason


def test_daily_loss_budget_rule_passes_below_budget() -> None:
    """The daily-loss rule passes below the risk budget."""
    result = enforce_daily_loss_budget(
        daily_realized_loss=Decimal("99"),
        daily_risk_budget=Decimal("100"),
    )

    assert result.allowed is True


def test_daily_loss_budget_rule_rejects_when_budget_is_hit() -> None:
    """The daily-loss rule rejects once the risk budget is hit."""
    result = enforce_daily_loss_budget(
        daily_realized_loss=Decimal("100"),
        daily_risk_budget=Decimal("100"),
    )

    assert result.allowed is False
    assert "has hit budget" in result.reason


def test_per_trade_loss_budget_rule_passes_at_budget() -> None:
    """The per-trade rule passes when proposed risk is at budget."""
    result = enforce_per_trade_loss_budget(
        proposed_trade_risk=Decimal("25"),
        max_risk_per_trade=Decimal("25"),
    )

    assert result.allowed is True


def test_per_trade_loss_budget_rule_rejects_above_budget() -> None:
    """The per-trade rule rejects when proposed risk exceeds budget."""
    result = enforce_per_trade_loss_budget(
        proposed_trade_risk=Decimal("26"),
        max_risk_per_trade=Decimal("25"),
    )

    assert result.allowed is False
    assert "exceeds per-trade budget" in result.reason


def test_no_averaging_rule_passes_when_not_adding_to_loser() -> None:
    """The averaging rule passes when the entry is not adding to a loser."""
    result = enforce_no_averaging_into_losing_position(
        has_open_losing_position=True,
        adds_to_existing_position=False,
    )

    assert result.allowed is True


def test_no_averaging_rule_rejects_adding_to_losing_position() -> None:
    """The averaging rule rejects adding size to a losing position."""
    result = enforce_no_averaging_into_losing_position(
        has_open_losing_position=True,
        adds_to_existing_position=True,
    )

    assert result.allowed is False
    assert "Averaging" in result.reason


def test_known_stop_distance_rule_passes_for_positive_stop() -> None:
    """The stop-distance rule passes when a positive stop distance is known."""
    result = enforce_known_stop_distance(stop_distance_ticks=Decimal("8"))

    assert result.allowed is True


def test_known_stop_distance_rule_rejects_unknown_stop() -> None:
    """The stop-distance rule rejects an unknown stop distance."""
    result = enforce_known_stop_distance(stop_distance_ticks=None)

    assert result.allowed is False
    assert "unknown" in result.reason


def test_connection_health_rule_passes_when_healthy() -> None:
    """The connection-health rule passes when the flag is true."""
    result = enforce_connection_health(connection_healthy=True)

    assert result.allowed is True


def test_connection_health_rule_rejects_when_unhealthy() -> None:
    """The connection-health rule rejects when the flag is false."""
    result = enforce_connection_health(connection_healthy=False)

    assert result.allowed is False
    assert "false" in result.reason


def test_account_synchronized_rule_passes_when_synchronized() -> None:
    """The account-sync rule passes when account state is synchronized."""
    result = enforce_account_synchronized(account_synchronized=True)

    assert result.allowed is True


def test_account_synchronized_rule_rejects_when_not_synchronized() -> None:
    """The account-sync rule rejects unsynchronized account state."""
    result = enforce_account_synchronized(account_synchronized=False)

    assert result.allowed is False
    assert "not synchronized" in result.reason


def test_pending_cancellation_rule_passes_when_no_cancellation_is_pending() -> None:
    """The cancellation rule passes when no unresolved cancellation is pending."""
    result = enforce_no_pending_cancellation(unresolved_order_cancellation_pending=False)

    assert result.allowed is True


def test_pending_cancellation_rule_rejects_pending_cancellation() -> None:
    """The cancellation rule rejects while a cancellation is unresolved."""
    result = enforce_no_pending_cancellation(unresolved_order_cancellation_pending=True)

    assert result.allowed is False
    assert "pending" in result.reason


def test_daily_lock_rule_passes_for_prior_lock_date() -> None:
    """The daily-lock rule passes when the lock is from a prior day."""
    result = enforce_daily_lock(
        current_date=date(2026, 7, 10),
        daily_lock_date=date(2026, 7, 9),
    )

    assert result.allowed is True


def test_daily_lock_rule_rejects_current_lock_date() -> None:
    """The daily-lock rule rejects when the lock date is the current date."""
    result = enforce_daily_lock(
        current_date=date(2026, 7, 10),
        daily_lock_date=date(2026, 7, 10),
    )

    assert result.allowed is False
    assert "locked" in result.reason


def test_evaluate_entry_limits_returns_one_result_per_rule() -> None:
    """The aggregate limit evaluator preserves individual rule results."""
    results = evaluate_entry_limits(_passing_state())

    assert len(results) == 12
    assert all(result.allowed for result in results)


def test_summarize_entry_permission_joins_rejection_reasons() -> None:
    """The summary decision returns GUI-ready rejection text."""
    state = _passing_state(connection_healthy=False, account_synchronized=False)

    result = summarize_entry_permission(state)

    assert result.allowed is False
    assert "Connection health flag is false" in result.reason
    assert "Account state is not synchronized" in result.reason


def _passing_state(**overrides: object) -> EntryLimitState:
    values = {
        "instrument_configs": (InstrumentConfig(symbol="MNQ"),),
        "open_positions": 1,
        "losing_trades_today": 0,
        "entries_today": 0,
        "daily_realized_loss": Decimal("0"),
        "daily_risk_budget": Decimal("90"),
        "proposed_trade_risk": Decimal("20"),
        "max_risk_per_trade": Decimal("30"),
        "has_open_losing_position": False,
        "adds_to_existing_position": False,
        "stop_distance_ticks": Decimal("8"),
        "connection_healthy": True,
        "account_synchronized": True,
        "unresolved_order_cancellation_pending": False,
        "current_date": date(2026, 7, 10),
        "daily_lock_date": None,
        "maximum_entries_per_day": 3,
    }
    values.update(overrides)
    return EntryLimitState(**values)  # type: ignore[arg-type]
