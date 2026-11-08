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

    def __post_init__(self) -> None:
        """Reject policies that would weaken the guarantees the governor exists to give."""
        fractions = {
            "day_loss_fraction": (self.day_loss_fraction, Decimal("0.60")),
            "max_trade_risk_fraction_of_mll": (self.max_trade_risk_fraction_of_mll, Decimal("0.10")),
            "hard_stop_buffer_fraction": (self.hard_stop_buffer_fraction, Decimal("0.50")),
            "retire_buffer_fraction": (self.retire_buffer_fraction, Decimal("0.90")),
            "consistency_cushion": (self.consistency_cushion, Decimal("0.30")),
            "micro_profit_share_limit": (self.micro_profit_share_limit, Decimal("0.49")),
        }
        for name, (value, ceiling) in fractions.items():
            if not ZERO < value <= ceiling:
                raise ValueError(f"{name} must be in (0, {ceiling}] (got {value})")
        if self.trades_per_day_budget < 1 or self.max_entries_per_day < 1 or self.max_losing_streak < 1:
            raise ValueError("per-day counts must be at least 1")
        if self.cooldown_after_loss_seconds < 0 or self.min_seconds_between_entries < 0:
            raise ValueError("time gaps must be non-negative")
        if not self.entry_window_start < self.entry_window_end <= self.flat_time:
            raise ValueError("entry window must end at or before the flat time")
        if self.min_net_reward_risk < ZERO:
            raise ValueError("min_net_reward_risk must be non-negative")
        if not 1 <= self.contracts_without_proof <= self.contracts_when_proven:
            raise ValueError("contract tiers must satisfy 1 <= without_proof <= when_proven")
        if self.edge_min_trades < 30 or self.edge_window_trades < self.edge_min_trades:
            raise ValueError("edge evidence needs at least 30 trades and a window no smaller than that")


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
    """The best single day as a share of total profit (None until there is any profit)."""
    total = sum((value for value in day_pnls.values() if value > 0), ZERO)
    if total <= 0:
        return None
    return max(day_pnls.values()) / total


# --- the rules -----------------------------------------------------------------------------


class LucidGovernor:
    """Stateless rule evaluation against an :class:`AccountSnapshot`."""

    def __init__(self, profile: AccountProfile, policy: GovernorPolicy | None = None) -> None:
        self.profile = profile
        self.policy = policy or GovernorPolicy()
        limit = profile.consistency_max_day_share
        if limit is not None and self.policy.consistency_cushion >= limit:
            raise ValueError(
                f"consistency_cushion {self.policy.consistency_cushion} must be smaller than the profile's "
                f"single-day share {limit}")

    # -- money ---------------------------------------------------------------------------------

    def floor(self, highest_closing_balance: Decimal) -> Decimal:
        """The balance at which the account breaches (trails the highest close, then locks)."""
        return self.profile.trailing_threshold(highest_closing_balance)

    def day_budget(self, snapshot: AccountSnapshot) -> DayBudget:
        """Fix the day's loss budget, per-trade risk cap and profit cap.

        Everything is a fraction of the buffer above the floor at the START of the day, so a
        string of maximum-loss days can only shrink the buffer geometrically and never reach
        the floor (apart from gaps beyond a stop, which the hard stop margin absorbs).
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
        if target_reached and consistent:
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
        add(CODE_MICROSCALP, snapshot.micro_profit_share < policy.micro_profit_share_limit,
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
        if _new_york_time(snapshot.now_ns) >= policy.flat_time:
            return PositionVerdict(
                PositionAction.FLATTEN, "governor_flat_time",
                f"{policy.flat_time:%H:%M} New York: flat before Lucid's auto-close")
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
    edge_cache: EdgeEvidence | None = None   # recomputed only when a trade closes (snapshots are hot)

    def __post_init__(self) -> None:
        """Start from the profile's balance unless told otherwise."""
        if self.balance == ZERO:
            self.balance = self.profile.account_size
        if self.highest_closing_balance == ZERO:
            self.highest_closing_balance = self.balance
        self.day_start_balance = self.balance

    def roll_day(self, trading_day: str) -> None:
        """Start a new trading day: bank the old day's close and reset the day's counters."""
        if not trading_day or trading_day == self.day:
            return
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

    def record_entry(self, ts_ns: int) -> None:
        """Count an entry that was actually filled."""
        self.day_entries += 1
        self.last_entry_ts_ns = ts_ns

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

    def snapshot(self, now_ns: int, open_pnl: Decimal = ZERO) -> AccountSnapshot:
        """An immutable view of the account for the rules to read."""
        share = (self.micro_profit / self.total_profit) if self.total_profit > 0 else ZERO
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
        )


# --- loading ---------------------------------------------------------------------------------


def _new_york_time(ts_ns: int) -> time:
    moment = datetime.fromtimestamp(ts_ns // _NS, tz=UTC).astimezone(NEW_YORK)
    return moment.time().replace(microsecond=0, tzinfo=None)


def load_governor_policy(path: Path) -> GovernorPolicy:
    """Read ``config/risk_governor.yaml``; a missing file means the safe defaults, a bad one raises.

    Unknown keys are rejected so a typo can never silently leave a rule at its default.
    """
    import yaml

    defaults = GovernorPolicy()
    if not path.is_file():
        return defaults
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected a mapping of governor settings")
    allowed = set(GovernorPolicy.__dataclass_fields__)
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"{path}: unknown governor setting(s): {', '.join(unknown)}")
    values: dict[str, object] = {}
    for key, raw in payload.items():
        current = getattr(defaults, key)
        if isinstance(current, bool):
            values[key] = bool(raw)
        elif isinstance(current, Decimal):
            values[key] = Decimal(str(raw))
        elif isinstance(current, int):
            values[key] = int(raw)
        elif isinstance(current, time):
            hours, minutes = str(raw).split(":")[:2]
            values[key] = time(int(hours), int(minutes))
        else:  # pragma: no cover - every field is one of the types above
            values[key] = raw
    return GovernorPolicy(**values)  # type: ignore[arg-type]
