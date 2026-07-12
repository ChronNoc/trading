"""Model decay monitor - item 24.

Tracks a deployed-in-sim candidate's rolling performance against the
validation statistics it was accepted with, and flags when drift is large
enough to require retirement or retraining. Flagging is all it does - it
never retrains or redeploys anything on its own.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from app.discovery.metrics import expectancy_r


@dataclass(frozen=True, slots=True)
class DecayReport:
    """Rolling-performance drift verdict."""

    drifted: bool
    reason: str
    rolling_expectancy_r: Decimal
    baseline_expectancy_r: Decimal
    observed_trades: int


class DecayMonitor:
    """Compares rolling live-sim expectancy against the validation baseline."""

    def __init__(
        self,
        *,
        baseline_expectancy_r: Decimal,
        window: int = 50,
        max_drop_r: Decimal = Decimal("0.3"),
        min_observations: int = 20,
    ) -> None:
        """Create a monitor around the accepted candidate's baseline."""
        if window <= 0 or min_observations <= 0:
            raise ValueError("window and min_observations must be positive")
        self.baseline_expectancy_r = baseline_expectancy_r
        self.max_drop_r = max_drop_r
        self.min_observations = min_observations
        self._window: deque[Decimal] = deque(maxlen=window)
        self._observed = 0

    def observe(self, r_multiple: Decimal) -> DecayReport:
        """Record one live-sim trade outcome and re-evaluate drift."""
        self._window.append(r_multiple)
        self._observed += 1
        rolling = expectancy_r(tuple(self._window))
        if self._observed < self.min_observations:
            return DecayReport(
                drifted=False,
                reason=f"warming up: {self._observed}/{self.min_observations} observations",
                rolling_expectancy_r=rolling,
                baseline_expectancy_r=self.baseline_expectancy_r,
                observed_trades=self._observed,
            )
        drop = self.baseline_expectancy_r - rolling
        if drop > self.max_drop_r:
            return DecayReport(
                drifted=True,
                reason=(
                    f"rolling expectancy {rolling}R is {drop}R below validation baseline "
                    f"{self.baseline_expectancy_r}R - candidate needs retirement or retraining"
                ),
                rolling_expectancy_r=rolling,
                baseline_expectancy_r=self.baseline_expectancy_r,
                observed_trades=self._observed,
            )
        return DecayReport(
            drifted=False,
            reason=f"rolling expectancy {rolling}R within {self.max_drop_r}R of baseline",
            rolling_expectancy_r=rolling,
            baseline_expectancy_r=self.baseline_expectancy_r,
            observed_trades=self._observed,
        )
