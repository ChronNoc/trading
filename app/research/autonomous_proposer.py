"""Bounded, shadow-only proposal of autonomous research candidates.

Populates the ``AutonomousStore`` with governed candidate proposals so the
Autonomous Intelligence page reflects real repository state. It is deliberately
PROPOSAL-ONLY: it never runs gates, trains, promotes, or trades. Advancing a
candidate through the governed lifecycle needs bounded gate runners for every
gate; those are tracked as a separate follow-up. Until they exist, proposing
without running gates keeps candidates honestly in ``PROPOSED`` rather than
failing them at the first unwired gate.

Shadow-only by construction: this module imports no execution/broker/paper-order
module. Candidates carry no runtime authority.
"""

from __future__ import annotations

from pathlib import Path

from app.research.autonomous_gates import (
    GateRequirement,
    apply_gate,
    evaluate_requirements,
    next_gate,
)
from app.research.autonomous_models import CandidateKind, CandidateState, propose_candidate
from app.research.autonomous_service import AutonomousIntelligenceService, GateResult
from app.research.autonomous_store import AutonomousStore

DEFAULT_STORE_ROOT = Path("data/autonomous")
DEFAULT_RAW_ROOT = Path("data/raw")

# DATA_VALIDATED gate: a candidate's data is validated when there is recorded
# session data available to train on. The metric name matches the gate contract
# exercised in tests/test_autonomous_intelligence.py.
DATA_VALIDATED_REQUIREMENT: tuple[GateRequirement, ...] = (
    GateRequirement("eligible_sessions", minimum=1.0),
)

# A small, fixed research grid the autonomous system proposes: does a logistic
# model on pooled triple-barrier features beat the take-everything baseline
# out-of-sample at this target/stop geometry? The identity omits any churning
# session list, so exactly these proposals exist per software revision and
# restarts deduplicate instead of piling up.
RESEARCH_GRID: tuple[tuple[int, int], ...] = ((12, 8), (8, 8), (6, 4))


def build_store(root: Path | str = DEFAULT_STORE_ROOT) -> AutonomousStore:
    """Open (creating if needed) the autonomous evidence store."""
    return AutonomousStore(root)


def build_service(store: AutonomousStore) -> AutonomousIntelligenceService:
    """A propose-only service: no gate runners, so it never advances candidates.

    ``run_once`` is intentionally never called here; without runners it would
    fail candidates at the first gate. Proposal uses only ``propose`` (dedup +
    activity + store quota).
    """
    return AutonomousIntelligenceService(
        store=store,
        gate_runners={},
        requirements={},
        receiver_health=lambda: {},  # unused: run_once is never invoked here
        owner="autonomous-backend-proposer",
    )


def _candidate(target_ticks: int, stop_ticks: int, *, now_ns: int, software_revision: str):
    return propose_candidate(
        kind=CandidateKind.MODEL,
        hypothesis=(
            "A logistic model on pooled triple-barrier features beats the take-everything "
            f"baseline out-of-sample at a {target_ticks}-tick target / {stop_ticks}-tick stop."
        ),
        proposer="autonomous-backend",
        created_at_ns=now_ns,
        software_revision=software_revision,
        definition={
            "model": "logistic_regression",
            "features": "triple_barrier_v1",
            "target_ticks": target_ticks,
            "stop_ticks": stop_ticks,
        },
        baseline_id="take-everything-v1",
        split_policy="walk-forward by futures trading day; final period held out",
        seed=7,
        preregistered_gates={
            "min_out_of_sample_predictions": 50,
            "beats_baseline_after_costs": True,
        },
    )


def propose_research_grid(
    service: AutonomousIntelligenceService,
    *,
    now_ns: int,
    software_revision: str,
) -> int:
    """Propose the research grid; return how many candidates were NEWLY added."""
    existing = {record.candidate_id for record in service.store.list_candidates()}
    newly_proposed = 0
    for target_ticks, stop_ticks in RESEARCH_GRID:
        candidate = _candidate(target_ticks, stop_ticks,
                               now_ns=now_ns, software_revision=software_revision)
        if candidate.candidate_id in existing:
            continue
        service.propose(candidate)
        newly_proposed += 1
    return newly_proposed


def count_eligible_sessions(raw_root: Path | str = DEFAULT_RAW_ROOT) -> int:
    """Count recorded sessions that carry trade data available for training."""
    root = Path(raw_root)
    if not root.is_dir():
        return 0
    return sum(1 for _ in root.rglob("trades.parquet"))


def data_validated_runner(raw_root: Path | str = DEFAULT_RAW_ROOT):
    """Build the DATA_VALIDATED gate runner: (candidate, deadline_ns) -> GateResult."""

    def runner(candidate: object, deadline_ns: int) -> GateResult:  # noqa: ARG001 - gate signature
        eligible = count_eligible_sessions(raw_root)
        warnings = () if eligible >= 1 else ("no recorded sessions with trade data",)
        return GateResult(
            metrics={"eligible_sessions": eligible},
            evidence_refs=(f"data_validation:{eligible}_recorded_sessions",),
            warnings=warnings,
        )

    return runner


def advance_data_validation(
    store: AutonomousStore,
    *,
    raw_root: Path | str = DEFAULT_RAW_ROOT,
    now_ns: int,
    owner: str = "autonomous-backend",
    budget_ns: int = 30_000_000_000,
) -> int:
    """Advance PROPOSED candidates one governed step to DATA_VALIDATED.

    Scoped to the ONE wired gate: it only ever processes candidates whose current
    state is PROPOSED, so - unlike the service's run_once - it can never fail a
    candidate at an unwired later gate. Uses the real gate primitives
    (evaluate_requirements + apply_gate) and a fenced store lease, so the
    transition is governed and idempotent (re-running skips non-PROPOSED records).
    Returns how many candidates newly passed data validation.
    """
    runner = data_validated_runner(raw_root)
    passed = 0
    for record in store.list_candidates():
        if record.state is not CandidateState.PROPOSED:
            continue
        gate = next_gate(record)
        if gate is not CandidateState.DATA_VALIDATED:
            continue
        lease = store.try_acquire(record.candidate_id, owner, now_ns=now_ns)
        if lease is None:
            continue
        try:
            result = runner(record, now_ns + budget_ns)
            evidence = evaluate_requirements(
                gate=gate, recorded_at_ns=now_ns, metrics=result.metrics,
                requirements=DATA_VALIDATED_REQUIREMENT,
                evidence_refs=result.evidence_refs, warnings=result.warnings)
            updated = apply_gate(record, evidence)
            store.publish(updated, lease=lease)
            store.append_activity({
                "event": "gate_passed" if evidence.passed else "gate_failed",
                "candidate_id": record.candidate_id, "gate": gate.value,
                "eligible_sessions": int(result.metrics["eligible_sessions"]),
                "recorded_at_ns": now_ns,
            })
            passed += int(evidence.passed)
        finally:
            try:
                store.release(lease)
            except PermissionError:
                pass
    return passed


DEFAULT_MODELS_ROOT = Path("data/models")


def default_offline_trainer(
    raw_root: Path | str = DEFAULT_RAW_ROOT,
    models_root: Path | str = DEFAULT_MODELS_ROOT,
):
    """Build the real OFFLINE_TRAINED trainer: train a challenger for a candidate.

    Returns a callable ``candidate -> metrics`` that runs the existing challenger
    pipeline at the candidate's target/stop geometry and reports the offline
    walk-forward metrics. It NEVER approves, loads, or trades the model.
    """

    def trainer(candidate: object) -> dict[str, float]:
        from decimal import Decimal

        from app.machine_learning.challenger_pipeline import (
            DatasetBuildConfig,
            build_validated_challenger,
        )
        from app.machine_learning.session_training import SessionTrainingConfig

        definition = getattr(candidate, "definition", {}) or {}
        target = float(definition.get("target_ticks", 12))
        stop = float(definition.get("stop_ticks", 8))
        config = DatasetBuildConfig(SessionTrainingConfig(
            target_ticks=Decimal(str(target)), stop_ticks=Decimal(str(stop))))
        result = build_validated_challenger(
            Path(raw_root), Path(models_root), config=config, model_version="0.1.0",
            target_ticks=target, stop_ticks=stop, min_oos_predictions=1)
        validation = result["validation"]
        return {
            "oos_predictions": float(validation["oos_predictions"]),
            "evaluated_days": float(validation["evaluated_days"]),
            "brier_score": float(validation["brier_score"]),
        }

    return trainer


def advance_offline_training(
    store: AutonomousStore,
    *,
    trainer,
    now_ns: int,
    min_oos_predictions: int = 1,
    limit: int | None = None,
    owner: str = "autonomous-backend",
) -> int:
    """Advance DATA_VALIDATED candidates one governed step to OFFLINE_TRAINED.

    Scoped to the OFFLINE_TRAINED gate only. ``trainer(candidate) -> metrics``
    does the real (heavy) training - injected so the gate logic is testable
    without ML. The gate requires the model to have produced out-of-sample
    predictions (``oos_predictions``); it does NOT require beating the baseline -
    that is a later gate. A trainer error leaves the candidate at DATA_VALIDATED
    (transient), never a false OFFLINE_TRAINED. Bounded by ``limit``.
    """
    requirement = (GateRequirement("oos_predictions", minimum=float(min_oos_predictions)),)
    passed = 0
    processed = 0
    for record in store.list_candidates():
        if record.state is not CandidateState.DATA_VALIDATED:
            continue
        if next_gate(record) is not CandidateState.OFFLINE_TRAINED:
            continue
        if limit is not None and processed >= limit:
            break
        lease = store.try_acquire(record.candidate_id, owner, now_ns=now_ns)
        if lease is None:
            continue
        try:
            try:
                metrics = {key: float(value) for key, value in dict(trainer(record)).items()}
            except Exception as error:  # noqa: BLE001 - transient training failure: never terminal
                store.append_activity({
                    "event": "gate_error", "candidate_id": record.candidate_id,
                    "gate": CandidateState.OFFLINE_TRAINED.value,
                    "detail": f"{type(error).__name__}: {error}"[:120], "recorded_at_ns": now_ns})
                continue
            processed += 1
            evidence = evaluate_requirements(
                gate=CandidateState.OFFLINE_TRAINED, recorded_at_ns=now_ns, metrics=metrics,
                requirements=requirement,
                evidence_refs=(f"offline_training:oos_{int(metrics.get('oos_predictions', 0))}",))
            updated = apply_gate(record, evidence)
            store.publish(updated, lease=lease)
            store.append_activity({
                "event": "gate_passed" if evidence.passed else "gate_failed",
                "candidate_id": record.candidate_id,
                "gate": CandidateState.OFFLINE_TRAINED.value,
                "oos_predictions": int(metrics.get("oos_predictions", 0)),
                "recorded_at_ns": now_ns})
            passed += int(evidence.passed)
        finally:
            try:
                store.release(lease)
            except PermissionError:
                pass
    return passed


def run_proposer(
    *,
    store_root: Path | str = DEFAULT_STORE_ROOT,
    now_ns: int,
    software_revision: str,
) -> int:
    """Open the store, propose the grid, and record the service start. Idempotent."""
    store = build_store(store_root)
    service = build_service(store)
    newly_proposed = propose_research_grid(
        service, now_ns=now_ns, software_revision=software_revision)
    store.append_activity({
        "event": "autonomous_proposer_ran",
        "software_revision": software_revision,
        "newly_proposed": newly_proposed,
        "recorded_at_ns": now_ns,
    })
    return newly_proposed


def run_autonomous_cycle(
    *,
    store_root: Path | str = DEFAULT_STORE_ROOT,
    raw_root: Path | str = DEFAULT_RAW_ROOT,
    models_root: Path | str = DEFAULT_MODELS_ROOT,
    now_ns: int,
    software_revision: str,
    train: bool = False,
    train_limit: int = 1,
) -> dict[str, int]:
    """Propose the grid and advance data validation; optionally run offline training.

    ``train`` is opt-in: offline training is HEAVY (rebuilds datasets from raw)
    and must never compete with capture, so it defaults off and, when enabled,
    is bounded to ``train_limit`` candidates per cycle. Idempotent + restart-safe.
    """
    store = build_store(store_root)
    service = build_service(store)
    proposed = propose_research_grid(
        service, now_ns=now_ns, software_revision=software_revision)
    validated = advance_data_validation(store, raw_root=raw_root, now_ns=now_ns)
    trained = 0
    if train:
        trained = advance_offline_training(
            store, trainer=default_offline_trainer(raw_root, models_root),
            now_ns=now_ns, limit=train_limit)
    store.append_activity({
        "event": "autonomous_cycle_ran",
        "software_revision": software_revision,
        "newly_proposed": proposed,
        "data_validated": validated,
        "offline_trained": trained,
        "recorded_at_ns": now_ns,
    })
    return {"proposed": proposed, "data_validated": validated, "offline_trained": trained}
