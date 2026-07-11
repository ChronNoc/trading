"""Tests for formal strategy specification loading."""

from decimal import Decimal

import pytest

from app.strategy.spec import (
    ImportantLevel,
    load_strategy_spec,
    parse_strategy_spec_yaml,
)


FULLY_RESOLVED_SPEC = """
instrument:
  symbol: MNQ
  allowed_contracts: 2
session:
  timezone: America/Chicago
  allowed_start: "08:30"
  allowed_end: "15:00"
context_requirements:
  trend_condition: aligned_with_opening_drive
  important_levels:
    - prior_day_high
    - overnight_low
entry_setup:
  liquidity_minimum: 100
  aggressive_volume_minimum: 250.5
  maximum_price_progress_ticks: 8
  reclaim_ticks: 2
  confirmation_window_ms: 1500
risk:
  maximum_trades_per_day: 3
  daily_loss_fraction: "0.02"
  maximum_open_positions: 1
exit:
  stop_method: 6
  target_method: 12.5
  break_even_rule: 4
  time_stop_seconds: 300
"""


PARTIALLY_UNRESOLVED_SPEC = """
instrument:
  symbol: MNQ
  allowed_contracts: 1
session:
  timezone: America/Chicago
  allowed_start: "09:00"
  allowed_end: "11:30"
context_requirements:
  trend_condition: rejecting_prior_extreme
  important_levels:
    - prior_day_low
    - manually_defined_level
entry_setup:
  liquidity_minimum: unresolved
  aggressive_volume_minimum: 175
  maximum_price_progress_ticks: unresolved
  reclaim_ticks: 1.5
  confirmation_window_ms: 800
risk:
  maximum_trades_per_day: 2
  daily_loss_fraction: "0.01"
  allow_averaging_down: false
  maximum_open_positions: 1
exit:
  stop_method: unresolved
  target_method: 10
  break_even_rule: unresolved
  time_stop_seconds: 240
"""


def test_load_strategy_spec_accepts_fully_resolved_spec(tmp_path) -> None:
    """A fully resolved YAML file loads into typed section models."""
    spec_path = tmp_path / "strategy.yaml"
    spec_path.write_text(FULLY_RESOLVED_SPEC, encoding="utf-8")

    spec = load_strategy_spec(spec_path)

    assert spec.instrument.symbol == "MNQ"
    assert spec.instrument.allowed_contracts == 2
    assert spec.session.allowed_start == "08:30"
    assert spec.context_requirements.important_levels == [
        ImportantLevel.PRIOR_DAY_HIGH,
        ImportantLevel.OVERNIGHT_LOW,
    ]
    assert spec.entry_setup.aggressive_volume_minimum == Decimal("250.5")
    assert spec.risk.daily_loss_fraction == Decimal("0.02")
    assert spec.risk.allow_averaging_down is False
    assert spec.exit.target_method == Decimal("12.5")
    assert spec.unresolved_parameters == ()
    assert spec.list_unresolved_parameters() == ()


def test_parse_strategy_spec_tracks_unresolved_fields() -> None:
    """Unresolved values remain visible and are listed by dotted field path."""
    spec = parse_strategy_spec_yaml(PARTIALLY_UNRESOLVED_SPEC)

    assert spec.entry_setup.liquidity_minimum == "unresolved"
    assert spec.entry_setup.maximum_price_progress_ticks == "unresolved"
    assert spec.exit.stop_method == "unresolved"
    assert spec.exit.break_even_rule == "unresolved"
    assert spec.entry_setup.reclaim_ticks == Decimal("1.5")
    assert spec.unresolved_parameters == (
        "entry_setup.liquidity_minimum",
        "entry_setup.maximum_price_progress_ticks",
        "exit.stop_method",
        "exit.break_even_rule",
    )


def test_parse_strategy_spec_rejects_unknown_important_level() -> None:
    """Context important levels must be one of the formal enum values."""
    invalid_spec = PARTIALLY_UNRESOLVED_SPEC.replace(
        "manually_defined_level",
        "weekly_vwap",
    )

    with pytest.raises(ValueError):
        parse_strategy_spec_yaml(invalid_spec)
