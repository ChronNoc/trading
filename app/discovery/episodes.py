"""Setup episodes: the unit of data the discovery pipeline backtests over.

An episode is one deterministic candidate moment - features measured at
decision time plus the forward outcome path. Until real Bookmap sessions
exist (Stage C), a seeded synthetic generator produces episodes so the
pipeline machinery is fully exercisable; every synthetic episode is
marked ``synthetic=True`` and the generator never enters real training.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

REGIME_TRENDING = "trending"
REGIME_RANGING = "ranging"
VOLATILITY_HIGH = "high_vol"
VOLATILITY_LOW = "low_vol"
SESSIONS = ("london", "new_york_open", "new_york_afternoon")


@dataclass(frozen=True, slots=True)
class SetupEpisode:
    """One candidate setup moment with decision-time features and outcome."""

    episode_date: date
    session: str
    regime: str
    volatility: str
    aggressive_volume: Decimal
    reload_count: int
    ask_pull_ratio: Decimal
    reclaim_ticks: Decimal
    raw_r_outcome: Decimal
    synthetic: bool = True

    @property
    def regime_tag(self) -> str:
        """Combined regime/volatility tag used for per-regime reporting."""
        return f"{self.regime}/{self.volatility}"


def generate_synthetic_episodes(
    *,
    seed: int,
    start_date: date,
    trading_days: int,
    episodes_per_day: int = 4,
) -> tuple[SetupEpisode, ...]:
    """Generate a deterministic synthetic episode history.

    Structure is intentional, not random noise: episodes with strong
    reloads and reclaims skew positive so threshold-style parameters have
    a real (synthetic) edge to find, while weak episodes skew negative.
    """
    if trading_days <= 0 or episodes_per_day <= 0:
        raise ValueError("trading_days and episodes_per_day must be positive")
    rng = random.Random(seed)
    episodes: list[SetupEpisode] = []
    for day_index in range(trading_days):
        episode_date = start_date + timedelta(days=day_index)
        for _ in range(episodes_per_day):
            reload_count = rng.randrange(0, 4)
            aggressive_volume = Decimal(rng.randrange(150, 900))
            ask_pull_ratio = Decimal(rng.randrange(20, 95)) / Decimal("100")
            reclaim_ticks = Decimal(rng.randrange(0, 4))
            quality = reload_count + (1 if ask_pull_ratio >= Decimal("0.5") else 0) + int(reclaim_ticks > 0)
            edge = Decimal(quality - 2) * Decimal("0.6")
            noise = Decimal(rng.randrange(-150, 150)) / Decimal("100")
            episodes.append(
                SetupEpisode(
                    episode_date=episode_date,
                    session=rng.choice(SESSIONS),
                    regime=rng.choice((REGIME_TRENDING, REGIME_RANGING)),
                    volatility=rng.choice((VOLATILITY_HIGH, VOLATILITY_LOW)),
                    aggressive_volume=aggressive_volume,
                    reload_count=reload_count,
                    ask_pull_ratio=ask_pull_ratio,
                    reclaim_ticks=reclaim_ticks,
                    raw_r_outcome=edge + noise,
                ),
            )
    return tuple(episodes)
