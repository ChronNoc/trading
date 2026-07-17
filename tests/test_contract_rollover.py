"""MNQ rollover: computed from the expiry rule, never a hardcoded table.

The defect these guard: ``current_contract_for_date`` raised ``ValueError`` for
any date outside a hand-maintained table that ended on 2027-03-11. A trading app
that throws on an ordinary date once a list runs out is a time bomb, and the
crash would surface far from its cause.

Alias-resolution behaviour is covered in ``tests/test_automatic_runtime.py``.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from app.market.contract_resolver import (
    DEFAULT_CONTRACT_SCHEDULE,
    ContractPeriod,
    ContractResolver,
    contract_symbol,
    front_contract_period,
    third_friday,
)


def test_computed_rule_reproduces_every_hand_maintained_period() -> None:
    """The rule must match the hand-verified schedule exactly, or it is wrong."""
    for period in DEFAULT_CONTRACT_SCHEDULE:
        computed = front_contract_period(period.start_date)
        assert computed.symbol == period.symbol, f"{period.symbol}: computed {computed.symbol}"
        assert computed.end_date == period.end_date, f"{period.symbol} end date"
        assert front_contract_period(period.end_date).symbol == period.symbol, (
            "the last day of the period is still the front contract"
        )


def test_expiry_is_the_third_friday_of_the_quarterly_month() -> None:
    assert third_friday(2026, 3) == date(2026, 3, 20)
    assert third_friday(2026, 6) == date(2026, 6, 19)
    assert third_friday(2026, 9) == date(2026, 9, 18)
    assert third_friday(2026, 12) == date(2026, 12, 18)
    # A month that starts ON a Friday must not return the second Friday.
    assert third_friday(2026, 5) == date(2026, 5, 15)
    assert third_friday(2027, 1) == date(2027, 1, 15)


def test_resolution_never_raises_after_the_table_runs_out() -> None:
    """Regression: the app used to throw on any date past 2027-03-11."""
    resolver = ContractResolver()
    assert resolver.current_contract_for_date(date(2031, 7, 14)) == "MNQU1"
    assert resolver.current_contract_for_date(date(2029, 1, 3)) == "MNQH9"


def test_every_day_across_years_resolves_to_a_real_contract() -> None:
    """No gap and no overlap: each date has exactly one front contract."""
    resolver = ContractResolver()
    day = date(2027, 1, 1)
    while day < date(2033, 1, 1):
        symbol = resolver.current_contract_for_date(day)
        assert re.fullmatch(r"MNQ[HMUZ]\d", symbol), f"{day}: bad symbol {symbol}"
        period = resolver.current_period_for_date(day)
        assert period.start_date <= day <= period.end_date, f"{day} outside {period}"
        day += timedelta(days=1)


def test_the_roll_happens_eight_days_before_expiry() -> None:
    resolver = ContractResolver()
    # MNQU6 expires 2026-09-18, so the roll to MNQZ6 lands on 2026-09-11.
    assert resolver.current_contract_for_date(date(2026, 9, 10)) == "MNQU6"
    assert resolver.current_contract_for_date(date(2026, 9, 11)) == "MNQZ6"


def test_a_configured_period_overrides_the_computed_rule() -> None:
    """An operator must be able to override an unusual roll."""
    override = ContractPeriod("MNQZ9", date(2026, 9, 1), date(2026, 9, 30))
    resolver = ContractResolver(schedule=(override,))
    assert resolver.current_contract_for_date(date(2026, 9, 15)) == "MNQZ9"
    # Outside the override the computed rule still applies.
    assert resolver.current_contract_for_date(date(2026, 7, 1)) == "MNQU6"


def test_days_until_rollover_counts_down_to_the_last_day() -> None:
    resolver = ContractResolver(schedule=())
    assert resolver.days_until_rollover(date(2026, 9, 10)) == 0   # last day of MNQU6
    assert resolver.days_until_rollover(date(2026, 9, 3)) == 7
    assert resolver.days_until_rollover(date(2026, 9, 11)) >= 0   # first day of MNQZ6


def test_year_code_wraps_correctly_into_the_next_decade() -> None:
    assert contract_symbol(2026, 9) == "MNQU6"
    assert contract_symbol(2030, 3) == "MNQH0"
    assert contract_symbol(2031, 12) == "MNQZ1"


def test_periods_are_contiguous_with_no_gap_between_contracts() -> None:
    """A one-day gap would leave a trading day with no front contract."""
    period = front_contract_period(date(2028, 2, 1))
    following = front_contract_period(period.end_date + timedelta(days=1))
    assert following.start_date == period.end_date + timedelta(days=1)
    assert following.symbol != period.symbol
