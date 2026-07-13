"""Consistency scoring for a paper-trading run.

Turns a :class:`PaperRunResult` into plain-English progress: how many
$100k accounts it took to reach profitability, win rate, expectancy in R,
worst losing streak, and a bounded 0-100 consistency score. All Decimal.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.discovery.paper_account import PaperRunResult, SimulatedTrade


@dataclass(frozen=True, slots=True)
class ConsistencyReport:
    """Progress summary of a paper-trading run."""

    total_trades: int
    accounts_used: int
    profitable: bool
    attempts_until_profit: int | None
    win_rate: Decimal
    expectancy_r: Decimal
    max_win_streak: int
    max_loss_streak: int
    net_pnl: Decimal
    consistency_score: Decimal

    def summary_lines(self) -> tuple[str, ...]:
        """Human-readable progress lines for the GUI."""
        profit = (
            f"profitable after {self.attempts_until_profit} account(s)"
            if self.profitable
            else f"not yet profitable across {self.accounts_used} account(s)"
        )
        return (
            f"Status: {profit}",
            f"Trades taken: {self.total_trades}",
            f"Win rate: {self.win_rate}",
            f"Expectancy: {self.expectancy_r} R/trade",
            f"Longest win streak: {self.max_win_streak}",
            f"Longest loss streak: {self.max_loss_streak}",
            f"Net P&L: ${self.net_pnl}",
            f"Consistency score: {self.consistency_score} / 100",
        )


def _streaks(trades: tuple[SimulatedTrade, ...]) -> tuple[int, int]:
    """Return (max win streak, max loss streak)."""
    max_win = max_loss = win = loss = 0
    for trade in trades:
        if trade.won:
            win += 1
            loss = 0
        else:
            loss += 1
            win = 0
        max_win = max(max_win, win)
        max_loss = max(max_loss, loss)
    return max_win, max_loss


def score_run(result: PaperRunResult) -> ConsistencyReport:
    """Compute the consistency report for a paper run."""
    trades = result.all_trades()
    total = len(trades)
    wins = sum(1 for trade in trades if trade.won)
    win_rate = (Decimal(wins) / Decimal(total)).quantize(Decimal("0.0001")) if total else Decimal("0")
    r_values = [trade.r_multiple for trade in trades]
    expectancy = (
        (sum(r_values, Decimal("0")) / Decimal(total)).quantize(Decimal("0.0001")) if total else Decimal("0")
    )
    max_win, max_loss = _streaks(trades)
    net_pnl = (
        result.accounts[-1].ending_balance - result.starting_balance if result.accounts else Decimal("0")
    ).quantize(Decimal("0.01"))

    # Consistency score: reward profitability with few resets and positive
    # expectancy, penalize long losing streaks and many blown accounts.
    blown = sum(1 for account in result.accounts if account.blew_up)
    score = Decimal("0")
    if result.profitable:
        score += Decimal("50")
        score += max(Decimal("0"), Decimal("30") - Decimal(blown) * Decimal("10"))
    score += min(Decimal("15"), max(Decimal("0"), expectancy * Decimal("30")))
    score -= min(Decimal("15"), Decimal(max_loss) * Decimal("2"))
    score = max(Decimal("0"), min(Decimal("100"), score)).quantize(Decimal("0.1"))

    return ConsistencyReport(
        total_trades=total,
        accounts_used=len(result.accounts),
        profitable=result.profitable,
        attempts_until_profit=result.attempts_until_profit,
        win_rate=win_rate,
        expectancy_r=expectancy,
        max_win_streak=max_win,
        max_loss_streak=max_loss,
        net_pnl=net_pnl,
        consistency_score=score,
    )
