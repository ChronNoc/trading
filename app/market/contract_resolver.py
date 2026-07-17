"""MNQ contract alias resolution and rollover checks.

Rollover is COMPUTED, not tabulated. MNQ expires on the third Friday of March,
June, September and December, and this project treats the front contract's last
day as eight days before that expiry.

That rule is not a guess: it reproduces every period of the hand-maintained
schedule below exactly (asserted in ``tests/test_contract_resolver.py``). The
table is kept as a regression fixture, but resolution no longer depends on it.

Why this matters: the table previously ended on 2027-03-11 and
``current_contract_for_date`` raised ``ValueError`` outside it. A trading app
that throws on an ordinary date once a hardcoded list runs out is a time bomb,
and the failure would land far from its cause.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

MNQ_ROOT = "MNQ"
MONTH_CODES = {"H": 3, "M": 6, "U": 9, "Z": 12}
# Month number -> CME month code, for computing a contract from a date.
CODE_FOR_MONTH = {month: code for code, month in MONTH_CODES.items()}
QUARTERLY_MONTHS = (3, 6, 9, 12)
# Days before expiry that liquidity moves to the next contract.
ROLL_DAYS_BEFORE_EXPIRY = 8


def third_friday(year: int, month: int) -> date:
    """Return the third Friday of ``month`` — the MNQ expiry date."""
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 14)


def contract_symbol(year: int, month: int) -> str:
    """Return the CME symbol for a quarterly contract, e.g. (2026, 9) -> MNQU6."""
    return f"{MNQ_ROOT}{CODE_FOR_MONTH[month]}{year % 10}"


def front_contract_period(trading_date: date) -> ContractPeriod:
    """Return the front-month contract period covering ``trading_date``.

    Deterministic for ANY date: the front contract is the first quarterly
    contract whose roll date has not yet passed.
    """
    for year in (trading_date.year, trading_date.year + 1):
        for month in QUARTERLY_MONTHS:
            last_day = third_friday(year, month) - timedelta(days=ROLL_DAYS_BEFORE_EXPIRY)
            if last_day < trading_date:
                continue
            previous = _previous_quarter(year, month)
            start = (third_friday(*previous) - timedelta(days=ROLL_DAYS_BEFORE_EXPIRY - 1))
            return ContractPeriod(contract_symbol(year, month), start, last_day)
    raise ValueError(f"no MNQ contract could be computed for {trading_date.isoformat()}")


def _previous_quarter(year: int, month: int) -> tuple[int, int]:
    index = QUARTERLY_MONTHS.index(month)
    return (year - 1, QUARTERLY_MONTHS[-1]) if index == 0 else (year, QUARTERLY_MONTHS[index - 1])


@dataclass(frozen=True, slots=True)
class ContractPeriod:
    """One configured front-contract period."""

    symbol: str
    start_date: date
    end_date: date


@dataclass(frozen=True, slots=True)
class ContractResolution:
    """Resolved contract status for GUI display and decision gating."""

    alias: str
    normalized_symbol: str | None
    display_symbol: str
    is_mnq: bool
    is_current_contract: bool
    decisions_allowed: bool
    requires_attention: bool
    reason: str


@dataclass(slots=True)
class ContractResolver:
    """Resolve Bookmap aliases to the current MNQ contract."""

    schedule: tuple[ContractPeriod, ...] = field(default_factory=lambda: DEFAULT_CONTRACT_SCHEDULE)
    active_contracts: set[str] = field(default_factory=set)

    def current_contract_for_date(self, trading_date: date) -> str:
        """Return the expected current MNQ contract for ``trading_date``.

        An explicitly configured period always wins, so an operator can override
        an unusual roll. Otherwise the contract is computed from the expiry rule,
        which is defined for every date - this never raises.
        """
        for period in self.schedule:
            if period.start_date <= trading_date <= period.end_date:
                return period.symbol
        return front_contract_period(trading_date).symbol

    def days_until_rollover(self, trading_date: date) -> int:
        """Return days until the front contract stops being front (0 = last day).

        Negative is impossible: the period returned always covers the date.
        """
        return (self.current_period_for_date(trading_date).end_date - trading_date).days

    def current_period_for_date(self, trading_date: date) -> ContractPeriod:
        """Return the full front-contract period covering ``trading_date``."""
        for period in self.schedule:
            if period.start_date <= trading_date <= period.end_date:
                return period
        return front_contract_period(trading_date)

    def resolve_alias(self, alias: str, trading_date: date) -> ContractResolution:
        """Resolve one Bookmap alias and return whether decisions are allowed."""
        normalized_symbol = normalize_mnq_alias(alias)
        if normalized_symbol is None:
            return ContractResolution(
                alias=alias,
                normalized_symbol=None,
                display_symbol=alias,
                is_mnq=False,
                is_current_contract=False,
                decisions_allowed=False,
                requires_attention=True,
                reason="Only exact MNQ contract aliases are accepted.",
            )

        current_contract = self.current_contract_for_date(trading_date)
        is_current = normalized_symbol == current_contract
        return ContractResolution(
            alias=alias,
            normalized_symbol=normalized_symbol,
            display_symbol=normalized_symbol,
            is_mnq=True,
            is_current_contract=is_current,
            decisions_allowed=is_current,
            requires_attention=not is_current,
            reason=(
                f"{normalized_symbol} is the selected current contract"
                if is_current
                else f"{normalized_symbol} is not current; expected {current_contract}"
            ),
        )

    def observe_alias(self, alias: str, trading_date: date) -> ContractResolution:
        """Resolve an alias and block decisions if two MNQ contracts are active."""
        resolution = self.resolve_alias(alias, trading_date)
        if resolution.normalized_symbol is not None:
            self.active_contracts.add(resolution.normalized_symbol)

        if len(self.active_contracts) > 1:
            contracts = ", ".join(sorted(self.active_contracts))
            return ContractResolution(
                alias=alias,
                normalized_symbol=resolution.normalized_symbol,
                display_symbol=contracts,
                is_mnq=resolution.is_mnq,
                is_current_contract=False,
                decisions_allowed=False,
                requires_attention=True,
                reason=f"Multiple MNQ contracts detected ({contracts}); recording continues, decisions blocked.",
            )
        return resolution

    def reset_session(self) -> None:
        """Clear active-contract tracking for a new recording session."""
        self.active_contracts.clear()


def normalize_mnq_alias(alias: str) -> str | None:
    """Return a normalized exact MNQ futures symbol or None."""
    normalized = alias.strip().upper().replace(" ", "").replace("-", "")
    match = re.fullmatch(r"MNQ([HMUZ])(\d{1,2})", normalized)
    if match is None:
        return None
    code, year = match.groups()
    if len(year) == 2:
        year = year[-1]
    return f"{MNQ_ROOT}{code}{year}"


DEFAULT_CONTRACT_SCHEDULE: tuple[ContractPeriod, ...] = (
    ContractPeriod("MNQH6", date(2025, 12, 12), date(2026, 3, 12)),
    ContractPeriod("MNQM6", date(2026, 3, 13), date(2026, 6, 11)),
    ContractPeriod("MNQU6", date(2026, 6, 12), date(2026, 9, 10)),
    ContractPeriod("MNQZ6", date(2026, 9, 11), date(2026, 12, 10)),
    ContractPeriod("MNQH7", date(2026, 12, 11), date(2027, 3, 11)),
)

