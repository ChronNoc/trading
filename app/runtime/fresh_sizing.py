"""Fresh-at-decision-time sizing - item 36.

A thin wrapper around the protected ``risk/sizing.py`` (unmodified) that
recomputes position size from current inputs at every call. There is no
cache anywhere in this module, and the result carries the timestamp it
was computed at so downstream code can prove freshness.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.risk.sizing import SizingInputs, SizingResult, calculate_position_size


@dataclass(frozen=True, slots=True)
class FreshSizingResult:
    """A sizing result stamped with its computation time."""

    result: SizingResult
    computed_at: datetime


def fresh_position_size(inputs: SizingInputs, *, now: datetime | None = None) -> FreshSizingResult:
    """Recompute position size from the given inputs right now.

    Callers must construct ``inputs`` from current account state at the
    decision moment - never reuse a previous FreshSizingResult.
    """
    return FreshSizingResult(
        result=calculate_position_size(inputs),
        computed_at=now or datetime.now(timezone.utc),
    )
