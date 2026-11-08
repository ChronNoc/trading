"""Deterministic Monte Carlo of a Lucid evaluation: governed, and under the old production rules.

It answers one question honestly: for a strategy whose true expectancy per trade is ``edge_r``
(in R of risk, net of costs), what happens to a Lucid evaluation account? It runs the governor's
OWN rule code (``LucidGovernor`` / ``GovernorBook``), so it measures what is actually enforced,
not a re-implementation. Alongside it runs the previous production behaviour (a fixed four
contracts, no daily caps, no profit cap) so the value of the rules is a measured difference.

Nothing here predicts profit. ``edge_r`` is an assumption you choose; the output shows what edge
an account needs before it has a fair chance, and that the governor turns "bust" into "retired".
Seeded and deterministic; Decimal money; no I/O.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from statistics import median

from app.risk.account_profile import AccountProfile
from app.risk.lucid_governor import (
    NEW_YORK,
    AccountStatus,
    ClosedTrade,
    EntryRequest,
    GovernorBook,
    GovernorPolicy,
    LucidGovernor,
    PositionAction,
    consistency_share,
)

TICK_VALUE = Decimal("0.50")
COMMISSION = Decimal("1.24")
SLIPPAGE_TICKS = Decimal("2")
_NS = 1_000_000_000
_FIRST_DAY = date(2027, 1, 4)  # a Monday
_SLOT_MINUTES = 10
_STALL_DAYS = 10  # ten trading days without a single trade while ACTIVE counts as stalled


@dataclass(frozen=True, slots=True)
class FitScenario:
    """A strategy described only by its true net expectancy and its stop and target."""

    edge_r: Decimal
    stop_ticks: int = 40
    target_ticks: int = 80
    setup_chance_per_slot: float = 0.5   # chance a setup appears in each 10-minute slot
    max_days: int = 250

    @property
    def round_trip_cost(self) -> Decimal:
        """Commission plus slippage for one contract, round turn."""
        return COMMISSION + SLIPPAGE_TICKS * TICK_VALUE

    @property
    def risk_per_contract(self) -> Decimal:
        """Dollars lost by one contract on a full stop, costs included."""
        return Decimal(self.stop_ticks) * TICK_VALUE + self.round_trip_cost

    @property
    def reward_per_contract(self) -> Decimal:
        """Dollars won by one contract at the target, after costs."""
        return Decimal(self.target_ticks) * TICK_VALUE - self.round_trip_cost

    @property
    def win_probability(self) -> float:
        """The win rate that gives exactly ``edge_r`` of expectancy per trade."""
        payoff = self.reward_per_contract / self.risk_per_contract
        probability = (self.edge_r + 1) / (payoff + 1)
        if not Decimal("0") < probability < Decimal("1"):
            raise ValueError(f"edge {self.edge_r}R is not reachable with this stop and target")
        return float(probability)


@dataclass(frozen=True, slots=True)
class EvaluationOutcome:
    """How one simulated evaluation ended."""

    status: str          # passed | retired | failed | stalled | active (ran out of days)
    days: int
    trades: int


@dataclass(frozen=True, slots=True)
class FitSummary:
    """The distribution of outcomes over many runs."""

    runs: int
    passed: int
    retired: int
    failed: int
    stalled: int
    active: int
    median_days_to_pass: int | None
    mean_trades: float

    def rate(self, count: int) -> float:
        """A count as a share of all runs."""
        return count / self.runs if self.runs else 0.0


def _ns(day: date, hour: int, minute: int) -> int:
    moment = datetime.combine(day, time(hour, minute), tzinfo=NEW_YORK).astimezone(UTC)
    return int(moment.timestamp()) * _NS


def _trading_days(count: int) -> list[date]:
    days: list[date] = []
    cursor = _FIRST_DAY
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _slots() -> list[tuple[int, int]]:
    out, minutes = [], 9 * 60 + 35
    while minutes < 16 * 60 + 15:
        out.append((minutes // 60, minutes % 60))
        minutes += _SLOT_MINUTES
    return out


def simulate_governed(
    profile: AccountProfile, policy: GovernorPolicy, scenario: FitScenario, rng: random.Random,
) -> EvaluationOutcome:
    """One evaluation traded under the governor (the real rule code)."""
    governor = LucidGovernor(profile, policy)
    book = GovernorBook(profile, policy)
    request = EntryRequest(
        Decimal(scenario.stop_ticks), Decimal(scenario.target_ticks), TICK_VALUE,
        COMMISSION, SLIPPAGE_TICKS, profile.max_micro_contracts)
    win_probability = scenario.win_probability
    trades = 0
    idle_days = 0
    for index, day in enumerate(_trading_days(scenario.max_days)):
        book.roll_day(day.isoformat())
        status = governor.status(book.snapshot(_ns(day, 9, 35)))
        if status is not AccountStatus.ACTIVE:
            return EvaluationOutcome(status.value, index, trades)
        if idle_days >= _STALL_DAYS:
            return EvaluationOutcome("stalled", index, trades)  # active, but no trade fits any more
        trades_before = trades
        for hour, minute in _slots():
            if book.day_locked_reason or book.day_entries >= policy.max_entries_per_day:
                break
            if rng.random() > scenario.setup_chance_per_slot:
                continue
            now = _ns(day, hour, minute)
            decision = governor.check_entry(book.snapshot(now), request)
            if not decision.allowed:
                continue
            risk = scenario.risk_per_contract * decision.contracts
            won = rng.random() < win_probability
            pnl = scenario.reward_per_contract * decision.contracts if won else -risk
            held = rng.randint(20, 300)
            book.record_entry(now)
            closed = now + held * _NS
            book.record_trade(ClosedTrade(now, closed, pnl, pnl / risk))
            trades += 1
            verdict = governor.check_position(book.snapshot(closed))
            if verdict.action is PositionAction.FLATTEN_AND_LOCK_DAY:
                book.lock_day(verdict.reason_code)
        idle_days = idle_days + 1 if trades == trades_before else 0
    return EvaluationOutcome("active", scenario.max_days, trades)


def simulate_unprotected(
    profile: AccountProfile, scenario: FitScenario, rng: random.Random, contracts: int = 4,
) -> EvaluationOutcome:
    """One evaluation under the previous production behaviour: fixed size, no daily caps.

    The same Lucid mechanics apply (an end-of-day trailing floor, the consistency rule); the
    only difference is that nothing limits size, entries, losses or profit within a day.
    """
    balance = profile.account_size
    highest_close = balance
    day_pnls: dict[str, Decimal] = {}
    win_probability = scenario.win_probability
    trades = 0
    for index, day in enumerate(_trading_days(scenario.max_days)):
        key = day.isoformat()
        day_pnls[key] = Decimal("0")
        floor = profile.trailing_threshold(highest_close)
        for _ in _slots():
            if rng.random() > scenario.setup_chance_per_slot:
                continue
            won = rng.random() < win_probability
            pnl = (scenario.reward_per_contract if won else -scenario.risk_per_contract) * contracts
            balance += pnl
            day_pnls[key] += pnl
            trades += 1
            if balance <= floor:
                return EvaluationOutcome("failed", index + 1, trades)
        highest_close = max(highest_close, balance)
        reached = balance >= profile.account_size + profile.profit_target
        share = consistency_share(day_pnls)
        limit = profile.consistency_max_day_share
        if reached and (limit is None or share is None or share <= limit):
            return EvaluationOutcome("passed", index + 1, trades)
    return EvaluationOutcome("active", scenario.max_days, trades)


def estimate(
    profile: AccountProfile, scenario: FitScenario, *, runs: int = 200, seed: int = 7,
    governed: bool = True, policy: GovernorPolicy | None = None,
) -> FitSummary:
    """Run many evaluations and summarise how they end."""
    chosen = policy or GovernorPolicy()
    outcomes: list[EvaluationOutcome] = []
    for run in range(runs):
        rng = random.Random(seed * 1_000_003 + run)
        outcomes.append(
            simulate_governed(profile, chosen, scenario, rng) if governed
            else simulate_unprotected(profile, scenario, rng))
    return _summarise(outcomes)


def _summarise(outcomes: Sequence[EvaluationOutcome]) -> FitSummary:
    def count(status: str) -> int:
        return sum(1 for o in outcomes if o.status == status)

    pass_days = [o.days for o in outcomes if o.status == "passed"]
    return FitSummary(
        runs=len(outcomes), passed=count("passed"), retired=count("retired"),
        failed=count("failed"), stalled=count("stalled"), active=count("active"),
        median_days_to_pass=int(median(pass_days)) if pass_days else None,
        mean_trades=sum(o.trades for o in outcomes) / len(outcomes) if outcomes else 0.0,
    )
