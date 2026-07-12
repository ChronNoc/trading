"""Composite performance metrics for candidate strategies.

Item 13 of the upgrade: win rate alone is never the score. Every
candidate reports win rate, profit factor, max drawdown, Sortino ratio,
and expectancy in R-multiples individually; the composite ranking value
exists only alongside its components, never instead of them.
All arithmetic uses Decimal.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class TradeResult:
    """One simulated trade outcome in R-multiples with its regime tags."""

    r_multiple: Decimal
    session: str
    regime_tag: str
    trade_date: str


@dataclass(frozen=True, slots=True)
class CompositeScore:
    """Full metric breakdown for one candidate. Never collapse silently."""

    trade_count: int
    win_rate: Decimal
    profit_factor: Decimal
    max_drawdown_r: Decimal
    sortino: Decimal
    expectancy_r: Decimal
    composite: Decimal

    def component_lines(self) -> tuple[str, ...]:
        """Render every component for reports and the GUI leaderboard."""
        return (
            f"trades: {self.trade_count}",
            f"win rate: {self.win_rate}",
            f"profit factor: {self.profit_factor}",
            f"max drawdown (R): {self.max_drawdown_r}",
            f"sortino: {self.sortino}",
            f"expectancy (R): {self.expectancy_r}",
            f"composite: {self.composite}",
        )


def win_rate(r_multiples: Sequence[Decimal]) -> Decimal:
    """Fraction of trades with positive R."""
    if not r_multiples:
        return Decimal("0")
    wins = sum(1 for value in r_multiples if value > 0)
    return (Decimal(wins) / Decimal(len(r_multiples))).quantize(Decimal("0.0001"))


def profit_factor(r_multiples: Sequence[Decimal]) -> Decimal:
    """Gross positive R divided by gross negative R (capped when no losses)."""
    gross_win = sum((value for value in r_multiples if value > 0), Decimal("0"))
    gross_loss = -sum((value for value in r_multiples if value < 0), Decimal("0"))
    if gross_loss == 0:
        return Decimal("99.9999") if gross_win > 0 else Decimal("0")
    return (gross_win / gross_loss).quantize(Decimal("0.0001"))


def max_drawdown_r(r_multiples: Sequence[Decimal]) -> Decimal:
    """Largest peak-to-trough drop of the cumulative R curve."""
    peak = Decimal("0")
    equity = Decimal("0")
    worst = Decimal("0")
    for value in r_multiples:
        equity += value
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst.quantize(Decimal("0.0001"))


def sortino(r_multiples: Sequence[Decimal]) -> Decimal:
    """Mean R over downside deviation (0 when no downside exists)."""
    if not r_multiples:
        return Decimal("0")
    mean = sum(r_multiples, Decimal("0")) / Decimal(len(r_multiples))
    downside = [value for value in r_multiples if value < 0]
    if not downside:
        return Decimal("99.9999") if mean > 0 else Decimal("0")
    downside_variance = sum((value * value for value in downside), Decimal("0")) / Decimal(len(r_multiples))
    downside_deviation = downside_variance.sqrt()
    if downside_deviation == 0:
        return Decimal("0")
    return (mean / downside_deviation).quantize(Decimal("0.0001"))


def expectancy_r(r_multiples: Sequence[Decimal]) -> Decimal:
    """Mean R per trade."""
    if not r_multiples:
        return Decimal("0")
    return (sum(r_multiples, Decimal("0")) / Decimal(len(r_multiples))).quantize(Decimal("0.0001"))


def composite_score(trades: Sequence[TradeResult]) -> CompositeScore:
    """Compute the full metric set and a bounded composite ranking value.

    The composite weights expectancy and Sortino, rewards profit factor,
    and penalizes drawdown. It exists for *ranking* candidates - gates and
    reports always use the individual components.
    """
    r_values = [trade.r_multiple for trade in trades]
    rate = win_rate(r_values)
    factor = profit_factor(r_values)
    drawdown = max_drawdown_r(r_values)
    sortino_ratio = sortino(r_values)
    expectancy = expectancy_r(r_values)
    composite = (
        expectancy * Decimal("2")
        + min(sortino_ratio, Decimal("3")) * Decimal("0.5")
        + min(factor, Decimal("3")) * Decimal("0.25")
        - drawdown * Decimal("0.05")
    ).quantize(Decimal("0.0001"))
    return CompositeScore(
        trade_count=len(trades),
        win_rate=rate,
        profit_factor=factor,
        max_drawdown_r=drawdown,
        sortino=sortino_ratio,
        expectancy_r=expectancy,
        composite=composite,
    )
