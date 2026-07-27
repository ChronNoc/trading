"""Shared triple-barrier resolution rule.

One rule, used everywhere a forward outcome is decided from a causal entry
point: target-before-stop wins (1), stop-first or timeout loses (0), and an
outcome that has not yet resolved by the time evidence runs out is left
``None`` — never guessed. Both offline dataset labeling
(``app.machine_learning.session_training``) and live-scored prediction
resolution (``app.machine_learning.outcome_journal``) call the exact same
functions here so the two can never silently drift apart.
"""

from __future__ import annotations

from decimal import Decimal


def barrier_prices(
    entry: Decimal,
    direction: str,
    *,
    target_ticks: Decimal,
    stop_ticks: Decimal,
    tick_size: Decimal,
) -> tuple[Decimal, Decimal]:
    """Return (target_price, stop_price) for one directional entry."""
    if direction not in {"long", "short"}:
        raise ValueError("direction must be 'long' or 'short'")
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    up = target_ticks * tick_size
    down = stop_ticks * tick_size
    if direction == "long":
        return entry + up, entry - down
    return entry - up, entry + down


def resolve_barrier(
    direction: str,
    *,
    target: Decimal,
    stop: Decimal,
    price: Decimal,
    timestamp_ns: int,
    deadline_ns: int,
) -> int | None:
    """1 if target-before-stop, 0 if stop-first or horizon expired, None if still open."""
    result = resolve_barrier_detail(
        direction, target=target, stop=stop, price=price,
        timestamp_ns=timestamp_ns, deadline_ns=deadline_ns,
    )
    return None if result is None else result[0]


def resolve_barrier_detail(
    direction: str,
    *,
    target: Decimal,
    stop: Decimal,
    price: Decimal,
    timestamp_ns: int,
    deadline_ns: int,
) -> tuple[int, str] | None:
    """Like :func:`resolve_barrier`, but also names which barrier resolved it.

    Returns ``(label, reason)`` where ``reason`` is one of ``"target"``,
    ``"stop"``, or ``"timeout"``; ``None`` while the outcome is still open.
    """
    if direction not in {"long", "short"}:
        raise ValueError("direction must be 'long' or 'short'")
    # An observation strictly after the configured horizon cannot resolve a
    # price barrier.  Check expiry before inspecting that future price, while
    # retaining inclusive deadline semantics for an event exactly at T+horizon.
    if timestamp_ns > deadline_ns:
        return 0, "timeout"
    if direction == "long":
        if price >= target:
            return 1, "target"
        if price <= stop:
            return 0, "stop"
    else:
        if price <= target:
            return 1, "target"
        if price >= stop:
            return 0, "stop"
    if timestamp_ns == deadline_ns:
        return 0, "timeout"
    return None
