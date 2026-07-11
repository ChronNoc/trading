"""Kill-switch decision logic for risk-control breaches."""

from __future__ import annotations

from dataclasses import dataclass

from app.risk.limits import EntryLimitState, evaluate_entry_limits


@dataclass(frozen=True, slots=True)
class KillSwitchState:
    """State required to decide whether the system should flatten now."""

    entry_limits: EntryLimitState
    manual_override_flatten: bool = False


@dataclass(frozen=True, slots=True)
class KillSwitchDecision:
    """Decision returned by the kill switch."""

    flatten_now: bool
    reason: str


def evaluate_kill_switch(state: KillSwitchState) -> KillSwitchDecision:
    """Return whether to flatten now and the human-readable reason."""
    if state.manual_override_flatten:
        return KillSwitchDecision(
            flatten_now=True,
            reason="Manual override requested flatten now.",
        )

    limit_results = evaluate_entry_limits(state.entry_limits)
    rejection_reasons = tuple(result.reason for result in limit_results if not result.allowed)
    if rejection_reasons:
        return KillSwitchDecision(
            flatten_now=True,
            reason="Risk limit breached: " + "; ".join(rejection_reasons),
        )

    return KillSwitchDecision(
        flatten_now=False,
        reason="No kill-switch condition is active.",
    )
