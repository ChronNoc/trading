"""Executable lifecycle gates for shadow-only autonomous candidates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from app.research.autonomous_models import (
    CandidateRecord,
    CandidateState,
    GateEvidence,
    LIFECYCLE,
    TERMINAL_STATES,
)


@dataclass(frozen=True, slots=True)
class GateRequirement:
    """One preregistered metric requirement evaluated without interpretation."""

    metric: str
    minimum: float | None = None
    maximum: float | None = None

    def __post_init__(self) -> None:
        if self.minimum is None and self.maximum is None:
            raise ValueError("gate requirement needs a minimum or maximum")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("gate requirement minimum cannot exceed maximum")

    def failure(self, metrics: Mapping[str, float | int | str | bool | None]) -> str | None:
        """Return an objective failure reason, or None when satisfied."""
        value = metrics.get(self.metric)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"missing numeric metric: {self.metric}"
        numeric = float(value)
        if self.minimum is not None and numeric < self.minimum:
            return f"{self.metric}={numeric} is below minimum {self.minimum}"
        if self.maximum is not None and numeric > self.maximum:
            return f"{self.metric}={numeric} exceeds maximum {self.maximum}"
        return None


def next_gate(record: CandidateRecord) -> CandidateState | None:
    """Return the only lifecycle gate that may follow the current state."""
    if record.state in TERMINAL_STATES:
        return None
    try:
        index = LIFECYCLE.index(record.state)
    except ValueError as exc:
        raise ValueError(f"candidate has unsupported lifecycle state: {record.state}") from exc
    if index + 1 >= len(LIFECYCLE):
        return None
    return LIFECYCLE[index + 1]


def evaluate_requirements(
    *,
    gate: CandidateState,
    recorded_at_ns: int,
    metrics: Mapping[str, float | int | str | bool | None],
    requirements: tuple[GateRequirement, ...],
    evidence_refs: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
) -> GateEvidence:
    """Build immutable gate evidence from preregistered numeric thresholds."""
    failures = tuple(filter(None, (requirement.failure(metrics) for requirement in requirements)))
    return GateEvidence(
        gate=gate,
        passed=not failures,
        recorded_at_ns=recorded_at_ns,
        evidence_refs=evidence_refs,
        metrics=dict(metrics),
        warnings=warnings,
        failure_reason="; ".join(failures),
    )


def apply_gate(
    record: CandidateRecord,
    evidence: GateEvidence,
    *,
    failure_state: CandidateState = CandidateState.FAILED_REQUIRES_REWORK,
) -> CandidateRecord:
    """Apply exactly the next gate, failing closed to a governed terminal state."""
    expected = next_gate(record)
    if expected is None:
        raise ValueError("terminal candidate cannot progress")
    if evidence.gate is not expected:
        raise ValueError(f"expected gate {expected.value}, received {evidence.gate.value}")
    if evidence.passed:
        return record.with_gate(evidence, evidence.gate)
    if failure_state not in {CandidateState.REJECTED, CandidateState.FAILED_REQUIRES_REWORK}:
        raise ValueError("failed gate must become REJECTED or FAILED_REQUIRES_REWORK")
    return record.with_gate(evidence, failure_state)
