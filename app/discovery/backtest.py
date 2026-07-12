"""Deterministic backtest of one parameter set over setup episodes.

Costs (commission + slippage) are applied to every simulated trade, with
an explicit worst-case pass available - item 19. All money math is
Decimal.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.discovery.episodes import SetupEpisode
from app.discovery.metrics import TradeResult


@dataclass(frozen=True, slots=True)
class CostModel:
    """Commission and slippage expressed in R-fractions per round trip."""

    commission_r: Decimal = Decimal("0.02")
    slippage_r: Decimal = Decimal("0.05")
    worst_case_multiplier: Decimal = Decimal("3")

    def round_trip_cost(self, *, worst_case: bool) -> Decimal:
        """Total R cost of one round trip."""
        cost = self.commission_r + self.slippage_r
        return cost * self.worst_case_multiplier if worst_case else cost


@dataclass(frozen=True, slots=True)
class ParameterSet:
    """One point in the strategy parameter grid."""

    min_reload_count: int
    min_aggressive_volume: Decimal
    min_ask_pull_ratio: Decimal
    target_r: Decimal
    stop_r: Decimal = Decimal("1")

    def key(self) -> str:
        """Stable dedupe key: quantized parameter values."""
        return (
            f"reload={self.min_reload_count}|volume={self.min_aggressive_volume.quantize(Decimal('1'))}"
            f"|pull={self.min_ask_pull_ratio.quantize(Decimal('0.01'))}"
            f"|target={self.target_r.quantize(Decimal('0.1'))}|stop={self.stop_r.quantize(Decimal('0.1'))}"
        )


def run_backtest(
    parameters: ParameterSet,
    episodes: Sequence[SetupEpisode],
    *,
    costs: CostModel | None = None,
    worst_case: bool = False,
) -> tuple[TradeResult, ...]:
    """Simulate the parameter set over episodes and return costed trades.

    An episode trades only when every threshold passes at decision time.
    The outcome is the episode's forward R path clipped by the stop and
    target, minus round-trip costs.
    """
    cost_model = costs or CostModel()
    cost = cost_model.round_trip_cost(worst_case=worst_case)
    trades: list[TradeResult] = []
    for episode in episodes:
        if episode.reload_count < parameters.min_reload_count:
            continue
        if episode.aggressive_volume < parameters.min_aggressive_volume:
            continue
        if episode.ask_pull_ratio < parameters.min_ask_pull_ratio:
            continue
        raw = episode.raw_r_outcome
        clipped = max(min(raw, parameters.target_r), -parameters.stop_r)
        trades.append(
            TradeResult(
                r_multiple=clipped - cost,
                session=episode.session,
                regime_tag=episode.regime_tag,
                trade_date=episode.episode_date.isoformat(),
            ),
        )
    return tuple(trades)
