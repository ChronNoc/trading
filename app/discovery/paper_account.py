"""Paper-trading account simulator using the mentor's risk rules.

Starts a $100k simulated account, sizes each trade by the risk engine,
enforces the mentor's rules (10-point stop, targets in R, max 3 trades
and 3 losses per day, 1% daily-risk lock), and tracks every simulated
win/loss. When an account is blown, a fresh $100k account opens; the run
continues until an account survives the trade sequence profitably or the
attempt cap is reached.

SIMULATION ONLY. All money math is Decimal. The trade outcomes fed in are
whatever the caller supplies - synthetic discovery episodes today, real
Stage-C setups later. This never places an order.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from app.discovery.metrics import TradeResult
from app.risk.sizing import SizingInputs, calculate_position_size

# MNQ contract economics and the mentor's rules (see LESSON_NOTES.md).
MNQ_TICK_VALUE = Decimal("0.50")
MNQ_TICKS_PER_POINT = Decimal("4")
MENTOR_STOP_POINTS = Decimal("10")
DEFAULT_STARTING_BALANCE = Decimal("100000")
DEFAULT_MAX_TRADES_PER_DAY = 3
DEFAULT_MAX_LOSSES_PER_DAY = 3
DEFAULT_COMMISSION = Decimal("1.24")
DEFAULT_SLIPPAGE_TICKS = Decimal("1")


@dataclass(frozen=True, slots=True)
class SimulatedTrade:
    """One simulated trade outcome in an account."""

    account_number: int
    trade_date: str
    contracts: int
    r_multiple: Decimal
    pnl: Decimal
    balance_after: Decimal
    won: bool
    reason: str


@dataclass(frozen=True, slots=True)
class AccountResult:
    """Outcome of one $100k account attempt."""

    account_number: int
    starting_balance: Decimal
    ending_balance: Decimal
    trades: tuple[SimulatedTrade, ...]
    blew_up: bool
    wins: int
    losses: int

    @property
    def net_pnl(self) -> Decimal:
        """Profit or loss over the account's life."""
        return self.ending_balance - self.starting_balance

    @property
    def win_rate(self) -> Decimal:
        """Fraction of trades that won."""
        total = self.wins + self.losses
        return (Decimal(self.wins) / Decimal(total)).quantize(Decimal("0.0001")) if total else Decimal("0")


@dataclass(frozen=True, slots=True)
class PaperRunResult:
    """Full result of running the trade sequence across reset accounts."""

    accounts: tuple[AccountResult, ...]
    profitable: bool
    attempts_until_profit: int | None
    starting_balance: Decimal

    def all_trades(self) -> tuple[SimulatedTrade, ...]:
        """Every simulated trade across every account attempt."""
        return tuple(trade for account in self.accounts for trade in account.trades)


@dataclass(frozen=True, slots=True)
class PaperAccountConfig:
    """Configuration for the paper-account simulator."""

    starting_balance: Decimal = DEFAULT_STARTING_BALANCE
    stop_points: Decimal = MENTOR_STOP_POINTS
    tick_value: Decimal = MNQ_TICK_VALUE
    ticks_per_point: Decimal = MNQ_TICKS_PER_POINT
    max_trades_per_day: int = DEFAULT_MAX_TRADES_PER_DAY
    max_losses_per_day: int = DEFAULT_MAX_LOSSES_PER_DAY
    daily_loss_fraction: Decimal = Decimal("0.03")
    commission: Decimal = DEFAULT_COMMISSION
    slippage_ticks: Decimal = DEFAULT_SLIPPAGE_TICKS
    max_account_attempts: int = 50

    @property
    def stop_distance_ticks(self) -> Decimal:
        """The mentor's stop in ticks (10 points x 4 ticks/point = 40)."""
        return self.stop_points * self.ticks_per_point

    def risk_per_contract(self) -> Decimal:
        """Dollar risk of one contract at the mentor's stop plus costs."""
        return (
            self.stop_distance_ticks * self.tick_value
            + self.commission
            + self.slippage_ticks * self.tick_value
        )


@dataclass(slots=True)
class _DayState:
    """Per-day counters enforcing the mentor's daily limits."""

    date: str
    trades: int = 0
    losses: int = 0
    realized_loss: Decimal = field(default_factory=lambda: Decimal("0"))


def _contracts_for(balance: Decimal, config: PaperAccountConfig) -> int:
    """Size the position from current balance via the risk engine."""
    if balance <= 0:
        return 0
    inputs = SizingInputs(
        account_size=balance,
        remaining_allowable_drawdown=balance,
        stop_distance_ticks=config.stop_distance_ticks,
        tick_value=config.tick_value,
        estimated_commission=config.commission,
        estimated_slippage=config.slippage_ticks * config.tick_value,
    )
    return calculate_position_size(inputs).contracts


def _simulate_one_account(
    account_number: int,
    trades: Sequence[TradeResult],
    config: PaperAccountConfig,
) -> tuple[AccountResult, int]:
    """Run trades on one account until blowup or sequence end.

    Returns the account result and how many trades of the sequence it
    consumed, so the next account resumes where this one blew up.
    """
    balance = config.starting_balance
    risk_per_contract = config.risk_per_contract()
    simulated: list[SimulatedTrade] = []
    wins = losses = 0
    day: _DayState | None = None
    consumed = 0

    for trade in trades:
        consumed += 1
        if day is None or day.date != trade.trade_date:
            day = _DayState(date=trade.trade_date)

        daily_loss_cap = config.starting_balance * config.daily_loss_fraction
        if day.trades >= config.max_trades_per_day:
            continue
        if day.losses >= config.max_losses_per_day:
            continue
        if day.realized_loss >= daily_loss_cap:
            continue

        contracts = _contracts_for(balance, config)
        if contracts <= 0:
            # Balance too small to risk-size even one contract: the account
            # can no longer trade the mentor's rules - blown in practice.
            return (
                AccountResult(
                    account_number=account_number,
                    starting_balance=config.starting_balance,
                    ending_balance=balance,
                    trades=tuple(simulated),
                    blew_up=True,
                    wins=wins,
                    losses=losses,
                ),
                consumed - 1,
            )

        # R-multiple -> dollars: 1R = risk_per_contract, scaled by size.
        pnl = (trade.r_multiple * risk_per_contract * Decimal(contracts)).quantize(Decimal("0.01"))
        balance += pnl
        won = pnl > 0
        wins += 1 if won else 0
        losses += 0 if won else 1
        day.trades += 1
        if not won:
            day.losses += 1
            day.realized_loss += -pnl
        simulated.append(
            SimulatedTrade(
                account_number=account_number,
                trade_date=trade.trade_date,
                contracts=contracts,
                r_multiple=trade.r_multiple,
                pnl=pnl,
                balance_after=balance,
                won=won,
                reason="win" if won else "loss",
            ),
        )
        if balance <= 0:
            return (
                AccountResult(
                    account_number=account_number,
                    starting_balance=config.starting_balance,
                    ending_balance=balance,
                    trades=tuple(simulated),
                    blew_up=True,
                    wins=wins,
                    losses=losses,
                ),
                consumed,
            )

    return (
        AccountResult(
            account_number=account_number,
            starting_balance=config.starting_balance,
            ending_balance=balance,
            trades=tuple(simulated),
            blew_up=False,
            wins=wins,
            losses=losses,
        ),
        consumed,
    )


def run_paper_accounts(
    trades: Sequence[TradeResult],
    config: PaperAccountConfig | None = None,
) -> PaperRunResult:
    """Run the trade sequence across reset $100k accounts until profitable.

    Each blown account is followed by a fresh $100k account resuming from
    where the previous one blew up. The run stops when an account finishes
    the remaining sequence with a net profit, or the attempt cap is hit.
    """
    settings = config or PaperAccountConfig()
    accounts: list[AccountResult] = []
    remaining = list(trades)
    attempts_until_profit: int | None = None

    for attempt in range(1, settings.max_account_attempts + 1):
        if not remaining:
            break
        account, consumed = _simulate_one_account(attempt, remaining, settings)
        accounts.append(account)
        if not account.blew_up:
            if account.net_pnl > 0:
                attempts_until_profit = attempt
            break
        remaining = remaining[consumed:]

    profitable = attempts_until_profit is not None
    return PaperRunResult(
        accounts=tuple(accounts),
        profitable=profitable,
        attempts_until_profit=attempts_until_profit,
        starting_balance=settings.starting_balance,
    )
