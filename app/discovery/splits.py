"""Walk-forward and holdout data splitting - items 11 and 12.

The holdout vault enforces the single most important discipline in the
pipeline: the holdout set is read exactly once, at the very end. A second
access raises.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.discovery.episodes import SetupEpisode


class HoldoutAlreadyConsumedError(RuntimeError):
    """Raised when code tries to read the holdout set a second time."""


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    """One rolling train -> validate window."""

    train: tuple[SetupEpisode, ...]
    validate: tuple[SetupEpisode, ...]


class HoldoutVault:
    """Holds the holdout episodes and permits exactly one read."""

    def __init__(self, episodes: tuple[SetupEpisode, ...]) -> None:
        """Seal the holdout episodes."""
        self._episodes = episodes
        self._consumed = False

    @property
    def consumed(self) -> bool:
        """Whether the single allowed read already happened."""
        return self._consumed

    @property
    def size(self) -> int:
        """Number of held-out episodes (safe to inspect without consuming)."""
        return len(self._episodes)

    def consume(self) -> tuple[SetupEpisode, ...]:
        """Return the holdout episodes once; any further call raises."""
        if self._consumed:
            raise HoldoutAlreadyConsumedError(
                "holdout set was already evaluated once; it must never be reused",
            )
        self._consumed = True
        return self._episodes


def split_holdout(
    episodes: Sequence[SetupEpisode],
    *,
    holdout_fraction: float = 0.2,
) -> tuple[tuple[SetupEpisode, ...], HoldoutVault]:
    """Split episodes by date: the most recent fraction becomes the holdout."""
    if not 0 < holdout_fraction < 1:
        raise ValueError("holdout_fraction must be between 0 and 1")
    ordered = sorted(episodes, key=lambda episode: episode.episode_date)
    cut = int(len(ordered) * (1 - holdout_fraction))
    if cut == 0 or cut == len(ordered):
        raise ValueError("holdout split produced an empty side; need more episodes")
    return tuple(ordered[:cut]), HoldoutVault(tuple(ordered[cut:]))


def walk_forward_windows(
    episodes: Sequence[SetupEpisode],
    *,
    train_days: int,
    validate_days: int,
) -> tuple[WalkForwardWindow, ...]:
    """Build rolling train -> validate windows ordered by date."""
    if train_days <= 0 or validate_days <= 0:
        raise ValueError("train_days and validate_days must be positive")
    ordered = sorted(episodes, key=lambda episode: episode.episode_date)
    dates = sorted({episode.episode_date for episode in ordered})
    windows: list[WalkForwardWindow] = []
    start = 0
    while start + train_days + validate_days <= len(dates):
        train_dates = set(dates[start : start + train_days])
        validate_dates = set(dates[start + train_days : start + train_days + validate_days])
        windows.append(
            WalkForwardWindow(
                train=tuple(episode for episode in ordered if episode.episode_date in train_dates),
                validate=tuple(episode for episode in ordered if episode.episode_date in validate_dates),
            ),
        )
        start += validate_days
    if not windows:
        raise ValueError("not enough distinct dates for a single walk-forward window")
    return tuple(windows)
