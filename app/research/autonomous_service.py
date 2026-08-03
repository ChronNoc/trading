"""Bounded orchestration for shadow-only autonomous intelligence work."""

from __future__ import annotations

from dataclasses import dataclass, replace
from queue import Empty, Queue
from threading import Thread
import time
from typing import Callable, Mapping, Protocol

from app.research.autonomous_gates import GateRequirement, apply_gate, evaluate_requirements, next_gate
from app.research.autonomous_models import CandidateRecord, CandidateState, TERMINAL_STATES
from app.research.autonomous_store import AutonomousStore, Lease


class CandidateBudgetExceeded(RuntimeError):
    """Raised when one candidate exceeds an immutable resource budget."""


class ReceiverHealthProvider(Protocol):
    """Minimal read-only capture health seam used for priority throttling."""

    def __call__(self) -> Mapping[str, object]: ...


class GateRunner(Protocol):
    """Bounded gate runner that returns metrics and immutable evidence references."""

    def __call__(self, candidate: CandidateRecord, deadline_ns: int) -> "GateResult": ...


@dataclass(frozen=True, slots=True)
class GateResult:
    """Objective output from one bounded gate runner."""

    metrics: Mapping[str, float | int | str | bool | None]
    evidence_refs: tuple[str, ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ServicePolicy:
    """Hard service quotas that candidate definitions cannot override."""

    max_candidates_per_run: int = 1
    max_candidates_per_day: int = 24
    max_store_bytes: int = 5_368_709_120
    retry_backoff_seconds: float = 30.0
    retry_backoff_factor: float = 2.0
    retry_backoff_max_seconds: float = 900.0
    heartbeat_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_candidates_per_run <= 0 or self.max_candidates_per_day <= 0:
            raise ValueError("candidate quotas must be positive")
        if self.max_store_bytes <= 0 or self.retry_backoff_seconds < 0:
            raise ValueError("store quota must be positive and backoff cannot be negative")
        if self.retry_backoff_factor < 1 or self.retry_backoff_max_seconds < 0:
            raise ValueError("backoff factor must be at least one and cap cannot be negative")
        if self.heartbeat_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")


@dataclass(frozen=True, slots=True)
class ServiceRun:
    """Truthful summary of one service pass."""

    advanced: int = 0
    failed: int = 0
    skipped: int = 0
    paused_reason: str = ""


class AutonomousIntelligenceService:
    """Progress candidates through objective gates without runtime authority."""

    def __init__(
        self,
        *,
        store: AutonomousStore,
        gate_runners: Mapping[CandidateState, GateRunner],
        requirements: Mapping[CandidateState, tuple[GateRequirement, ...]],
        receiver_health: ReceiverHealthProvider,
        policy: ServicePolicy = ServicePolicy(),
        owner: str = "autonomous-intelligence-service",
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if not owner.strip():
            raise ValueError("service owner is required")
        self.store = store
        self.gate_runners = dict(gate_runners)
        self.requirements = dict(requirements)
        self.receiver_health = receiver_health
        self.policy = policy
        self.owner = owner
        self.clock_ns = clock_ns

    def propose(self, candidate: CandidateRecord) -> CandidateRecord:
        """Persist a deduplicated proposal without triggering evaluation."""
        existing = self.store.load(candidate.candidate_id)
        if existing is not None:
            return existing
        self._enforce_store_quota()
        self.store.publish(candidate)
        self.store.append_activity(
            {
                "event": "candidate_proposed",
                "candidate_id": candidate.candidate_id,
                "state": candidate.state.value,
                "recorded_at_ns": self.clock_ns(),
            }
        )
        return candidate

    def run_once(self) -> ServiceRun:
        """Advance a bounded number of eligible candidates after cheap safety gates."""
        pause = self._capture_pause_reason()
        if pause:
            return ServiceRun(paused_reason=pause)
        self._enforce_store_quota()
        now_ns = self.clock_ns()
        candidates = tuple(
            record
            for record in self.store.list_candidates()
            if record.state not in TERMINAL_STATES and next_gate(record) is not None
        )
        eligible = tuple(record for record in candidates if record.next_retry_at_ns <= now_ns)
        deferred = len(candidates) - len(eligible)
        remaining_today = self.policy.max_candidates_per_day - self.store.attempt_count_for_utc_day(now_ns)
        if remaining_today <= 0:
            return ServiceRun(skipped=len(candidates), paused_reason="daily candidate quota reached")
        limit = min(self.policy.max_candidates_per_run, remaining_today)
        advanced = failed = skipped = 0
        for record in eligible[:limit]:
            outcome = self._advance(record)
            advanced += outcome.advanced
            failed += outcome.failed
            skipped += outcome.skipped
        skipped += deferred + max(0, len(eligible) - limit)
        return ServiceRun(advanced=advanced, failed=failed, skipped=skipped)

    def _advance(self, record: CandidateRecord) -> ServiceRun:
        lease = self.store.try_acquire(record.candidate_id, self.owner, now_ns=self.clock_ns())
        if lease is None:
            return ServiceRun(skipped=1)
        try:
            gate = next_gate(record)
            if gate is None:
                return ServiceRun(skipped=1)
            runner = self.gate_runners.get(gate)
            requirements = self.requirements.get(gate)
            if runner is None or requirements is None:
                failed_record = self._record_terminal_error(
                    record,
                    f"no bounded runner or requirements for {gate.value}",
                )
                self._publish_transition(failed_record, lease, "gate_configuration_missing")
                return ServiceRun(failed=1)
            started_at_ns = self.clock_ns()
            deadline_ns = started_at_ns + record.resource_budget.wall_clock_seconds * 1_000_000_000
            self.store.append_activity(
                {
                    "event": "gate_attempt_started",
                    "candidate_id": record.candidate_id,
                    "gate": gate.value,
                    "recorded_at_ns": started_at_ns,
                }
            )
            usage_before = self.store.disk_usage_bytes()
            try:
                result, lease = self._run_with_heartbeats(runner, record, deadline_ns, lease)
                finished_at_ns = self.clock_ns()
                if finished_at_ns > deadline_ns:
                    raise TimeoutError(f"{gate.value} exceeded its wall-clock deadline")
                disk_growth = max(0, self.store.disk_usage_bytes() - usage_before)
                if disk_growth > record.resource_budget.disk_bytes:
                    raise CandidateBudgetExceeded(
                        f"{gate.value} wrote {disk_growth} bytes, exceeding candidate budget "
                        f"{record.resource_budget.disk_bytes}"
                    )
                evidence = evaluate_requirements(
                    gate=gate,
                    recorded_at_ns=finished_at_ns,
                    metrics=result.metrics,
                    requirements=requirements,
                    evidence_refs=result.evidence_refs,
                    warnings=result.warnings,
                )
                updated = apply_gate(record, evidence)
                if evidence.passed:
                    updated = replace(updated, attempt_count=0, next_retry_at_ns=0)
                event = "gate_passed" if evidence.passed else "gate_failed"
            except CandidateBudgetExceeded as exc:
                updated = self._record_terminal_error(record, f"{type(exc).__name__}: {exc}")
                event = "candidate_budget_exceeded"
            except Exception as exc:  # transient worker failures retry within the candidate budget
                updated, event = self._record_retry(record, f"{type(exc).__name__}: {exc}")
            self._publish_transition(updated, lease, event)
            advanced = int(updated.state is gate)
            failed = int(updated.state in {CandidateState.REJECTED, CandidateState.FAILED_REQUIRES_REWORK})
            return ServiceRun(advanced=advanced, failed=failed, skipped=int(not advanced and not failed))
        finally:
            try:
                self.store.release(lease)
            except PermissionError:
                pass

    def _run_with_heartbeats(
        self,
        runner: GateRunner,
        record: CandidateRecord,
        deadline_ns: int,
        lease: Lease,
    ) -> tuple[GateResult, Lease]:
        """Run bounded work off-thread while the owning service refreshes its lease."""
        outcomes: Queue[tuple[bool, object]] = Queue(maxsize=1)

        def invoke() -> None:
            try:
                outcomes.put((True, runner(record, deadline_ns)))
            except BaseException as exc:
                outcomes.put((False, exc))

        worker = Thread(target=invoke, name=f"autonomous-gate-{record.candidate_id[:12]}", daemon=True)
        worker.start()
        heartbeat_seconds = min(
            self.policy.heartbeat_seconds,
            max(0.001, self.store.lease_ns / 2_000_000_000),
        )
        while worker.is_alive():
            worker.join(heartbeat_seconds)
            if worker.is_alive():
                lease = self.store.heartbeat(lease, now_ns=self.clock_ns())
        if self.clock_ns() > deadline_ns:
            raise TimeoutError("gate runner exceeded its wall-clock deadline")
        try:
            succeeded, value = outcomes.get_nowait()
        except Empty as exc:
            raise RuntimeError("gate runner exited without an outcome") from exc
        if not succeeded:
            assert isinstance(value, BaseException)
            raise value
        if not isinstance(value, GateResult):
            raise TypeError("gate runner must return GateResult")
        return value, lease

    def _publish_transition(self, record: CandidateRecord, lease: Lease, event: str) -> None:
        self.store.publish(record, lease=lease)
        self.store.append_activity(
            {
                "event": event,
                "candidate_id": record.candidate_id,
                "state": record.state.value,
                "recorded_at_ns": self.clock_ns(),
            }
        )
        if record.state in TERMINAL_STATES:
            self.store.mark_complete(record.candidate_id)

    @staticmethod
    def _record_terminal_error(record: CandidateRecord, error: str) -> CandidateRecord:
        return replace(
            record,
            state=CandidateState.FAILED_REQUIRES_REWORK,
            errors=(*record.errors, error),
        )

    def _record_retry(self, record: CandidateRecord, error: str) -> tuple[CandidateRecord, str]:
        attempt_count = record.attempt_count + 1
        if attempt_count >= record.resource_budget.max_attempts:
            return (
                replace(
                    record,
                    state=CandidateState.FAILED_REQUIRES_REWORK,
                    attempt_count=attempt_count,
                    next_retry_at_ns=0,
                    errors=(*record.errors, error),
                ),
                "gate_retry_exhausted",
            )
        delay_seconds = min(
            self.policy.retry_backoff_seconds
            * (self.policy.retry_backoff_factor ** (attempt_count - 1)),
            self.policy.retry_backoff_max_seconds,
        )
        return (
            replace(
                record,
                attempt_count=attempt_count,
                next_retry_at_ns=self.clock_ns() + int(delay_seconds * 1_000_000_000),
                errors=(*record.errors, error),
            ),
            "gate_retry_scheduled",
        )

    def _capture_pause_reason(self) -> str:
        try:
            health = self.receiver_health()
        except Exception as exc:
            return f"receiver health unavailable: {type(exc).__name__}"
        if not bool(health.get("capture_priority_clear", False)):
            return str(health.get("reason", "capture priority is active"))
        return ""

    def _enforce_store_quota(self) -> None:
        if self.store.disk_usage_bytes() >= self.policy.max_store_bytes:
            raise RuntimeError("autonomous store disk quota reached")
