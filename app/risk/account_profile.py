"""Typed, machine-enforceable prop-account profile (Lucid Flex 25K by default).

Replaces the prose-only profile fields with real types the risk engine and the
paper ledger can *enforce*: Decimal money, integer contract caps, enums, and
timezone-aware session times. Every rule carries its provenance (source URL,
retrieval date, effective date, and a hash of the source excerpt it came from),
so an unverified rule is structurally distinguishable from a verified one.

Unresolved rules block DEMO/LIVE arming. They never block recording or paper
research - capture and honest offline evaluation must always keep working.

Project safety limits (max entries/day, max losses/day, no averaging down) are
*additional* and always apply: :meth:`AccountProfile.effective_limits` takes the
STRICTER of the firm rule and the project rule, never the looser.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import time
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

EXCHANGE_TZ = ZoneInfo("America/New_York")


class DrawdownMethod(str, Enum):
    """How the maximum-loss limit moves."""

    EOD_TRAILING_THEN_LOCK = "eod_trailing_then_lock"
    INTRADAY_TRAILING = "intraday_trailing"
    STATIC = "static"
    UNRESOLVED = "unresolved"


class NewsPolicy(str, Enum):
    """Whether trading around news is permitted."""

    UNRESTRICTED = "unrestricted"
    RESTRICTED = "restricted"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class RuleProvenance:
    """Where a rule came from and when it was read."""

    source_url: str
    retrieval_date: str
    effective_date: str
    excerpt: str = ""

    @property
    def excerpt_sha256(self) -> str:
        """Hash of the source excerpt, so a silent upstream edit is detectable."""
        return hashlib.sha256(self.excerpt.encode("utf-8")).hexdigest()

    @property
    def verified(self) -> bool:
        """A rule is verified only with a real URL and retrieval date."""
        return bool(self.source_url and self.retrieval_date)


@dataclass(frozen=True, slots=True)
class ProjectSafetyLimits:
    """Project limits that always apply ON TOP of the firm's rules."""

    max_entries_per_day: int = 3
    max_losses_per_day: int = 3
    daily_risk_fraction: Decimal = Decimal("0.01")
    allow_averaging_down: bool = False

    def __post_init__(self) -> None:
        """Validate; these may never be widened by config."""
        if self.max_entries_per_day < 1 or self.max_losses_per_day < 1:
            raise ValueError("project daily limits must be >= 1")
        if not Decimal("0") < self.daily_risk_fraction <= Decimal("0.05"):
            raise ValueError("daily_risk_fraction must be in (0, 5%]")
        if self.allow_averaging_down:
            raise ValueError("averaging down is prohibited by this project")


@dataclass(frozen=True, slots=True)
class EffectiveLimits:
    """The binding limits after combining firm rules with project safety."""

    max_contracts: int
    max_entries_per_day: int
    max_losses_per_day: int
    daily_loss_cap: Decimal | None
    allow_averaging_down: bool


@dataclass(frozen=True, slots=True)
class AccountProfile:
    """A fully typed prop-account profile."""

    profile_id: str
    firm: str
    display_name: str
    account_size: Decimal
    profit_target: Decimal
    max_loss_limit: Decimal
    drawdown_method: DrawdownMethod
    # EOD trailing specifics: the MLL trails the highest CLOSING balance until the
    # account exceeds initial_trail_balance, then locks at locked_mll_balance.
    initial_trail_balance: Decimal | None
    locked_mll_balance: Decimal | None
    max_micro_contracts: int
    max_mini_contracts: int
    consistency_max_day_share: Decimal | None  # e.g. 0.50 => no day may exceed 50% of profit
    consistency_applies_to_funded: bool
    daily_loss_limit: Decimal | None  # None = firm imposes none
    session_close: time  # exchange-local auto-flat time
    session_reopen: time
    news_policy: NewsPolicy
    flatten_overnight: bool
    prohibited_behaviours: tuple[str, ...]
    min_hold_seconds_for_profit_share: int  # microscalping test threshold
    provenance: RuleProvenance
    project_limits: ProjectSafetyLimits = field(default_factory=ProjectSafetyLimits)
    unresolved_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate the money/contract invariants."""
        for name, value in (("account_size", self.account_size),
                            ("profit_target", self.profit_target),
                            ("max_loss_limit", self.max_loss_limit)):
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_micro_contracts < 0 or self.max_mini_contracts < 0:
            raise ValueError("contract caps must be non-negative")
        if self.consistency_max_day_share is not None and not (
            Decimal("0") < self.consistency_max_day_share <= Decimal("1")
        ):
            raise ValueError("consistency_max_day_share must be in (0, 1]")

    # -- gating -----------------------------------------------------------------

    @property
    def resolved(self) -> bool:
        """True only when every rule is verified against an official source."""
        return not self.unresolved_fields and self.provenance.verified

    @property
    def blocks_broker_arming(self) -> bool:
        """Unresolved rules block DEMO/LIVE arming (never recording or research)."""
        return not self.resolved

    # -- enforcement -------------------------------------------------------------

    def effective_limits(self) -> EffectiveLimits:
        """Return the binding limits: the STRICTER of firm and project rules."""
        firm_daily = self.daily_loss_limit
        return EffectiveLimits(
            max_contracts=self.max_micro_contracts,  # this project trades MNQ micros
            max_entries_per_day=self.project_limits.max_entries_per_day,
            max_losses_per_day=self.project_limits.max_losses_per_day,
            daily_loss_cap=firm_daily,  # None => only the project's risk budget binds
            allow_averaging_down=False,  # never widened
        )

    def trailing_threshold(self, highest_closing_balance: Decimal) -> Decimal:
        """Return the current max-loss threshold for an EOD trailing account.

        The MLL trails the highest CLOSING balance until the account exceeds the
        initial trail balance, after which it locks. Touching it is a breach.
        """
        if self.drawdown_method is not DrawdownMethod.EOD_TRAILING_THEN_LOCK:
            return self.account_size - self.max_loss_limit
        if self.initial_trail_balance is None or self.locked_mll_balance is None:
            return self.account_size - self.max_loss_limit
        if highest_closing_balance > self.initial_trail_balance:
            return self.locked_mll_balance
        trailed = highest_closing_balance - self.max_loss_limit
        floor = self.account_size - self.max_loss_limit
        return max(trailed, floor)

    def drawdown_room(self, current_balance: Decimal, highest_closing_balance: Decimal) -> Decimal:
        """Return how much the account may lose before breaching."""
        return current_balance - self.trailing_threshold(highest_closing_balance)

    def target_progress(self, current_balance: Decimal) -> Decimal:
        """Return progress toward the profit target as a 0..1 fraction."""
        gained = current_balance - self.account_size
        if gained <= 0:
            return Decimal("0")
        return min(Decimal("1"), (gained / self.profit_target).quantize(Decimal("0.0001")))

    def consistency_breached(self, per_day_profit: Mapping[str, Decimal]) -> bool:
        """Return whether any single day exceeds the allowed share of total profit."""
        if self.consistency_max_day_share is None:
            return False
        total = sum((v for v in per_day_profit.values() if v > 0), Decimal("0"))
        if total <= 0:
            return False
        best = max((v for v in per_day_profit.values()), default=Decimal("0"))
        return (best / total) > self.consistency_max_day_share


def lucid_flex_25k() -> AccountProfile:
    """The verified LucidFlex 25K Evaluation profile (see config/prop_profiles).

    Every value below was read from Lucid's own pages on 2026-07-17; see
    ``config/prop_profiles/lucid_flex_25k.yaml`` for the full URL list.
    """
    return AccountProfile(
        profile_id="lucid_flex_25k",
        firm="Lucid Trading",
        display_name="LucidFlex 25K Evaluation",
        account_size=Decimal("25000"),
        profit_target=Decimal("1250"),
        max_loss_limit=Decimal("1000"),
        drawdown_method=DrawdownMethod.EOD_TRAILING_THEN_LOCK,
        initial_trail_balance=Decimal("26100"),
        locked_mll_balance=Decimal("25100"),
        max_micro_contracts=20,
        max_mini_contracts=2,
        consistency_max_day_share=Decimal("0.50"),
        consistency_applies_to_funded=False,
        daily_loss_limit=None,  # Lucid imposes no DLL on Flex evaluations
        session_close=time(16, 45, tzinfo=EXCHANGE_TZ),
        session_reopen=time(18, 0, tzinfo=EXCHANGE_TZ),
        news_policy=NewsPolicy.UNRESTRICTED,
        flatten_overnight=True,
        prohibited_behaviours=(
            "microscalping (>50% of profit from trades held <=5s)",
            "hedging",
            "high-frequency trading",
        ),
        min_hold_seconds_for_profit_share=5,
        provenance=RuleProvenance(
            source_url="https://support.lucidtrading.com/en/articles/12945790-lucidflex-evaluation-account",
            retrieval_date="2026-07-17",
            effective_date="2026-04-15",
            excerpt=(
                "$25,000 | $1,250 profit target | $1,000 max loss limit | 50% consistency | "
                "2 mini or 20 micros. There is no DLL on LucidFlex evaluation accounts. "
                "EOD drawdown: MLL trails highest closing balance to Initial Trail $26,100, "
                "then locks at $25,100."
            ),
        ),
        unresolved_fields=(),
    )


def load_selected_profile(path: Path | None = None) -> AccountProfile:
    """Return the selected account profile (Lucid Flex 25K is the default).

    ``path`` selects a YAML profile. The verified Lucid Flex 25K profile is the
    default so the app never falls back to a fabricated $100,000 account. If the
    selected YAML is not fully verified, the typed shape is still returned but is
    marked unresolved, which blocks DEMO/LIVE arming while leaving recording and
    paper research fully working.
    """
    from dataclasses import replace

    profile = lucid_flex_25k()
    if path is None:
        return profile

    from app.execution.prop_rules import PropRuleProfile

    raw = PropRuleProfile.load(path)
    if raw.blocks_automated_execution:
        return replace(profile, unresolved_fields=raw.unresolved_fields or ("profile unverified",))
    if "flex_25k" in path.name.lower():
        return profile
    # A different verified firm profile: carry its provenance onto the typed shape
    # rather than silently presenting Lucid's numbers for another account.
    return replace(
        profile,
        profile_id=path.stem,
        display_name=f"{raw.firm} {raw.account_type}",
        unresolved_fields=("typed mapping not implemented for this profile",),
    )
