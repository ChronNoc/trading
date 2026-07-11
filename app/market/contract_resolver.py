"""MNQ contract alias resolution and rollover checks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

MNQ_ROOT = "MNQ"
MONTH_CODES = {"H": 3, "M": 6, "U": 9, "Z": 12}


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
        """Return the expected current MNQ contract for ``trading_date``."""
        for period in self.schedule:
            if period.start_date <= trading_date <= period.end_date:
                return period.symbol
        raise ValueError(f"no MNQ rollover period configured for {trading_date.isoformat()}")

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

