"""Lucid-fitted account risk governor: the rules that keep a prop evaluation alive.

The older engine (``limits.py`` / ``sizing.py`` / ``kill_switch.py``) is generic: a flat
1%-of-account budget, a fixed contract count and a daily entry cap. It knows nothing about the
two things that actually decide a Lucid evaluation:

* the **Max Loss Limit** is an *end-of-day trailing floor*; touching it ends the account, so the
  only question that matters before every trade is "how much room is left above the floor today";
* the **consistency rule** caps how much of the total profit any one day may supply, so a great
  day can be as damaging to a pass as a bad one.

This governor derives every number from the account profile (``AccountProfile``), never from
backtest results, so it cannot be tuned to make a result look better. It does not create an
edge; it makes the account survive long enough to find out whether there is one, keeps the
account inside Lucid's rules, and refuses to scale size up without the bot's own out-of-sample
evidence. The generic engine stays in place as the outer guard: this module only ever adds
restrictions (the stricter of the two always wins) and never disables or bypasses anything.

Pure and deterministic: Decimal money, explicit inputs, explicit outputs, every rule reporting
whether it passed and why. It imports no broker, network or paper-engine code.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from decimal import ROUND_FLOOR, Decimal
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

from app.risk.account_profile import AccountProfile

NEW_YORK = ZoneInfo("America/New_York")
ZERO = Decimal("0")
ONE = Decimal("1")
CENT = Decimal("0.01")
_NS = 1_000_000_000
_Z_ONE_SIDED_95 = Decimal("1.645")

# Stable reason codes (shown in the GUI, logged with every rejected setup).
CODE_APPROVED = "governor_approved"
CODE_ACCOUNT = "governor_account_not_active"
CODE_DAY_LOCKED = "governor_day_locked"
CODE_DAY_LOSS = "governor_day_loss_budget"
CODE_PROFIT_CAP = "governor_day_profit_cap"
CODE_WINDOW = "governor_outside_entry_window"
CODE_ENTRIES = "governor_daily_entries"
CODE_STREAK = "governor_losing_streak"
CODE_SPACING = "governor_entry_spacing"
CODE_COOLDOWN = "governor_loss_cooldown"
CODE_MICROSCALP = "governor_microscalp_guard"
CODE_QUALITY = "governor_trade_quality"
CODE_RISK_BUDGET = "governor_risk_budget"
CODE_CONFIG = "governor_config_invalid"

# Lucid auto-flattens at 16:45 New York; the governor must always be flat before that.
LATEST_FLAT_TIME = time(16, 44)
# GovernorBook state documents: bump when the layout changes (an old file is then refused).
STATE_VERSION = 1


class AccountStatus(str, Enum):
    """Where the evaluation account stands."""

    ACTIVE = "active"
    PASSED = "passed"        # target reached and consistency satisfied
    RETIRED = "retired"      # too little room left to survive normal variance
    FAILED = "failed"        # equity reached the max-loss floor


class EdgeState(str, Enum):
    """What the bot's own closed trades say about its expectancy."""

    NO_EVIDENCE = "no_evidence"      # too few trades to say anything
    INCONCLUSIVE = "inconclusive"    # enough trades, but not proven either way
    PROVEN = "proven"                # lower confidence bound of expectancy is above zero
    REFUTED = "refuted"              # upper confidence bound of expectancy is below zero


class PositionAction(str, Enum):
    """What to do with an open position and the rest of the day."""

    HOLD = "hold"
    FLATTEN_AND_LOCK_DAY = "flatten_and_lock_day"
    FLATTEN = "flatten"


@dataclass(frozen=True, slots=True)
class GovernorPolicy:
    """Every tunable rule, as fractions of the profile's own numbers. Validated on creation."""

    enabled: bool = True
    # --- survival (derived from the Max Loss Limit) ---------------------------------------
    day_loss_fraction: Decimal = Decimal("0.25")             # of the start-of-day buffer
    trades_per_day_budget: int = 2                           # one trade may use at most 1/N of the day budget
    max_trade_risk_fraction_of_mll: Decimal = Decimal("0.04")
    hard_stop_buffer_fraction: Decimal = Decimal("0.10")     # flatten this close to the floor
    retire_buffer_fraction: Decimal = Decimal("0.30")        # stop the account below this much room
    # --- consistency (derived from the PROFILE's single-day share, so it follows the checkout) --
    consistency_cushion: Decimal = Decimal("0.10")           # points kept below the firm's limit
    # --- behaviour -------------------------------------------------------------------------
    max_entries_per_day: int = 8
    max_losing_streak: int = 3
    cooldown_after_loss_seconds: int = 300
    min_seconds_between_entries: int = 120
    entry_window_start: time = time(9, 35)                   # New York time
    entry_window_end: time = time(16, 15)
    flat_time: time = time(16, 40)                           # before Lucid's 16:45 auto-flat
    # --- trade quality (derived from costs, not from results) ------------------------------
    min_net_reward_risk: Decimal = Decimal("1.0")            # (reward - costs) / (risk + costs)
    # --- microscalping (Lucid prohibits >50% of profit from trades held <= 5 s) ------------
    micro_hold_seconds: int = 5
    micro_profit_share_limit: Decimal = Decimal("0.30")
    # --- scaling only on evidence ------------------------------------------------------------
    contracts_without_proof: int = 1
    contracts_when_proven: int = 3
    edge_window_trades: int = 200
    edge_min_trades: int = 100
    edge_min_profit_factor: Decimal = Decimal("1.2")
    # Not a setting: set by ``read_governor_policy`` when config/risk_governor.yaml could not be
    # used. The strict defaults then still guard any open position, and every entry is refused
    # (CODE_CONFIG) until the file is fixed, so a typo is an error, never a silent default.
    config_error: str = ""

    def __post_init__(self) -> None:
        """Reject policies that would weaken the guarantees the governor exists to give.

        Every setting has a bound on BOTH sides. A value may be made stricter or, within these
        bounds, looser on purpose, but never so loose that a Lucid rule or the survival
        guarantee is lost (no zero cooldowns, no high-frequency entry counts, no flat time after
        Lucid's 16:45 auto-close, no NaN).
        """
        for name in ("day_loss_fraction", "max_trade_risk_fraction_of_mll", "hard_stop_buffer_fraction",
                     "retire_buffer_fraction", "consistency_cushion", "micro_profit_share_limit",
                     "min_net_reward_risk", "edge_min_profit_factor"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} must be a finite number (got {value!r})")
        fractions = {
            # name: (exclusive floor or inclusive floor, ceiling)
            "day_loss_fraction": (ZERO, False, Decimal("0.60")),
            "max_trade_risk_fraction_of_mll": (ZERO, False, Decimal("0.10")),
            "hard_stop_buffer_fraction": (Decimal("0.05"), True, Decimal("0.50")),
            "retire_buffer_fraction": (Decimal("0.15"), True, Decimal("0.90")),
            "consistency_cushion": (ZERO, False, Decimal("0.30")),
            "micro_profit_share_limit": (ZERO, False, Decimal("0.49")),
        }
        for name, (floor, inclusive, ceiling) in fractions.items():
            value = getattr(self, name)
            above_floor = value >= floor if inclusive else value > floor
            if not (above_floor and value <= ceiling):
                bracket = "[" if inclusive else "("
                raise ValueError(f"{name} must be in {bracket}{floor}, {ceiling}] (got {value})")
        if self.retire_buffer_fraction <= self.hard_stop_buffer_fraction:
            raise ValueError("retire_buffer_fraction must be above hard_stop_buffer_fraction")
        counts = {
            "trades_per_day_budget": (1, 10),
            "max_entries_per_day": (1, 20),             # Lucid prohibits high-frequency trading
            "max_losing_streak": (1, 6),
            "cooldown_after_loss_seconds": (60, 3600),
            "min_seconds_between_entries": (30, 3600),
            "micro_hold_seconds": (5, 60),              # Lucid's test is "5 seconds or less"
            "edge_min_trades": (30, 1000),
            "edge_window_trades": (30, 1000),           # the book keeps the last 1,000 trades
        }
        for name, (low, high) in counts.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} must be a whole number in [{low}, {high}] (got {value!r})")
        if self.edge_window_trades < self.edge_min_trades:
            raise ValueError("edge_window_trades must be at least edge_min_trades")
        if not self.entry_window_start < self.entry_window_end <= self.flat_time <= LATEST_FLAT_TIME:
            raise ValueError(
                f"times must satisfy entry_window_start < entry_window_end <= flat_time <= "
                f"{LATEST_FLAT_TIME:%H:%M} (Lucid auto-flattens at 16:45 New York)")
        if not Decimal("0.5") <= self.min_net_reward_risk <= Decimal("5"):
            raise ValueError(f"min_net_reward_risk must be in [0.5, 5] (got {self.min_net_reward_risk})")
        if not 1 <= self.contracts_without_proof <= self.contracts_when_proven <= 5:
            raise ValueError("contract tiers must satisfy 1 <= without_proof <= when_proven <= 5")
        if not Decimal("1") <= self.edge_min_profit_factor <= Decimal("5"):
            raise ValueError(f"edge_min_profit_factor must be in [1, 5] (got {self.edge_min_profit_factor})")


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """The few facts about a finished trade the governor needs (no paper-engine types)."""

    opened_ts_ns: int
    closed_ts_ns: int
    net_pnl: Decimal
    r_multiple: Decimal

    @property
    def hold_seconds(self) -> Decimal:
        """How long the position was open."""
        return Decimal(self.closed_ts_ns - self.opened_ts_ns) / Decimal(_NS)


@dataclass(frozen=True, slots=True)
class EdgeEvidence:
    """What the closed trades prove about expectancy (in R, net of costs)."""

    state: EdgeState
    trades: int
    mean_r: Decimal
    lower_bound_r: Decimal
    upper_bound_r: Decimal
    profit_factor: Decimal | None

    @property
    def live_blocked(self) -> bool:
        """Anything short of PROVEN must keep real-money arming blocked."""
        return self.state is not EdgeState.PROVEN


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Everything the rules read, explicitly (no hidden state)."""

    now_ns: int
    trading_day: str
    balance: Decimal                    # realized
    open_pnl: Decimal                   # unrealized
    highest_closing_balance: Decimal
    day_start_balance: Decimal
    day_realized: Decimal
    day_entries: int
    losing_streak: int
    last_entry_ts_ns: int | None
    last_loss_ts_ns: int | None
    day_locked_reason: str
    micro_profit_share: Decimal
    edge: EdgeEvidence
    best_day_share: Decimal | None      # consistency ratio so far (None until there is profit)
    traded_days: int | None = None      # days with at least one entry (None = not tracked)


@dataclass(frozen=True, slots=True)
class DayBudget:
    """The day's money, fixed at the start of the day from the buffer above the floor."""

    floor: Decimal
    start_buffer: Decimal
    loss_budget: Decimal
    per_trade_risk_cap: Decimal
    profit_cap: Decimal | None   # None when the profile has no consistency rule


@dataclass(frozen=True, slots=True)
class RuleCheck:
    """One rule's verdict, individually explainable."""

    name: str
    passed: bool
    message: str


@dataclass(frozen=True, slots=True)
class EntryRequest:
    """A proposed entry, reduced to the numbers the governor needs."""

    stop_ticks: Decimal
    target_ticks: Decimal
    tick_value: Decimal
    commission_per_contract: Decimal
    slippage_ticks_round_trip: Decimal
    contract_cap: int                  # the account's hard contract ceiling and the engine's own sizing


@dataclass(frozen=True, slots=True)
class EntryDecision:
    """Whether to enter, with how many contracts, and every rule that spoke."""

    allowed: bool
    contracts: int
    reason_code: str
    reason: str
    checks: tuple[RuleCheck, ...]
    budget: DayBudget

    @property
    def failures(self) -> tuple[RuleCheck, ...]:
        """The rules that blocked the entry."""
        return tuple(check for check in self.checks if not check.passed)


@dataclass(frozen=True, slots=True)
class PositionVerdict:
    """What to do right now about an open position."""

    action: PositionAction
    reason_code: str
    reason: str


# --- evidence -------------------------------------------------------------------------------


def assess_edge(trades: Sequence[ClosedTrade], policy: GovernorPolicy) -> EdgeEvidence:
    """Classify expectancy from the most recent closed trades (R-multiples net of costs).

    PROVEN needs enough trades, a lower 95% bound above zero and a real profit factor.
    REFUTED means even the upper bound is below zero. Everything else is not enough.
    """
    window = list(trades)[-policy.edge_window_trades:]
    count = len(window)
    if count == 0:
        return EdgeEvidence(EdgeState.NO_EVIDENCE, 0, ZERO, ZERO, ZERO, None)
    mean = sum((t.r_multiple for t in window), ZERO) / Decimal(count)
    if count < 2:
        return EdgeEvidence(EdgeState.NO_EVIDENCE, count, mean, mean, mean, None)
    variance = sum(((t.r_multiple - mean) ** 2 for t in window), ZERO) / Decimal(count - 1)
    standard_error = (variance / Decimal(count)).sqrt()
    lower, upper = mean - _Z_ONE_SIDED_95 * standard_error, mean + _Z_ONE_SIDED_95 * standard_error
    gains = sum((t.net_pnl for t in window if t.net_pnl > 0), ZERO)
    losses = -sum((t.net_pnl for t in window if t.net_pnl < 0), ZERO)
    profit_factor = (gains / losses) if losses > 0 else None
    if count < policy.edge_min_trades:
        state = EdgeState.NO_EVIDENCE
    elif lower > 0 and (profit_factor is None or profit_factor >= policy.edge_min_profit_factor):
        state = EdgeState.PROVEN
    elif upper < 0:
        state = EdgeState.REFUTED
    else:
        state = EdgeState.INCONCLUSIVE
    return EdgeEvidence(state, count, mean, lower, upper, profit_factor)


def consistency_share(day_pnls: Mapping[str, Decimal]) -> Decimal | None:
    """The best single day as a share of the account's NET profit (None until there is profit).

    Lucid's rule is "largest single day profit / account profit": losing days reduce the
    account profit, so they raise the share. Dividing by the winning days alone would let a
    history with losing days pass this check while failing Lucid's.
    """
    total = sum(day_pnls.values(), ZERO)
    if total <= 0:
        return None
    return max(day_pnls.values()) / total


# --- the rules -----------------------------------------------------------------------------


class LucidGovernor:
    """Stateless rule evaluation against an :class:`AccountSnapshot`.

    ``min_profit_hold_enforced`` tells the governor that the executor will not take a profit
    on a position younger than ``policy.micro_hold_seconds`` (the paper engine enforces this
    whenever the governor is on). New trades then cannot be microscalps, so they can only
    dilute the microscalp share: the guard reports the share but no longer blocks entries,
    which would otherwise freeze an account for good after one fast winner.
    """

    def __init__(
        self, profile: AccountProfile, policy: GovernorPolicy | None = None, *,
        min_profit_hold_enforced: bool = False,
    ) -> None:
        self.profile = profile
        self.policy = policy or GovernorPolicy()
        self.min_profit_hold_enforced = min_profit_hold_enforced
        limit = profile.consistency_max_day_share
        if limit is not None and self.policy.consistency_cushion >= limit:
            raise ValueError(
                f"consistency_cushion {self.policy.consistency_cushion} must be smaller than the profile's "
                f"single-day share {limit}")
        if self.policy.micro_hold_seconds < profile.min_hold_seconds_for_profit_share:
            raise ValueError(
                f"micro_hold_seconds {self.policy.micro_hold_seconds} is shorter than the firm's "
                f"microscalping test ({profile.min_hold_seconds_for_profit_share}s)")

    # -- money ---------------------------------------------------------------------------------

    def floor(self, highest_closing_balance: Decimal) -> Decimal:
        """The balance at which the account breaches (trails the highest close, then locks)."""
        return self.profile.trailing_threshold(highest_closing_balance)

    def day_budget(self, snapshot: AccountSnapshot) -> DayBudget:
        """Fix the day's loss budget, per-trade risk cap and profit cap.

        Everything is a fraction of the buffer above the floor at the START of the day, so a
        string of maximum-loss days can only shrink the buffer geometrically and never reach
        the floor. That holds for losses the size of a stop. A gap beyond a stop is absorbed
        only by the part of the buffer the day budget leaves untouched (at least 75% of it, so
        at least $225 at the retire threshold); the hard stop margin is a flatten trigger, not
        a cushion. A single loss bigger than that can still reach the floor.
        """
        policy, profile = self.policy, self.profile
        floor = self.floor(snapshot.highest_closing_balance)
        start_buffer = max(ZERO, snapshot.day_start_balance - floor)
        hard_margin = policy.hard_stop_buffer_fraction * profile.max_loss_limit
        loss_budget = max(ZERO, min(policy.day_loss_fraction * start_buffer, start_buffer - hard_margin))
        per_trade = min(
            loss_budget / Decimal(policy.trades_per_day_budget),
            policy.max_trade_risk_fraction_of_mll * profile.max_loss_limit,
        )
        limit = profile.consistency_max_day_share
        profit_cap = (
            None if limit is None
            else ((limit - policy.consistency_cushion) * profile.profit_target).quantize(CENT, rounding=ROUND_FLOOR))
        return DayBudget(
            floor=floor,
            start_buffer=start_buffer,
            loss_budget=loss_budget.quantize(CENT, rounding=ROUND_FLOOR),
            per_trade_risk_cap=per_trade.quantize(CENT, rounding=ROUND_FLOOR),
            profit_cap=profit_cap,
        )

    def status(self, snapshot: AccountSnapshot) -> AccountStatus:
        """FAILED / PASSED / RETIRED / ACTIVE for the account right now."""
        profile, policy = self.profile, self.policy
        equity = snapshot.balance + snapshot.open_pnl
        if equity <= self.floor(snapshot.highest_closing_balance):
            return AccountStatus.FAILED
        target_reached = snapshot.balance >= profile.account_size + profile.profit_target
        consistent = (
            profile.consistency_max_day_share is None
            or snapshot.best_day_share is None
            or snapshot.best_day_share <= profile.consistency_max_day_share
        )
        enough_days = snapshot.traded_days is None or snapshot.traded_days >= profile.min_trading_days
        if target_reached and consistent and enough_days:
            return AccountStatus.PASSED
        budget = self.day_budget(snapshot)
        if budget.start_buffer < policy.retire_buffer_fraction * profile.max_loss_limit:
            return AccountStatus.RETIRED
        return AccountStatus.ACTIVE

    # -- before a trade ----------------------------------------------------------------------

    def check_entry(self, snapshot: AccountSnapshot, request: EntryRequest) -> EntryDecision:
        """Run every entry rule and size the trade. The strictest rule always wins."""
        policy = self.policy
        budget = self.day_budget(snapshot)
        day_pnl = snapshot.day_realized + snapshot.open_pnl
        status = self.status(snapshot)
        checks: list[RuleCheck] = []

        def add(name: str, passed: bool, ok: str, bad: str) -> None:
            checks.append(RuleCheck(name, passed, ok if passed else bad))

        add(CODE_CONFIG, not policy.config_error, "governor settings are valid",
            f"config/risk_governor.yaml is invalid, so no entries until it is fixed: {policy.config_error}")
        add(CODE_ACCOUNT, status is AccountStatus.ACTIVE, "account is active",
            f"account is {status.value}")
        add(CODE_DAY_LOCKED, not snapshot.day_locked_reason, "day is not locked",
            f"day is locked: {snapshot.day_locked_reason}")
        loss_so_far = max(ZERO, -day_pnl)
        add(CODE_DAY_LOSS, loss_so_far < budget.loss_budget,
            f"today's loss ${loss_so_far} is inside the ${budget.loss_budget} budget",
            f"today's loss ${loss_so_far} reached the ${budget.loss_budget} day budget")
        if budget.profit_cap is None:
            add(CODE_PROFIT_CAP, True, "the profile has no consistency rule", "")
        else:
            add(CODE_PROFIT_CAP, day_pnl < budget.profit_cap,
                f"today's profit {max(day_pnl, ZERO)} is under the ${budget.profit_cap} consistency cap",
                f"today's profit reached the ${budget.profit_cap} cap that protects the consistency rule")
        local = _new_york_time(snapshot.now_ns)
        in_window = policy.entry_window_start <= local < policy.entry_window_end
        add(CODE_WINDOW, in_window,
            f"{local:%H:%M} is inside the entry window",
            f"{local:%H:%M} is outside {policy.entry_window_start:%H:%M}-{policy.entry_window_end:%H:%M} New York")
        add(CODE_ENTRIES, snapshot.day_entries < policy.max_entries_per_day,
            f"{snapshot.day_entries} of {policy.max_entries_per_day} entries used today",
            f"all {policy.max_entries_per_day} entries for today are used")
        add(CODE_STREAK, snapshot.losing_streak < policy.max_losing_streak,
            f"losing streak {snapshot.losing_streak} is below {policy.max_losing_streak}",
            f"{snapshot.losing_streak} losses in a row today")
        add(CODE_SPACING, self._elapsed_ok(snapshot.now_ns, snapshot.last_entry_ts_ns, policy.min_seconds_between_entries),
            "enough time since the last entry", f"less than {policy.min_seconds_between_entries}s since the last entry")
        add(CODE_COOLDOWN, self._elapsed_ok(snapshot.now_ns, snapshot.last_loss_ts_ns, policy.cooldown_after_loss_seconds),
            "cooldown after the last loss is over", f"within {policy.cooldown_after_loss_seconds}s of a loss")
        micro_ok = snapshot.micro_profit_share < policy.micro_profit_share_limit
        if self.min_profit_hold_enforced and not micro_ok:
            add(CODE_MICROSCALP, True,
                f"{snapshot.micro_profit_share:.0%} of profit is from <= {policy.micro_hold_seconds}s trades, "
                f"but no profit is taken before {policy.micro_hold_seconds}s, so new trades can only lower it", "")
        else:
            add(CODE_MICROSCALP, micro_ok,
                f"{snapshot.micro_profit_share:.0%} of profit from <= {policy.micro_hold_seconds}s trades",
                f"{snapshot.micro_profit_share:.0%} of profit comes from trades held <= {policy.micro_hold_seconds}s "
                "(Lucid prohibits more than 50%)")

        round_trip_cost = request.commission_per_contract + request.slippage_ticks_round_trip * request.tick_value
        risk_dollars = request.stop_ticks * request.tick_value + round_trip_cost
        reward_dollars = request.target_ticks * request.tick_value - round_trip_cost
        net_reward_risk = reward_dollars / risk_dollars if risk_dollars > 0 else ZERO
        add(CODE_QUALITY, net_reward_risk >= policy.min_net_reward_risk,
            f"net reward:risk {net_reward_risk:.2f} after ${round_trip_cost} of costs",
            f"net reward:risk {net_reward_risk:.2f} after ${round_trip_cost} of costs is below {policy.min_net_reward_risk}")

        tier_cap = policy.contracts_when_proven if snapshot.edge.state is EdgeState.PROVEN else policy.contracts_without_proof
        remaining_loss = max(ZERO, budget.loss_budget + min(day_pnl, ZERO))
        affordable = min(budget.per_trade_risk_cap, remaining_loss)
        by_risk = int((affordable / risk_dollars).to_integral_value(rounding=ROUND_FLOOR)) if risk_dollars > 0 else 0
        contracts = max(0, min(by_risk, tier_cap, request.contract_cap, self.profile.max_micro_contracts))
        add(CODE_RISK_BUDGET, contracts >= 1,
            f"{contracts} contract(s): ${risk_dollars} risk each fits ${affordable} "
            f"(tier cap {tier_cap}, edge {snapshot.edge.state.value})",
            f"one contract risks ${risk_dollars} but only ${affordable} may be risked "
            f"(per-trade cap ${budget.per_trade_risk_cap}, left today ${remaining_loss})")

        failures = [check for check in checks if not check.passed]
        if failures:
            return EntryDecision(False, 0, failures[0].name, "; ".join(c.message for c in failures),
                                 tuple(checks), budget)
        return EntryDecision(True, contracts, CODE_APPROVED, "; ".join(c.message for c in checks[-1:]),
                             tuple(checks), budget)

    # -- while a position is open -------------------------------------------------------------

    def check_position(self, snapshot: AccountSnapshot) -> PositionVerdict:
        """Decide whether to flatten now: floor proximity, day stop, profit cap, flat time."""
        policy = self.policy
        budget = self.day_budget(snapshot)
        equity = snapshot.balance + snapshot.open_pnl
        hard_margin = policy.hard_stop_buffer_fraction * self.profile.max_loss_limit
        day_pnl = snapshot.day_realized + snapshot.open_pnl
        if equity - budget.floor <= hard_margin:
            return PositionVerdict(
                PositionAction.FLATTEN_AND_LOCK_DAY, "governor_floor_proximity",
                f"equity {equity} is within ${hard_margin} of the max-loss floor {budget.floor}")
        if budget.loss_budget > 0 and -day_pnl >= budget.loss_budget:
            return PositionVerdict(
                PositionAction.FLATTEN_AND_LOCK_DAY, CODE_DAY_LOSS,
                f"today's loss reached the ${budget.loss_budget} budget")
        if budget.profit_cap is not None and day_pnl >= budget.profit_cap:
            return PositionVerdict(
                PositionAction.FLATTEN_AND_LOCK_DAY, CODE_PROFIT_CAP,
                f"today's profit reached the ${budget.profit_cap} cap that protects the consistency rule")
        local = _new_york_time(snapshot.now_ns)
        if local >= policy.flat_time:
            return PositionVerdict(
                PositionAction.FLATTEN, "governor_flat_time",
                f"{policy.flat_time:%H:%M} New York: flat before Lucid's auto-close")
        if local < policy.entry_window_start:
            # Only reachable when no event arrived between the flat time and midnight: a position
            # must never be carried overnight just because the clock wrapped past 00:00.
            return PositionVerdict(
                PositionAction.FLATTEN, "governor_flat_time",
                f"{local:%H:%M} New York is outside the holding hours "
                f"{policy.entry_window_start:%H:%M}-{policy.flat_time:%H:%M}")
        return PositionVerdict(PositionAction.HOLD, "governor_hold", "inside every limit")

    @staticmethod
    def _elapsed_ok(now_ns: int, since_ns: int | None, seconds: int) -> bool:
        return since_ns is None or (now_ns - since_ns) >= seconds * _NS


# --- the tracker the engine feeds ------------------------------------------------------------


@dataclass(slots=True)
class GovernorBook:
    """Mutable bookkeeping that builds immutable snapshots for the rules to read."""

    profile: AccountProfile
    policy: GovernorPolicy
    balance: Decimal = ZERO
    highest_closing_balance: Decimal = ZERO
    day: str = ""
    day_start_balance: Decimal = ZERO
    day_realized: Decimal = ZERO
    day_entries: int = 0
    losing_streak: int = 0
    last_entry_ts_ns: int | None = None
    last_loss_ts_ns: int | None = None
    day_locked_reason: str = ""
    total_profit: Decimal = ZERO
    micro_profit: Decimal = ZERO
    day_pnls: dict[str, Decimal] = field(default_factory=dict)
    closing_balances: dict[str, Decimal] = field(default_factory=dict)
    trades: deque[ClosedTrade] = field(default_factory=lambda: deque(maxlen=1000))
    traded_days: set[str] = field(default_factory=set)
    edge_cache: EdgeEvidence | None = None   # recomputed only when a trade closes (snapshots are hot)

    def __post_init__(self) -> None:
        """Start from the profile's balance unless told otherwise."""
        if self.balance == ZERO:
            self.balance = self.profile.account_size
        if self.highest_closing_balance == ZERO:
            self.highest_closing_balance = self.balance
        self.day_start_balance = self.balance

    def roll_day(self, trading_day: str) -> bool:
        """Start a new trading day: bank the old day's close and reset the day's counters.

        Returns whether the day changed. A day EARLIER than the current one is ignored: one
        stale event across the 18:00 roll must never reopen a finished day, clear today's lock
        or overwrite a banked day's result.
        """
        if not trading_day or trading_day == self.day:
            return False
        if self.day and trading_day < self.day:
            return False
        if self.day:
            self.closing_balances[self.day] = self.balance
            self.day_pnls[self.day] = self.day_realized
            self.highest_closing_balance = max(self.highest_closing_balance, self.balance)
        self.day = trading_day
        self.day_start_balance = self.balance
        self.day_realized = ZERO
        self.day_entries = 0
        self.losing_streak = 0
        self.last_entry_ts_ns = None
        self.last_loss_ts_ns = None
        self.day_locked_reason = ""
        return True

    def record_entry(self, ts_ns: int) -> None:
        """Count an entry that was actually filled."""
        self.day_entries += 1
        self.last_entry_ts_ns = ts_ns
        if self.day:
            self.traded_days.add(self.day)

    def record_trade(self, trade: ClosedTrade) -> None:
        """Book a closed trade: balance, day result, streak and the microscalping tally."""
        self.balance += trade.net_pnl
        self.day_realized += trade.net_pnl
        self.trades.append(trade)
        self.edge_cache = None
        if trade.net_pnl <= 0:
            self.losing_streak += 1
            self.last_loss_ts_ns = trade.closed_ts_ns
        else:
            self.losing_streak = 0
            self.total_profit += trade.net_pnl
            if trade.hold_seconds <= Decimal(self.policy.micro_hold_seconds):
                self.micro_profit += trade.net_pnl

    def lock_day(self, reason: str) -> None:
        """Stop new entries for the rest of this trading day."""
        if not self.day_locked_reason:
            self.day_locked_reason = reason

    def micro_profit_share(self) -> Decimal:
        """Profit from trades held <= micro_hold_seconds, as a share of the account's profit.

        Measured against NET profit when there is any (the stricter base, since net profit is
        never more than the sum of the winners), otherwise against the winning trades.
        """
        if self.micro_profit <= 0:
            return ZERO
        net_profit = self.balance - self.profile.account_size
        base = net_profit if net_profit > 0 else self.total_profit
        return (self.micro_profit / base) if base > 0 else ZERO

    def snapshot(self, now_ns: int, open_pnl: Decimal = ZERO) -> AccountSnapshot:
        """An immutable view of the account for the rules to read."""
        share = self.micro_profit_share()
        if self.edge_cache is None:
            self.edge_cache = assess_edge(tuple(self.trades), self.policy)
        today = dict(self.day_pnls)
        if self.day:
            today[self.day] = self.day_realized
        return AccountSnapshot(
            now_ns=now_ns, trading_day=self.day, balance=self.balance, open_pnl=open_pnl,
            highest_closing_balance=self.highest_closing_balance,
            day_start_balance=self.day_start_balance, day_realized=self.day_realized,
            day_entries=self.day_entries, losing_streak=self.losing_streak,
            last_entry_ts_ns=self.last_entry_ts_ns, last_loss_ts_ns=self.last_loss_ts_ns,
            day_locked_reason=self.day_locked_reason, micro_profit_share=share,
            edge=self.edge_cache,
            best_day_share=consistency_share(today),
            traded_days=len(self.traded_days),
        )

    # -- persistence (pure conversion; the file I/O lives in app.risk.governor_state) ----------

    def to_state(self) -> dict[str, object]:
        """Everything the book knows, as a JSON-safe document (money as strings)."""
        return {
            "version": STATE_VERSION,
            "profile_id": self.profile.profile_id,
            "account_size": str(self.profile.account_size),
            "balance": str(self.balance),
            "highest_closing_balance": str(self.highest_closing_balance),
            "day": self.day,
            "day_start_balance": str(self.day_start_balance),
            "day_realized": str(self.day_realized),
            "day_entries": self.day_entries,
            "losing_streak": self.losing_streak,
            "last_entry_ts_ns": self.last_entry_ts_ns,
            "last_loss_ts_ns": self.last_loss_ts_ns,
            "day_locked_reason": self.day_locked_reason,
            "total_profit": str(self.total_profit),
            "micro_profit": str(self.micro_profit),
            "day_pnls": {day: str(value) for day, value in self.day_pnls.items()},
            "closing_balances": {day: str(value) for day, value in self.closing_balances.items()},
            "traded_days": sorted(self.traded_days),
            "trades": [
                [trade.opened_ts_ns, trade.closed_ts_ns, str(trade.net_pnl), str(trade.r_multiple)]
                for trade in self.trades
            ],
        }

    @classmethod
    def from_state(
        cls, profile: AccountProfile, policy: GovernorPolicy, state: Mapping[str, object],
    ) -> "GovernorBook":
        """Rebuild a book from :meth:`to_state` output; refuses a document for another account."""
        if state.get("version") != STATE_VERSION:
            raise ValueError(f"governor state version {state.get('version')!r} != {STATE_VERSION}")
        if state.get("profile_id") != profile.profile_id or Decimal(str(state.get("account_size"))) != profile.account_size:
            raise ValueError(
                f"governor state belongs to {state.get('profile_id')!r} "
                f"({state.get('account_size')}), not {profile.profile_id!r} ({profile.account_size})")

        def money(key: str) -> Decimal:
            value = Decimal(str(state[key]))
            if not value.is_finite():
                raise ValueError(f"governor state {key} is not a finite number")
            return value

        def optional_ns(key: str) -> int | None:
            value = state.get(key)
            return None if value is None else int(value)  # type: ignore[arg-type]

        book = cls(profile=profile, policy=policy, balance=money("balance"),
                   highest_closing_balance=money("highest_closing_balance"))
        book.day = str(state["day"])
        book.day_start_balance = money("day_start_balance")
        book.day_realized = money("day_realized")
        book.day_entries = int(state["day_entries"])  # type: ignore[arg-type]
        book.losing_streak = int(state["losing_streak"])  # type: ignore[arg-type]
        book.last_entry_ts_ns = optional_ns("last_entry_ts_ns")
        book.last_loss_ts_ns = optional_ns("last_loss_ts_ns")
        book.day_locked_reason = str(state["day_locked_reason"])
        book.total_profit = money("total_profit")
        book.micro_profit = money("micro_profit")
        book.day_pnls = {str(k): Decimal(str(v)) for k, v in dict(state["day_pnls"]).items()}  # type: ignore[call-overload]
        book.closing_balances = {
            str(k): Decimal(str(v)) for k, v in dict(state["closing_balances"]).items()}  # type: ignore[call-overload]
        book.traded_days = {str(day) for day in state["traded_days"]}  # type: ignore[union-attr]
        for opened, closed, net, r_multiple in state["trades"]:  # type: ignore[union-attr,misc]
            book.trades.append(ClosedTrade(int(opened), int(closed), Decimal(str(net)), Decimal(str(r_multiple))))
        return book


# --- loading ---------------------------------------------------------------------------------


def _new_york_time(ts_ns: int) -> time:
    moment = datetime.fromtimestamp(ts_ns // _NS, tz=UTC).astimezone(NEW_YORK)
    return moment.time().replace(microsecond=0, tzinfo=None)


def _read_unique_mapping(path: Path) -> object:
    """Parse YAML with the safe loader, refusing a key written twice (the last would win)."""
    import yaml

    class _UniqueKeyLoader(yaml.SafeLoader):
        pass

    def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
        mapping: dict[object, object] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in mapping:
                raise ValueError(f"{path}: setting {key!r} is written more than once")
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    _UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)
    return yaml.load(path.read_text(encoding="utf-8-sig"), Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass


def _setting(path: Path, key: str, raw: object, default: object) -> object:
    """Convert one YAML value to the type of its default, refusing anything ambiguous."""
    where = f"{path}: {key}"
    if isinstance(default, bool):
        if not isinstance(raw, bool):
            raise ValueError(f"{where} must be true or false (got {raw!r})")
        return raw
    if isinstance(default, Decimal):
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise ValueError(f"{where} must be a number (got {raw!r})")
        try:
            value = Decimal(str(raw).strip())
        except ArithmeticError as error:
            raise ValueError(f"{where} must be a number (got {raw!r})") from error
        if not value.is_finite():
            raise ValueError(f"{where} must be a finite number (got {raw!r})")
        return value
    if isinstance(default, int):
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise ValueError(f"{where} must be a whole number (got {raw!r})")
        return raw
    if isinstance(default, time):
        if not isinstance(raw, str):
            # YAML 1.1 reads an unquoted 9:35 as the number 575 (base-60), so insist on quotes.
            raise ValueError(f'{where} must be a quoted New York time like "09:35" (got {raw!r})')
        parts = raw.strip().split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError(f'{where} must look like "09:35" (got {raw!r})')
        return time(int(parts[0]), int(parts[1]))
    raise ValueError(f"{where} has an unsupported type")  # pragma: no cover


def load_governor_policy(path: Path | str) -> GovernorPolicy:
    """Read ``config/risk_governor.yaml``; a missing or empty file means the safe defaults.

    Anything else that is not exactly right raises ``ValueError`` (``read_governor_policy``
    turns that into a refusal to trade, never a silent default): unknown keys, a key written
    twice, ``enabled`` that is not true/false, unquoted times, NaN, and any value outside the
    bounds in :class:`GovernorPolicy`.
    """
    path = Path(path)
    defaults = GovernorPolicy()
    if not path.is_file():
        return defaults
    payload = _read_unique_mapping(path)
    if payload is None:
        return defaults
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a mapping of governor settings")
    allowed = set(GovernorPolicy.__dataclass_fields__) - {"config_error"}
    unknown = sorted(str(key) for key in set(payload) - allowed)
    if unknown:
        raise ValueError(f"{path}: unknown governor setting(s): {', '.join(unknown)}")
    values = {key: _setting(path, key, raw, getattr(defaults, key)) for key, raw in payload.items()}
    return GovernorPolicy(**values)  # type: ignore[arg-type]
