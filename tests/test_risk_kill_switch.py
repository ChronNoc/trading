"""Tests for kill-switch decisions."""

from datetime import date
from decimal import Decimal

from app.risk.kill_switch import KillSwitchState, evaluate_kill_switch
from app.risk.limits import EntryLimitState, InstrumentConfig


def test_kill_switch_does_not_flatten_when_all_limits_pass() -> None:
    """The kill switch stays inactive when no rule is breached."""
    decision = evaluate_kill_switch(KillSwitchState(entry_limits=_passing_state()))

    assert decision.flatten_now is False
    assert "No kill-switch condition" in decision.reason


def test_kill_switch_flattens_for_manual_override() -> None:
    """The kill switch flattens immediately when manual override is true."""
    decision = evaluate_kill_switch(
        KillSwitchState(entry_limits=_passing_state(), manual_override_flatten=True),
    )

    assert decision.flatten_now is True
    assert "Manual override" in decision.reason


def test_kill_switch_flattens_for_failed_limit_rule() -> None:
    """The kill switch flattens when an underlying risk rule fails."""
    state = _passing_state(connection_healthy=False)

    decision = evaluate_kill_switch(KillSwitchState(entry_limits=state))

    assert decision.flatten_now is True
    assert "Connection health flag is false" in decision.reason


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
