"""Past-only market regime classification."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from statistics import mean
from typing import Sequence

from app.market.features import calculate_short_term_volatility
from app.market.state import MarketState


class VolatilityRegime(StrEnum):
    """Volatility buckets used by profile routing."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    EXTREME = "extreme"
    UNKNOWN = "unknown"


class LiquidityRegime(StrEnum):
    """Liquidity buckets used by profile routing."""

    THIN = "thin"
    NORMAL = "normal"
    DEEP = "deep"
    UNKNOWN = "unknown"


class BehaviorRegime(StrEnum):
    """Behavior buckets used by profile routing."""

    TRENDING = "trending"
    ROTATIONAL = "rotational"
    UNSTABLE = "unstable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RegimeClassification:
    """Current market regime and decision gating reason."""

    volatility: VolatilityRegime
    liquidity: LiquidityRegime
    behavior: BehaviorRegime
    sample_count: int
    decisions_allowed: bool
    reason: str

    @property
    def summary(self) -> str:
        """Return a compact GUI display string."""
        return f"{self.volatility}/{self.liquidity}/{self.behavior}"


@dataclass(frozen=True, slots=True)
class RegimeClassifier:
    """Classify regime using only current and past market states."""

    min_samples: int = 5
    stale_after_ns: int = 5_000_000_000
    low_volatility: Decimal = Decimal("0.15")
    high_volatility: Decimal = Decimal("0.80")
    extreme_volatility: Decimal = Decimal("1.50")
    thin_liquidity: Decimal = Decimal("50")
    deep_liquidity: Decimal = Decimal("250")

    def classify(
        self,
        snapshots: Sequence[MarketState],
        *,
        current_timestamp_ns: int | None = None,
    ) -> RegimeClassification:
        """Return a regime classification from a rolling market-state window."""
        if len(snapshots) < self.min_samples:
            return self._unknown(len(snapshots), "insufficient samples for regime classification")

        last = snapshots[-1]
        if last.mid_price is None or last.spread is None:
            return self._unknown(len(snapshots), "incomplete book state")

        if current_timestamp_ns is not None and current_timestamp_ns - last.timestamp_ns > self.stale_after_ns:
            return self._unknown(len(snapshots), "market data is stale")

        volatility_value = calculate_short_term_volatility(snapshots)
        volatility = self._volatility_bucket(volatility_value)
        liquidity = self._liquidity_bucket(snapshots)
        behavior = self._behavior_bucket(snapshots, volatility_value)
        decisions_allowed = BehaviorRegime.UNKNOWN not in {behavior} and LiquidityRegime.UNKNOWN not in {liquidity}
        decisions_allowed = decisions_allowed and volatility != VolatilityRegime.UNKNOWN and behavior != BehaviorRegime.UNSTABLE
        reason = "regime classified from current session samples" if decisions_allowed else "regime blocks decisions"
        return RegimeClassification(
            volatility=volatility,
            liquidity=liquidity,
            behavior=behavior,
            sample_count=len(snapshots),
            decisions_allowed=decisions_allowed,
            reason=reason,
        )

    def _unknown(self, sample_count: int, reason: str) -> RegimeClassification:
        return RegimeClassification(
            volatility=VolatilityRegime.UNKNOWN,
            liquidity=LiquidityRegime.UNKNOWN,
            behavior=BehaviorRegime.UNKNOWN,
            sample_count=sample_count,
            decisions_allowed=False,
            reason=reason,
        )

    def _volatility_bucket(self, volatility_value: Decimal) -> VolatilityRegime:
        if volatility_value >= self.extreme_volatility:
            return VolatilityRegime.EXTREME
        if volatility_value >= self.high_volatility:
            return VolatilityRegime.HIGH
        if volatility_value <= self.low_volatility:
            return VolatilityRegime.LOW
        return VolatilityRegime.NORMAL

    def _liquidity_bucket(self, snapshots: Sequence[MarketState]) -> LiquidityRegime:
        top_depth_values = []
        for snapshot in snapshots:
            if snapshot.bid_depth and snapshot.ask_depth:
                top_depth_values.append(snapshot.bid_depth[0].size + snapshot.ask_depth[0].size)
        if not top_depth_values:
            return LiquidityRegime.UNKNOWN
        average_depth = sum(top_depth_values, Decimal("0")) / Decimal(len(top_depth_values))
        if average_depth < self.thin_liquidity:
            return LiquidityRegime.THIN
        if average_depth > self.deep_liquidity:
            return LiquidityRegime.DEEP
        return LiquidityRegime.NORMAL

    def _behavior_bucket(self, snapshots: Sequence[MarketState], volatility_value: Decimal) -> BehaviorRegime:
        mid_prices = [snapshot.mid_price for snapshot in snapshots if snapshot.mid_price is not None]
        if len(mid_prices) < self.min_samples:
            return BehaviorRegime.UNKNOWN
        price_range = max(mid_prices) - min(mid_prices)
        net_change = mid_prices[-1] - mid_prices[0]
        average_spread = Decimal(str(mean(float(snapshot.spread or Decimal("0")) for snapshot in snapshots)))
        if average_spread > Decimal("1.25") or volatility_value >= self.extreme_volatility:
            return BehaviorRegime.UNSTABLE
        if abs(net_change) >= max(volatility_value * Decimal("2"), Decimal("0.50")):
            return BehaviorRegime.TRENDING
        if price_range <= max(volatility_value * Decimal("3"), Decimal("0.75")):
            return BehaviorRegime.ROTATIONAL
        return BehaviorRegime.UNKNOWN

