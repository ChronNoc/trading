"""Typed, content-addressed records for shadow-only autonomous research."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
import hashlib
import json
from typing import Any, Mapping


SCHEMA_VERSION = 1


class CandidateKind(StrEnum):
    """Bounded kinds of autonomous research evidence."""

    MODEL = "MODEL"
    STRATEGY = "STRATEGY"
    RISK = "RISK"


class CandidateState(StrEnum):
    """Governed lifecycle for research evidence, never runtime approval."""

    PROPOSED = "PROPOSED"
    DATA_VALIDATED = "DATA_VALIDATED"
    OFFLINE_TRAINED = "OFFLINE_TRAINED"
    WALK_FORWARD_VALIDATED = "WALK_FORWARD_VALIDATED"
    STABILITY_VALIDATED = "STABILITY_VALIDATED"
    COST_VALIDATED = "COST_VALIDATED"
    SHADOW_CANDIDATE = "SHADOW_CANDIDATE"
    SHADOW_OBSERVING = "SHADOW_OBSERVING"
    SHADOW_ELIGIBLE = "SHADOW_ELIGIBLE"
    SHADOW_APPROVED = "SHADOW_APPROVED"
    REJECTED = "REJECTED"
    FAILED_REQUIRES_REWORK = "FAILED_REQUIRES_REWORK"


LIFECYCLE: tuple[CandidateState, ...] = (
    CandidateState.PROPOSED,
    CandidateState.DATA_VALIDATED,
    CandidateState.OFFLINE_TRAINED,
    CandidateState.WALK_FORWARD_VALIDATED,
    CandidateState.STABILITY_VALIDATED,
    CandidateState.COST_VALIDATED,
    CandidateState.SHADOW_CANDIDATE,
    CandidateState.SHADOW_OBSERVING,
    CandidateState.SHADOW_ELIGIBLE,
    CandidateState.SHADOW_APPROVED,
)
TERMINAL_STATES = frozenset(
    {CandidateState.SHADOW_APPROVED, CandidateState.REJECTED, CandidateState.FAILED_REQUIRES_REWORK}
)


@dataclass(frozen=True, slots=True)
class SafetyAttestation:
    """Explicit proof that candidate production did not cross safety authority."""

    no_broker_gateway: bool = True
    no_execution_gateway: bool = True
    no_approval_write: bool = True
    no_safety_config_write: bool = True
    shadow_paper_research_only: bool = True

    @property
    def valid(self) -> bool:
        """Return whether every immutable safety statement is true."""
        return all(asdict(self).values())


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    """Per-candidate hard resource limits."""

    wall_clock_seconds: int = 3600
    disk_bytes: int = 1_073_741_824
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if self.wall_clock_seconds <= 0 or self.disk_bytes <= 0 or self.max_attempts <= 0:
            raise ValueError("resource limits must be positive")


@dataclass(frozen=True, slots=True)
class GateEvidence:
    """Immutable result of one objective lifecycle gate."""

    gate: CandidateState
    passed: bool
    recorded_at_ns: int
    evidence_refs: tuple[str, ...] = ()
    metrics: Mapping[str, float | int | str | bool | None] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    failure_reason: str = ""

    def __post_init__(self) -> None:
        if self.gate not in LIFECYCLE[1:]:
            raise ValueError("gate must be a governed post-proposal lifecycle state")
        if self.recorded_at_ns < 0:
            raise ValueError("recorded_at_ns cannot be negative")
        if self.passed and self.failure_reason:
            raise ValueError("passing gate cannot have a failure reason")
        if not self.passed and not self.failure_reason.strip():
            raise ValueError("failed gate requires a reason")


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    """Content-addressed autonomous candidate and its append-only gate history."""

    candidate_id: str
    kind: CandidateKind
    hypothesis: str
    proposer: str
    created_at_ns: int
    software_revision: str
    definition: Mapping[str, Any]
    baseline_id: str
    feature_hash: str = ""
    label_hash: str = ""
    source_session_ids: tuple[str, ...] = ()
    split_policy: str = ""
    seed: int = 0
    cost_and_fill_assumptions: Mapping[str, Any] = field(default_factory=dict)
    preregistered_gates: Mapping[str, Any] = field(default_factory=dict)
    resource_budget: ResourceBudget = field(default_factory=ResourceBudget)
    safety_attestation: SafetyAttestation = field(default_factory=SafetyAttestation)
    parent_id: str | None = None
    state: CandidateState = CandidateState.PROPOSED
    gate_history: tuple[GateEvidence, ...] = ()
    attempt_evidence_refs: tuple[str, ...] = ()
    attempt_count: int = 0
    next_retry_at_ns: int = 0
    errors: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported candidate schema {self.schema_version}")
        if not self.hypothesis.strip() or not self.proposer.strip():
            raise ValueError("candidate requires a hypothesis and proposer")
        if not self.baseline_id.strip():
            raise ValueError("candidate requires an explicit baseline")
        if self.created_at_ns < 0:
            raise ValueError("created_at_ns cannot be negative")
        if self.attempt_count < 0 or self.next_retry_at_ns < 0:
            raise ValueError("attempt metadata cannot be negative")
        if not self.safety_attestation.valid:
            raise ValueError("candidate safety attestation is incomplete")
        expected = candidate_identity(self.identity_payload())
        if self.candidate_id != expected:
            raise ValueError("candidate_id does not match immutable defining inputs")

    def identity_payload(self) -> dict[str, Any]:
        """Return only immutable defining inputs used for deduplication."""
        return {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "hypothesis": self.hypothesis,
            "proposer": self.proposer,
            "software_revision": self.software_revision,
            "definition": self.definition,
            "baseline_id": self.baseline_id,
            "feature_hash": self.feature_hash,
            "label_hash": self.label_hash,
            "source_session_ids": self.source_session_ids,
            "split_policy": self.split_policy,
            "seed": self.seed,
            "cost_and_fill_assumptions": self.cost_and_fill_assumptions,
            "preregistered_gates": self.preregistered_gates,
            "resource_budget": asdict(self.resource_budget),
            "safety_attestation": asdict(self.safety_attestation),
            "parent_id": self.parent_id,
        }

    def with_gate(self, evidence: GateEvidence, state: CandidateState) -> "CandidateRecord":
        """Return a new record after a validated state transition."""
        return replace(self, state=state, gate_history=(*self.gate_history, evidence))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""
        payload = asdict(self)
        payload["kind"] = self.kind.value
        payload["state"] = self.state.value
        for gate, evidence in zip(self.gate_history, payload["gate_history"], strict=True):
            evidence["gate"] = gate.gate.value
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CandidateRecord":
        """Decode a persisted record and revalidate its identity and safety."""
        data = dict(payload)
        data["kind"] = CandidateKind(str(data["kind"]))
        data["state"] = CandidateState(str(data.get("state", CandidateState.PROPOSED)))
        data["source_session_ids"] = tuple(data.get("source_session_ids", ()))
        data["attempt_evidence_refs"] = tuple(data.get("attempt_evidence_refs", ()))
        data["attempt_count"] = int(data.get("attempt_count", 0))
        data["next_retry_at_ns"] = int(data.get("next_retry_at_ns", 0))
        data["errors"] = tuple(data.get("errors", ()))
        data["resource_budget"] = ResourceBudget(**dict(data.get("resource_budget", {})))
        data["safety_attestation"] = SafetyAttestation(**dict(data.get("safety_attestation", {})))
        data["gate_history"] = tuple(
            GateEvidence(
                gate=CandidateState(str(item["gate"])),
                passed=bool(item["passed"]),
                recorded_at_ns=int(item["recorded_at_ns"]),
                evidence_refs=tuple(item.get("evidence_refs", ())),
                metrics=dict(item.get("metrics", {})),
                warnings=tuple(item.get("warnings", ())),
                failure_reason=str(item.get("failure_reason", "")),
            )
            for item in data.get("gate_history", ())
        )
        return cls(**data)


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Serialize an identity or evidence payload deterministically."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def candidate_identity(payload: Mapping[str, Any]) -> str:
    """Return the full SHA-256 identity for immutable candidate inputs."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def propose_candidate(
    *,
    kind: CandidateKind,
    hypothesis: str,
    proposer: str,
    created_at_ns: int,
    software_revision: str,
    definition: Mapping[str, Any],
    baseline_id: str,
    **kwargs: Any,
) -> CandidateRecord:
    """Construct a deduplicated proposal from its immutable definition."""
    resource_budget = kwargs.get("resource_budget", ResourceBudget())
    safety_attestation = kwargs.get("safety_attestation", SafetyAttestation())
    identity = candidate_identity(
        {
            "schema_version": SCHEMA_VERSION,
            "kind": kind.value,
            "hypothesis": hypothesis,
            "proposer": proposer,
            "software_revision": software_revision,
            "definition": dict(definition),
            "baseline_id": baseline_id,
            "feature_hash": kwargs.get("feature_hash", ""),
            "label_hash": kwargs.get("label_hash", ""),
            "source_session_ids": tuple(kwargs.get("source_session_ids", ())),
            "split_policy": kwargs.get("split_policy", ""),
            "seed": kwargs.get("seed", 0),
            "cost_and_fill_assumptions": kwargs.get("cost_and_fill_assumptions", {}),
            "preregistered_gates": kwargs.get("preregistered_gates", {}),
            "resource_budget": asdict(resource_budget),
            "safety_attestation": asdict(safety_attestation),
            "parent_id": kwargs.get("parent_id"),
        }
    )
    return CandidateRecord(
        candidate_id=identity,
        kind=kind,
        hypothesis=hypothesis,
        proposer=proposer,
        created_at_ns=created_at_ns,
        software_revision=software_revision,
        definition=dict(definition),
        baseline_id=baseline_id,
        **kwargs,
    )
