"""One fixed $100,000 paper ledger for quality-gated real outcomes only."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from app.research.real_episodes import CompletedRealOutcome
from app.risk.sizing import SizingInputs, calculate_position_size

MNQ_TICK_SIZE = Decimal("0.25")
MNQ_TICK_VALUE = Decimal("0.50")


@dataclass(frozen=True, slots=True)
class RealPaperLedgerConfig:
    """Fixed-account and daily-lock assumptions for offline paper results."""

    starting_balance: Decimal = Decimal("100000")
    remaining_allowable_drawdown: Decimal = Decimal("100000")
    max_entries_per_day: int = 3
    max_losses_per_day: int = 3

    def __post_init__(self) -> None:
        """Validate paper-account assumptions."""
        if self.starting_balance <= 0 or self.remaining_allowable_drawdown <= 0:
            raise ValueError("account and drawdown amounts must be positive")
        if self.max_entries_per_day <= 0 or self.max_losses_per_day <= 0:
            raise ValueError("daily entry and loss limits must be positive")


@dataclass(frozen=True, slots=True)
class RealPaperTrade:
    """One ledger row retaining all real episode identifiers and economics."""

    session_id: str
    setup_id: str
    provenance: str
    trading_day: str
    direction: str
    decision_ts_ns: int
    entry_ts_ns: int
    exit_ts_ns: int
    entry: Decimal
    stop: Decimal
    target: Decimal
    exit: Decimal
    costs_per_contract: Decimal
    contracts: int
    r_multiple: Decimal
    pnl: Decimal
    balance_after: Decimal
    outcome: str
    input_hash: str

    @property
    def won(self) -> bool:
        """Return whether this row realized positive net P&L."""
        return self.pnl > 0


@dataclass(frozen=True, slots=True)
class SkippedPaperOutcome:
    """An eligible outcome that the fixed account could not take."""

    session_id: str
    setup_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class RealPaperLedgerResult:
    """Complete one-account result with no reset or survivorship loop."""

    starting_balance: Decimal
    ending_balance: Decimal
    trades: tuple[RealPaperTrade, ...]
    skipped: tuple[SkippedPaperOutcome, ...]
    stopped: bool

    @property
    def net_pnl(self) -> Decimal:
        """Return fixed-account net P&L."""
        return self.ending_balance - self.starting_balance

    @property
    def wins(self) -> int:
        """Return the count of profitable ledger trades."""
        return sum(1 for trade in self.trades if trade.won)

    @property
    def losses(self) -> int:
        """Return the count of non-profitable ledger trades."""
        return sum(1 for trade in self.trades if not trade.won)

    @property
    def win_rate(self) -> Decimal:
        """Return the observed win fraction, or zero for an empty ledger."""
        if not self.trades:
            return Decimal("0")
        return (Decimal(self.wins) / Decimal(len(self.trades))).quantize(Decimal("0.0001"))


def run_real_paper_ledger(
    outcomes: Sequence[CompletedRealOutcome],
    config: RealPaperLedgerConfig | None = None,
) -> RealPaperLedgerResult:
    """Run quality-gated outcomes through one chronological account, no resets."""
    settings = config or RealPaperLedgerConfig()
    balance = settings.starting_balance
    trades: list[RealPaperTrade] = []
    skipped: list[SkippedPaperOutcome] = []
    day = ""
    day_entries = 0
    day_losses = 0
    day_realized_loss = Decimal("0")
    open_until_ns = 0
    stopped = False

    for outcome in sorted(outcomes, key=lambda item: (item.entry_ts_ns, item.session_id, item.setup_id)):
        if not outcome.eligible_for_ledger:
            continue
        if outcome.trading_day != day:
            day = outcome.trading_day
            day_entries = 0
            day_losses = 0
            day_realized_loss = Decimal("0")
        if stopped:
            skipped.append(_skip(outcome, "account stopped after balance reached zero"))
            continue
        if outcome.entry_ts_ns < open_until_ns:
            skipped.append(_skip(outcome, "one-position maximum: prior paper trade still open"))
            continue

        stop_ticks = abs(outcome.entry - outcome.stop) / MNQ_TICK_SIZE
        sizing = calculate_position_size(
            SizingInputs(
                account_size=max(balance, Decimal("0")),
                remaining_allowable_drawdown=settings.remaining_allowable_drawdown,
                stop_distance_ticks=stop_ticks,
                tick_value=MNQ_TICK_VALUE,
                estimated_commission=outcome.commission,
                estimated_slippage=outcome.slippage_cost,
            ),
        )
        if day_entries >= settings.max_entries_per_day:
            skipped.append(_skip(outcome, "daily entry lock reached"))
            continue
        if day_losses >= settings.max_losses_per_day:
            skipped.append(_skip(outcome, "daily losing-trade lock reached"))
            continue
        if day_realized_loss >= sizing.daily_risk_budget:
            skipped.append(_skip(outcome, "daily loss-budget lock reached"))
            continue
        if sizing.contracts <= 0:
            skipped.append(_skip(outcome, "risk sizing allowed zero contracts"))
            continue

        pnl = (outcome.net_pnl_per_contract * Decimal(sizing.contracts)).quantize(Decimal("0.01"))
        balance += pnl
        day_entries += 1
        if pnl <= 0:
            day_losses += 1
            day_realized_loss += -pnl
        open_until_ns = outcome.exit_ts_ns
        trades.append(
            RealPaperTrade(
                session_id=outcome.session_id,
                setup_id=outcome.setup_id,
                provenance=outcome.provenance,
                trading_day=outcome.trading_day,
                direction=outcome.direction,
                decision_ts_ns=outcome.decision_ts_ns,
                entry_ts_ns=outcome.entry_ts_ns,
                exit_ts_ns=outcome.exit_ts_ns,
                entry=outcome.entry,
                stop=outcome.stop,
                target=outcome.target,
                exit=outcome.exit,
                costs_per_contract=outcome.commission + outcome.slippage_cost,
                contracts=sizing.contracts,
                r_multiple=outcome.r_multiple,
                pnl=pnl,
                balance_after=balance,
                outcome=outcome.outcome,
                input_hash=outcome.input_hash,
            ),
        )
        if balance <= 0:
            stopped = True

    return RealPaperLedgerResult(
        starting_balance=settings.starting_balance,
        ending_balance=balance,
        trades=tuple(trades),
        skipped=tuple(skipped),
        stopped=stopped,
    )


def real_paper_summary(result: RealPaperLedgerResult) -> tuple[str, ...]:
    """Return honest GUI summary lines for one fixed real-data ledger."""
    expectancy = (
        sum((trade.r_multiple for trade in result.trades), Decimal("0")) / Decimal(len(result.trades))
        if result.trades
        else Decimal("0")
    )
    status = "stopped" if result.stopped else "active/survived available outcomes"
    return (
        "Real Bookmap outcomes only; one fixed account, no resets.",
        f"Status: {status}",
        f"Trades taken: {len(result.trades)}",
        f"Outcomes skipped by account rules: {len(result.skipped)}",
        f"Win rate: {result.win_rate}",
        f"Net expectancy: {expectancy.quantize(Decimal('0.0001'))} R/trade",
        f"Starting balance: ${result.starting_balance.quantize(Decimal('0.01'))}",
        f"Ending balance: ${result.ending_balance.quantize(Decimal('0.01'))}",
        f"Net P&L: ${result.net_pnl.quantize(Decimal('0.01'))}",
    )


def _skip(outcome: CompletedRealOutcome, reason: str) -> SkippedPaperOutcome:
    return SkippedPaperOutcome(session_id=outcome.session_id, setup_id=outcome.setup_id, reason=reason)
