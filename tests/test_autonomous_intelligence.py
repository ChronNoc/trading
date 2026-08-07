"""Safety and restart contracts for autonomous intelligence persistence."""

from __future__ import annotations

import json

import pytest

from app.research.autonomous_gates import GateRequirement, apply_gate, evaluate_requirements, next_gate
from app.research.autonomous_models import (
    CandidateKind,
    CandidateRecord,
    CandidateState,
    GateEvidence,
    LIFECYCLE,
    ResourceBudget,
    SafetyAttestation,
    propose_candidate,
)
from app.research.autonomous_service import (
    AutonomousIntelligenceService,
    GateResult,
    ServicePolicy,
)
from app.research.autonomous_store import AutonomousStore


def _candidate(*, created_at_ns: int = 100, definition: dict[str, object] | None = None) -> CandidateRecord:
    return propose_candidate(
        kind=CandidateKind.MODEL,
        hypothesis="A bounded feature-window change improves the preregistered baseline.",
        proposer="test-suite",
        created_at_ns=created_at_ns,
        software_revision="abc123",
        definition=definition or {"feature_window": 20},
        baseline_id="constant-prior-v1",
        feature_hash="feature-v1",
        label_hash="label-v1",
        source_session_ids=("session-a", "session-b"),
        split_policy="chronological with untouched final period",
        seed=7,
        preregistered_gates={"minimum_days": 10},
    )


def test_required_candidate_lifecycle_is_exact() -> None:
    assert tuple(state.value for state in LIFECYCLE) == (
        "PROPOSED",
        "DATA_VALIDATED",
        "OFFLINE_TRAINED",
        "WALK_FORWARD_VALIDATED",
        "STABILITY_VALIDATED",
        "COST_VALIDATED",
        "SHADOW_CANDIDATE",
        "SHADOW_OBSERVING",
        "SHADOW_ELIGIBLE",
        "SHADOW_APPROVED",
    )
    assert CandidateState.REJECTED not in LIFECYCLE
    assert CandidateState.FAILED_REQUIRES_REWORK not in LIFECYCLE


def test_candidate_identity_deduplicates_creation_time() -> None:
    first = _candidate(created_at_ns=100)
    later = _candidate(created_at_ns=999)

    assert first.candidate_id == later.candidate_id
    assert first.created_at_ns != later.created_at_ns


def test_candidate_identity_changes_with_definition() -> None:
    assert _candidate().candidate_id != _candidate(definition={"feature_window": 40}).candidate_id


def test_candidate_round_trip_revalidates_identity() -> None:
    candidate = _candidate().with_gate(
        GateEvidence(
            gate=CandidateState.DATA_VALIDATED,
            passed=True,
            recorded_at_ns=200,
            evidence_refs=("evidence/data-validation.json",),
            metrics={"eligible_sessions": 12},
        ),
        CandidateState.DATA_VALIDATED,
    )

    restored = CandidateRecord.from_dict(json.loads(json.dumps(candidate.to_dict())))

    assert restored == candidate


def test_incomplete_safety_attestation_is_rejected() -> None:
    with pytest.raises(ValueError, match="safety attestation"):
        _candidate_with(safety_attestation=SafetyAttestation(no_broker_gateway=False))


@pytest.mark.parametrize(
    "budget",
    (
        ResourceBudget(wall_clock_seconds=1, disk_bytes=1, max_attempts=1),
    ),
)
def test_positive_resource_budget_is_accepted(budget: ResourceBudget) -> None:
    assert _candidate_with(resource_budget=budget).resource_budget == budget


@pytest.mark.parametrize(
    "values",
    (
        {"wall_clock_seconds": 0},
        {"disk_bytes": 0},
        {"max_attempts": 0},
    ),
)
def test_non_positive_resource_limits_are_rejected(values: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="resource limits"):
        ResourceBudget(**values)


def test_attempt_metadata_must_be_non_negative() -> None:
    candidate = _candidate()

    with pytest.raises(ValueError, match="attempt metadata"):
        CandidateRecord.from_dict({**candidate.to_dict(), "attempt_count": -1})
    with pytest.raises(ValueError, match="attempt metadata"):
        CandidateRecord.from_dict({**candidate.to_dict(), "next_retry_at_ns": -1})


def test_gate_evidence_requires_objective_post_proposal_gate_and_failure_reason() -> None:
    with pytest.raises(ValueError, match="post-proposal"):
        GateEvidence(CandidateState.PROPOSED, True, 1)
    with pytest.raises(ValueError, match="requires a reason"):
        GateEvidence(CandidateState.DATA_VALIDATED, False, 1)
    with pytest.raises(ValueError, match="passing gate"):
        GateEvidence(CandidateState.DATA_VALIDATED, True, 1, failure_reason="contradiction")


def test_store_publishes_evidence_before_checkpoint(tmp_path) -> None:
    store = AutonomousStore(tmp_path)
    candidate = _candidate()

    with pytest.raises(FileNotFoundError, match="evidence must exist"):
        store.mark_complete(candidate.candidate_id)

    store.publish(candidate)
    store.mark_complete(candidate.candidate_id)

    assert store.load(candidate.candidate_id) == candidate
    assert store.completed_ids() == {candidate.candidate_id}


def test_store_deduplicates_identical_publication(tmp_path) -> None:
    store = AutonomousStore(tmp_path)
    candidate = _candidate()

    path = store.publish(candidate)
    first_bytes = path.read_bytes()
    store.publish(candidate)

    assert path.read_bytes() == first_bytes
    assert len(tuple((tmp_path / "candidates").glob("*.json"))) == 1


def test_stale_lease_recovery_fences_previous_worker(tmp_path) -> None:
    store = AutonomousStore(tmp_path, lease_seconds=1)
    candidate = _candidate()
    first = store.try_acquire(candidate.candidate_id, "worker-a", now_ns=1)
    assert first is not None
    assert store.try_acquire(candidate.candidate_id, "worker-b", now_ns=500_000_000) is None

    recovered = store.try_acquire(candidate.candidate_id, "worker-b", now_ns=2_000_000_000)

    assert recovered is not None
    assert recovered.token != first.token
    with pytest.raises(PermissionError, match="fenced"):
        store.publish(candidate, lease=first)
    assert store.publish(candidate, lease=recovered).is_file()


def test_heartbeat_retains_fencing_token(tmp_path) -> None:
    store = AutonomousStore(tmp_path)
    candidate = _candidate()
    lease = store.try_acquire(candidate.candidate_id, "worker-a", now_ns=1)
    assert lease is not None

    refreshed = store.heartbeat(lease, now_ns=2)

    assert refreshed.token == lease.token
    assert refreshed.heartbeat_at_ns == 2
    store.assert_lease(refreshed)


def test_activity_is_append_only(tmp_path) -> None:
    store = AutonomousStore(tmp_path)

    store.append_activity({"sequence": 1, "event": "proposed"})
    store.append_activity({"sequence": 2, "event": "validated"})

    events = [json.loads(line) for line in store.activity_path.read_text(encoding="utf-8").splitlines()]
    assert events == [
        {"event": "proposed", "sequence": 1},
        {"event": "validated", "sequence": 2},
    ]


def test_gate_requirements_fail_closed_on_missing_or_out_of_range_metrics() -> None:
    evidence = evaluate_requirements(
        gate=CandidateState.DATA_VALIDATED,
        recorded_at_ns=10,
        metrics={"eligible_sessions": 4},
        requirements=(
            GateRequirement("eligible_sessions", minimum=10),
            GateRequirement("source_delay_seconds", maximum=2),
        ),
    )

    assert evidence.passed is False
    assert "below minimum" in evidence.failure_reason
    assert "missing numeric metric" in evidence.failure_reason


def test_gate_progression_accepts_only_the_exact_next_state() -> None:
    candidate = _candidate()
    evidence = evaluate_requirements(
        gate=CandidateState.DATA_VALIDATED,
        recorded_at_ns=10,
        metrics={"eligible_sessions": 12},
        requirements=(GateRequirement("eligible_sessions", minimum=10),),
        evidence_refs=("evidence/data.json",),
    )

    advanced = apply_gate(candidate, evidence)

    assert advanced.state is CandidateState.DATA_VALIDATED
    assert next_gate(advanced) is CandidateState.OFFLINE_TRAINED
    with pytest.raises(ValueError, match="expected gate"):
        apply_gate(advanced, evidence)


def test_failed_gate_uses_only_governed_failure_states() -> None:
    candidate = _candidate()
    failed = evaluate_requirements(
        gate=CandidateState.DATA_VALIDATED,
        recorded_at_ns=10,
        metrics={"eligible_sessions": 4},
        requirements=(GateRequirement("eligible_sessions", minimum=10),),
    )

    rework = apply_gate(candidate, failed)
    rejected = apply_gate(candidate, failed, failure_state=CandidateState.REJECTED)

    assert rework.state is CandidateState.FAILED_REQUIRES_REWORK
    assert rejected.state is CandidateState.REJECTED
    assert next_gate(rework) is None
    with pytest.raises(ValueError, match="REJECTED or FAILED_REQUIRES_REWORK"):
        apply_gate(candidate, failed, failure_state=CandidateState.SHADOW_CANDIDATE)


def test_service_pauses_for_capture_priority_without_running_gate(tmp_path) -> None:
    store = AutonomousStore(tmp_path)
    calls: list[str] = []

    def runner(candidate: CandidateRecord, deadline_ns: int) -> GateResult:
        calls.append(candidate.candidate_id)
        return GateResult({"eligible_sessions": 12}, ("evidence/data.json",))

    service = AutonomousIntelligenceService(
        store=store,
        gate_runners={CandidateState.DATA_VALIDATED: runner},
        requirements={CandidateState.DATA_VALIDATED: (GateRequirement("eligible_sessions", minimum=10),)},
        receiver_health=lambda: {"capture_priority_clear": False, "reason": "receiver backlog"},
    )
    service.propose(_candidate())

    result = service.run_once()

    assert result.paused_reason == "receiver backlog"
    assert calls == []


def test_service_persists_gate_before_terminal_checkpoint(tmp_path) -> None:
    store = AutonomousStore(tmp_path)
    candidate = _candidate()
    service = AutonomousIntelligenceService(
        store=store,
        gate_runners={
            CandidateState.DATA_VALIDATED: lambda candidate, deadline: GateResult(
                {"eligible_sessions": 4}, ("evidence/rejected-data.json",)
            )
        },
        requirements={CandidateState.DATA_VALIDATED: (GateRequirement("eligible_sessions", minimum=10),)},
        receiver_health=lambda: {"capture_priority_clear": True},
    )
    service.propose(candidate)

    result = service.run_once()
    persisted = store.load(candidate.candidate_id)

    assert result.failed == 1
    assert persisted is not None
    assert persisted.state is CandidateState.FAILED_REQUIRES_REWORK
    assert persisted.gate_history[-1].failure_reason
    assert store.completed_ids() == {candidate.candidate_id}


def test_service_advances_only_one_exact_gate_per_pass(tmp_path) -> None:
    store = AutonomousStore(tmp_path)
    candidate = _candidate()
    service = AutonomousIntelligenceService(
        store=store,
        gate_runners={
            CandidateState.DATA_VALIDATED: lambda candidate, deadline: GateResult(
                {"eligible_sessions": 12}, ("evidence/data.json",)
            )
        },
        requirements={CandidateState.DATA_VALIDATED: (GateRequirement("eligible_sessions", minimum=10),)},
        receiver_health=lambda: {"capture_priority_clear": True},
    )
    assert service.propose(candidate) == service.propose(candidate)

    result = service.run_once()
    persisted = store.load(candidate.candidate_id)

    assert result.advanced == 1
    assert persisted is not None
    assert persisted.state is CandidateState.DATA_VALIDATED
    assert persisted.gate_history[-1].gate is CandidateState.DATA_VALIDATED
    assert store.completed_ids() == set()


def test_service_fails_closed_when_gate_runner_is_missing(tmp_path) -> None:
    store = AutonomousStore(tmp_path)
    candidate = _candidate()
    service = AutonomousIntelligenceService(
        store=store,
        gate_runners={},
        requirements={},
        receiver_health=lambda: {"capture_priority_clear": True},
    )
    service.propose(candidate)

    result = service.run_once()
    persisted = store.load(candidate.candidate_id)

    assert result.failed == 1
    assert persisted is not None
    assert persisted.state is CandidateState.FAILED_REQUIRES_REWORK
    assert "no bounded runner" in persisted.errors[-1]


def test_service_retries_transient_runner_failure_after_restart(tmp_path) -> None:
    store = AutonomousStore(tmp_path)
    candidate = _candidate()
    now = [1_000_000_000]
    calls: list[int] = []

    def runner(candidate: CandidateRecord, deadline_ns: int) -> GateResult:
        calls.append(deadline_ns)
        if len(calls) == 1:
            raise OSError("temporary input failure")
        return GateResult({"eligible_sessions": 12}, ("evidence/data.json",))

    def service() -> AutonomousIntelligenceService:
        return AutonomousIntelligenceService(
            store=store,
            gate_runners={CandidateState.DATA_VALIDATED: runner},
            requirements={CandidateState.DATA_VALIDATED: (GateRequirement("eligible_sessions", minimum=10),)},
            receiver_health=lambda: {"capture_priority_clear": True},
            policy=ServicePolicy(retry_backoff_seconds=2),
            clock_ns=lambda: now[0],
        )

    service().propose(candidate)
    first = service().run_once()
    persisted = store.load(candidate.candidate_id)

    assert first.skipped == 1
    assert persisted is not None
    assert persisted.state is CandidateState.PROPOSED
    assert persisted.attempt_count == 1
    assert persisted.next_retry_at_ns == 3_000_000_000
    assert service().run_once().skipped == 1
    assert len(calls) == 1

    now[0] = persisted.next_retry_at_ns
    second = service().run_once()

    assert second.advanced == 1
    assert len(calls) == 2
    assert store.load(candidate.candidate_id).state is CandidateState.DATA_VALIDATED


def test_successful_retry_resets_attempt_metadata(tmp_path) -> None:
    store = AutonomousStore(tmp_path)
    candidate = _candidate()
    now = [1]
    calls = [0]

    def runner(candidate: CandidateRecord, deadline_ns: int) -> GateResult:
        calls[0] += 1
        if calls[0] == 1:
            raise OSError("temporary")
        return GateResult({"eligible_sessions": 12}, ("evidence/data.json",))

    service = AutonomousIntelligenceService(
        store=store,
        gate_runners={CandidateState.DATA_VALIDATED: runner},
        requirements={CandidateState.DATA_VALIDATED: (GateRequirement("eligible_sessions", minimum=10),)},
        receiver_health=lambda: {"capture_priority_clear": True},
        policy=ServicePolicy(retry_backoff_seconds=0),
        clock_ns=lambda: now[0],
    )
    service.propose(candidate)

    service.run_once()
    service.run_once()
    persisted = store.load(candidate.candidate_id)

    assert persisted is not None
    assert persisted.state is CandidateState.DATA_VALIDATED
    assert persisted.attempt_count == 0
    assert persisted.next_retry_at_ns == 0


def test_long_running_gate_refreshes_lease_heartbeat(tmp_path, monkeypatch) -> None:
    store = AutonomousStore(tmp_path, lease_seconds=0.02)
    candidate = _candidate()
    heartbeats: list[int] = []
    original_heartbeat = store.heartbeat

    def heartbeat(lease, *, now_ns=None):
        heartbeats.append(int(now_ns))
        return original_heartbeat(lease, now_ns=now_ns)

    monkeypatch.setattr(store, "heartbeat", heartbeat)

    def runner(candidate: CandidateRecord, deadline_ns: int) -> GateResult:
        import time

        time.sleep(0.04)
        return GateResult({"eligible_sessions": 12}, ("evidence/data.json",))

    service = AutonomousIntelligenceService(
        store=store,
        gate_runners={CandidateState.DATA_VALIDATED: runner},
        requirements={CandidateState.DATA_VALIDATED: (GateRequirement("eligible_sessions", minimum=10),)},
        receiver_health=lambda: {"capture_priority_clear": True},
        policy=ServicePolicy(heartbeat_seconds=0.005),
    )
    service.propose(candidate)

    result = service.run_once()

    assert result.advanced == 1
    assert heartbeats


def test_service_exhausts_bounded_retry_budget(tmp_path) -> None:
    store = AutonomousStore(tmp_path)
    candidate = _candidate_with(
        resource_budget=ResourceBudget(wall_clock_seconds=1, disk_bytes=1_000_000, max_attempts=2)
    )
    now = [1]
    service = AutonomousIntelligenceService(
        store=store,
        gate_runners={CandidateState.DATA_VALIDATED: lambda candidate, deadline: (_ for _ in ()).throw(OSError("offline"))},
        requirements={CandidateState.DATA_VALIDATED: (GateRequirement("eligible_sessions", minimum=10),)},
        receiver_health=lambda: {"capture_priority_clear": True},
        policy=ServicePolicy(retry_backoff_seconds=0),
        clock_ns=lambda: now[0],
    )
    service.propose(candidate)

    first = service.run_once()
    second = service.run_once()
    persisted = store.load(candidate.candidate_id)

    assert first.skipped == 1
    assert second.failed == 1
    assert persisted is not None
    assert persisted.state is CandidateState.FAILED_REQUIRES_REWORK
    assert persisted.attempt_count == 2
    assert store.completed_ids() == {candidate.candidate_id}


def test_objective_gate_failure_is_not_retried(tmp_path) -> None:
    store = AutonomousStore(tmp_path)
    candidate = _candidate()
    service = AutonomousIntelligenceService(
        store=store,
        gate_runners={
            CandidateState.DATA_VALIDATED: lambda candidate, deadline: GateResult(
                {"eligible_sessions": 1}, ("evidence/failed.json",)
            )
        },
        requirements={CandidateState.DATA_VALIDATED: (GateRequirement("eligible_sessions", minimum=10),)},
        receiver_health=lambda: {"capture_priority_clear": True},
    )
    service.propose(candidate)

    result = service.run_once()
    persisted = store.load(candidate.candidate_id)

    assert result.failed == 1
    assert persisted is not None
    assert persisted.state is CandidateState.FAILED_REQUIRES_REWORK
    assert persisted.attempt_count == 0
    assert persisted.next_retry_at_ns == 0


def test_service_daily_quota_survives_restart(tmp_path) -> None:
    store = AutonomousStore(tmp_path)
    now_ns = 10_000_000_000
    policy = ServicePolicy(max_candidates_per_run=2, max_candidates_per_day=1)
    calls: list[str] = []

    def make_service() -> AutonomousIntelligenceService:
        return AutonomousIntelligenceService(
            store=store,
            gate_runners={
                CandidateState.DATA_VALIDATED: lambda candidate, deadline: (
                    calls.append(candidate.candidate_id)
                    or GateResult({"eligible_sessions": 12}, ("evidence/data.json",))
                )
            },
            requirements={CandidateState.DATA_VALIDATED: (GateRequirement("eligible_sessions", minimum=10),)},
            receiver_health=lambda: {"capture_priority_clear": True},
            policy=policy,
            clock_ns=lambda: now_ns,
        )

    make_service().propose(_candidate(definition={"candidate": 1}))
    make_service().propose(_candidate(definition={"candidate": 2}))

    first = make_service().run_once()
    second = make_service().run_once()

    assert first.advanced == 1
    assert second.paused_reason == "daily candidate quota reached"
    assert second.skipped == 2
    assert len(calls) == 1
    assert store.attempt_count_for_utc_day(now_ns) == 1


def test_service_fails_candidate_when_disk_budget_is_exceeded(tmp_path) -> None:
    store = AutonomousStore(tmp_path)
    candidate = _candidate_with(
        resource_budget=ResourceBudget(wall_clock_seconds=1, disk_bytes=1, max_attempts=3)
    )

    def runner(candidate: CandidateRecord, deadline_ns: int) -> GateResult:
        store.append_activity({"event": "large-output", "payload": "x" * 100})
        return GateResult({"eligible_sessions": 12}, ("evidence/data.json",))

    service = AutonomousIntelligenceService(
        store=store,
        gate_runners={CandidateState.DATA_VALIDATED: runner},
        requirements={CandidateState.DATA_VALIDATED: (GateRequirement("eligible_sessions", minimum=10),)},
        receiver_health=lambda: {"capture_priority_clear": True},
    )
    service.propose(candidate)

    result = service.run_once()
    persisted = store.load(candidate.candidate_id)

    assert result.failed == 1
    assert persisted is not None
    assert persisted.state is CandidateState.FAILED_REQUIRES_REWORK
    assert "CandidateBudgetExceeded" in persisted.errors[-1]
    assert persisted.attempt_count == 0


def test_service_enforces_store_quota_before_proposal(tmp_path) -> None:
    store = AutonomousStore(tmp_path)
    service = AutonomousIntelligenceService(
        store=store,
        gate_runners={},
        requirements={},
        receiver_health=lambda: {"capture_priority_clear": True},
        policy=ServicePolicy(max_store_bytes=1),
    )
    store.append_activity({"event": "existing"})
    store.publish(_candidate(definition={"existing": True}))

    with pytest.raises(RuntimeError, match="disk quota"):
        service.propose(_candidate())


def test_autonomous_modules_have_no_execution_or_approval_authority_imports() -> None:
    import ast
    from pathlib import Path

    banned = (
        "app.execution",
        "app.paper.execution",
        "app.paper.streaming_engine",
        "app.risk",
        "app.machine_learning.registry",
        "broker",
        "tradovate",
    )
    violations: list[str] = []
    for source_path in sorted(Path("app/research").glob("autonomous_*.py")):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imports: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        violations.extend(
            f"{source_path.name}: {name}"
            for name in imports
            if any(authority in name.lower() for authority in banned)
        )

    assert not violations, f"autonomous modules imported forbidden authority: {violations}"


def test_store_quota_counts_activity_and_checkpoint_state(tmp_path) -> None:
    store = AutonomousStore(tmp_path)
    candidate = _candidate()
    store.publish(candidate)
    before = store.disk_usage_bytes()

    store.append_activity({"event": "published"})
    store.mark_complete(candidate.candidate_id)

    assert store.disk_usage_bytes() > before


def _candidate_with(**kwargs: object) -> CandidateRecord:
    candidate = _candidate()
    defining = {
        "kind": candidate.kind,
        "hypothesis": candidate.hypothesis,
        "proposer": candidate.proposer,
        "created_at_ns": candidate.created_at_ns,
        "software_revision": candidate.software_revision,
        "definition": candidate.definition,
        "baseline_id": candidate.baseline_id,
        "feature_hash": candidate.feature_hash,
        "label_hash": candidate.label_hash,
        "source_session_ids": candidate.source_session_ids,
        "split_policy": candidate.split_policy,
        "seed": candidate.seed,
        "preregistered_gates": candidate.preregistered_gates,
    }
    defining.update(kwargs)
    return propose_candidate(**defining)


def test_model_evidence_reports_zero_oos_honestly(tmp_path) -> None:
    """The ML evidence panel must show a REJECTED, 0-OOS attempt as 'not proven'."""
    from app.gui.autonomous_view import read_autonomous_snapshot

    attempts = tmp_path / "models" / "attempts"
    attempts.mkdir(parents=True)
    (attempts / "attempt-abc.json").write_text(json.dumps({
        "attempt_id": "attempt-abc",
        "model_type": "logistic_regression",
        "validation": {
            "validation_state": "REJECTED", "oos_predictions": 0, "evaluated_days": 0,
            "total_rows": 0, "beats_baseline": False, "note": "insufficient walk-forward data",
        },
    }), encoding="utf-8")
    session_dir = tmp_path / "models" / "per_session" / "session_x"
    session_dir.mkdir(parents=True)
    (session_dir / "report.json").write_text(
        json.dumps({"trained": True, "wins": 12, "losses": 8}), encoding="utf-8")

    snap = read_autonomous_snapshot(
        store_root=tmp_path / "store", reports_root=tmp_path / "reports",
        models_root=tmp_path / "models",
    )
    evidence = snap.model_evidence
    assert evidence.attempts_total == 1
    assert evidence.validated_count == 0
    assert evidence.best_oos_predictions == 0
    assert "0 out-of-sample predictions" in evidence.headline
    assert evidence.attempts[0].state == "REJECTED"
    assert evidence.attempts[0].oos_predictions == 0
    # in-sample plumbing is counted but never presented as validation
    assert evidence.sessions_seen == 1 and evidence.sessions_trainable == 1
    assert evidence.in_sample_labels == 20


def test_model_evidence_reflects_a_validated_attempt(tmp_path) -> None:
    """The panel is not hardcoded to pessimism - a real OOS pass is shown as such."""
    from app.gui.autonomous_view import read_autonomous_snapshot

    attempts = tmp_path / "models" / "attempts"
    attempts.mkdir(parents=True)
    (attempts / "a.json").write_text(json.dumps({
        "attempt_id": "a", "model_type": "xgboost",
        "validation": {"validation_state": "VALIDATED", "oos_predictions": 250,
                       "evaluated_days": 6, "total_rows": 900, "beats_baseline": True},
    }), encoding="utf-8")

    snap = read_autonomous_snapshot(
        store_root=tmp_path / "store", reports_root=tmp_path / "reports",
        models_root=tmp_path / "models",
    )
    evidence = snap.model_evidence
    assert evidence.validated_count == 1
    assert evidence.best_oos_predictions == 250
    assert "validated out-of-sample" in evidence.headline
    assert evidence.attempts[0].beats_baseline is True
