"""Bounded, shadow-only autonomous research: propose and advance candidates.

Populates the ``AutonomousStore`` with governed candidate proposals and advances
them through the wired governed gates so the Autonomous Intelligence page
reflects real repository state. Wired gates so far: DATA_VALIDATED (recorded
data exists), OFFLINE_TRAINED (a challenger produced out-of-sample predictions),
and WALK_FORWARD_VALIDATED (that model's OOS expectancy after costs beats the
baseline). Each gate is advanced by a SCOPED helper that only ever processes its
own current state, so it can never fail a candidate at an unwired later gate.

Shadow-only by construction: this module imports no execution/broker/paper-order
module, and no gate ever promotes, loads, or trades a model. Candidates carry no
runtime authority; LIVE stays locked.
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
        expectancy = float(validation["expectancy_ticks"])
        baseline = float(validation["baseline_expectancy_ticks"])
        # OFFLINE_TRAINED only requires that a model was trained and produced
        # out-of-sample predictions. The walk-forward PERFORMANCE (does its OOS
        # expectancy after costs beat the baseline?) is computed by the very same
        # evaluation, so it is carried forward here and checked by the separate
        # WALK_FORWARD_VALIDATED gate - the model is never re-trained.
        return {
            "oos_predictions": float(validation["oos_predictions"]),
            "evaluated_days": float(validation["evaluated_days"]),
            "brier_score": float(validation["brier_score"]),
            "taken_trades": float(validation["taken_trades"]),
            "expectancy_ticks": expectancy,
            "baseline_expectancy_ticks": baseline,
            "expectancy_edge_ticks": expectancy - baseline,
            "beats_baseline_after_costs": 1.0 if validation["beats_baseline"] else 0.0,
            # Consistency across independent OOS days - read by STABILITY_VALIDATED.
            "stable_fold_fraction": float(validation.get("stable_fold_fraction", 0.0)),
            # Base cost the expectancy already nets - COST_VALIDATED stresses beyond it.
            "cost_ticks": float(validation.get("cost_ticks", 2.0)),
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
            oos = int(metrics.get("oos_predictions", 0))
            if evidence.passed:
                store.publish(apply_gate(record, evidence), lease=lease)
                store.append_activity({
                    "event": "gate_passed", "candidate_id": record.candidate_id,
                    "gate": CandidateState.OFFLINE_TRAINED.value,
                    "oos_predictions": oos, "recorded_at_ns": now_ns})
                passed += 1
            else:
                # Too few out-of-sample predictions means not enough eligible data
                # yet - NOT a bad candidate. Defer (stay DATA_VALIDATED) so it can
                # pass once more sessions accumulate; do not fail it for rework.
                store.append_activity({
                    "event": "gate_deferred", "candidate_id": record.candidate_id,
                    "gate": CandidateState.OFFLINE_TRAINED.value,
                    "reason": evidence.failure_reason, "oos_predictions": oos,
                    "recorded_at_ns": now_ns})
        finally:
            try:
                store.release(lease)
            except PermissionError:
                pass
    return passed


# WALK_FORWARD_VALIDATED gate: the trained model's out-of-sample expectancy
# after costs must beat the take-everything baseline. The evaluation already
# ran during OFFLINE_TRAINED, so this gate reads the carried-forward metric
# (stored as 1.0/0.0) - it never re-trains.
WALK_FORWARD_REQUIREMENT: tuple[GateRequirement, ...] = (
    GateRequirement("beats_baseline_after_costs", minimum=1.0),
)


def _passed_gate_metrics(record, gate: CandidateState):
    """Return the metrics from the record's most recent PASSED evidence at ``gate``."""
    for evidence in reversed(record.gate_history):
        if evidence.gate is gate and evidence.passed:
            return evidence.metrics
    return None


def advance_walk_forward_validation(
    store: AutonomousStore,
    *,
    now_ns: int,
    limit: int | None = None,
    owner: str = "autonomous-backend",
) -> int:
    """Advance OFFLINE_TRAINED candidates one governed step to WALK_FORWARD_VALIDATED.

    Reads the walk-forward performance that OFFLINE_TRAINED already recorded (no
    re-training) and checks whether the model's out-of-sample expectancy after
    costs beat the baseline. Unlike OFFLINE_TRAINED - where too little data is a
    transient defer - a fully evaluated model that does NOT beat the baseline is
    an honest, terminal REJECTED (the strategy genuinely did not work). If the
    carried-forward metric is absent (e.g. trained before it was recorded), the
    candidate is deferred, never rejected. Returns how many newly validated.
    """
    passed = 0
    processed = 0
    for record in store.list_candidates():
        if record.state is not CandidateState.OFFLINE_TRAINED:
            continue
        if next_gate(record) is not CandidateState.WALK_FORWARD_VALIDATED:
            continue
        if limit is not None and processed >= limit:
            break
        prior = _passed_gate_metrics(record, CandidateState.OFFLINE_TRAINED)
        if prior is None or "beats_baseline_after_costs" not in prior:
            # No carried-forward walk-forward evidence: cannot evaluate this gate
            # without re-running training. Defer (never reject for missing data).
            store.append_activity({
                "event": "gate_deferred", "candidate_id": record.candidate_id,
                "gate": CandidateState.WALK_FORWARD_VALIDATED.value,
                "reason": "walk-forward metrics unavailable; retrain to populate",
                "recorded_at_ns": now_ns})
            continue
        lease = store.try_acquire(record.candidate_id, owner, now_ns=now_ns)
        if lease is None:
            continue
        try:
            processed += 1
            edge = float(prior.get("expectancy_edge_ticks", 0.0))
            metrics = {
                "beats_baseline_after_costs": float(prior["beats_baseline_after_costs"]),
                "expectancy_edge_ticks": edge,
                "taken_trades": float(prior.get("taken_trades", 0.0)),
            }
            evidence = evaluate_requirements(
                gate=CandidateState.WALK_FORWARD_VALIDATED, recorded_at_ns=now_ns,
                metrics=metrics, requirements=WALK_FORWARD_REQUIREMENT,
                evidence_refs=(f"walk_forward:edge_{edge:+.4f}_ticks",))
            if evidence.passed:
                store.publish(apply_gate(record, evidence), lease=lease)
                store.append_activity({
                    "event": "gate_passed", "candidate_id": record.candidate_id,
                    "gate": CandidateState.WALK_FORWARD_VALIDATED.value,
                    "expectancy_edge_ticks": edge, "recorded_at_ns": now_ns})
                passed += 1
            else:
                # Fully evaluated and does not beat baseline: an honest terminal
                # rejection, not a defer. The governed lifecycle is allowed to say
                # "this candidate does not work" - that is the point of the gate.
                store.publish(
                    apply_gate(record, evidence, failure_state=CandidateState.REJECTED),
                    lease=lease)
                store.append_activity({
                    "event": "gate_rejected", "candidate_id": record.candidate_id,
                    "gate": CandidateState.WALK_FORWARD_VALIDATED.value,
                    "reason": evidence.failure_reason,
                    "expectancy_edge_ticks": edge, "recorded_at_ns": now_ns})
        finally:
            try:
                store.release(lease)
            except PermissionError:
                pass
    return passed


# STABILITY_VALIDATED gate: the out-of-sample edge must be CONSISTENT across
# independent trading days, not carried by one lucky day. Reads the carried-
# forward per-fold consistency (stable_fold_fraction) - it never re-trains.
STABILITY_MIN_FOLD_FRACTION = 0.6
STABILITY_REQUIREMENT: tuple[GateRequirement, ...] = (
    GateRequirement("stable_fold_fraction", minimum=STABILITY_MIN_FOLD_FRACTION),
)


def advance_stability_validation(
    store: AutonomousStore,
    *,
    now_ns: int,
    limit: int | None = None,
    owner: str = "autonomous-backend",
) -> int:
    """Advance WALK_FORWARD_VALIDATED candidates one step to STABILITY_VALIDATED.

    Reads the per-fold consistency the training step recorded (no re-training)
    and checks the edge held on a sufficient fraction of independent out-of-sample
    days. Like WALK_FORWARD_VALIDATED, an evaluated-but-unstable edge is an honest
    terminal REJECTED; an absent metric defers. Returns how many newly validated.
    """
    passed = 0
    processed = 0
    for record in store.list_candidates():
        if record.state is not CandidateState.WALK_FORWARD_VALIDATED:
            continue
        if next_gate(record) is not CandidateState.STABILITY_VALIDATED:
            continue
        if limit is not None and processed >= limit:
            break
        prior = _passed_gate_metrics(record, CandidateState.OFFLINE_TRAINED)
        if prior is None or "stable_fold_fraction" not in prior:
            store.append_activity({
                "event": "gate_deferred", "candidate_id": record.candidate_id,
                "gate": CandidateState.STABILITY_VALIDATED.value,
                "reason": "stability metrics unavailable; retrain to populate",
                "recorded_at_ns": now_ns})
            continue
        lease = store.try_acquire(record.candidate_id, owner, now_ns=now_ns)
        if lease is None:
            continue
        try:
            processed += 1
            fraction = float(prior["stable_fold_fraction"])
            evidence = evaluate_requirements(
                gate=CandidateState.STABILITY_VALIDATED, recorded_at_ns=now_ns,
                metrics={"stable_fold_fraction": fraction}, requirements=STABILITY_REQUIREMENT,
                evidence_refs=(f"stability:{fraction:.4f}_fold_fraction",))
            if evidence.passed:
                store.publish(apply_gate(record, evidence), lease=lease)
                store.append_activity({
                    "event": "gate_passed", "candidate_id": record.candidate_id,
                    "gate": CandidateState.STABILITY_VALIDATED.value,
                    "stable_fold_fraction": fraction, "recorded_at_ns": now_ns})
                passed += 1
            else:
                # Evaluated but the edge is not consistent across days: honest,
                # terminal REJECTED (not a defer - the evidence exists and fails).
                store.publish(
                    apply_gate(record, evidence, failure_state=CandidateState.REJECTED),
                    lease=lease)
                store.append_activity({
                    "event": "gate_rejected", "candidate_id": record.candidate_id,
                    "gate": CandidateState.STABILITY_VALIDATED.value,
                    "reason": evidence.failure_reason,
                    "stable_fold_fraction": fraction, "recorded_at_ns": now_ns})
        finally:
            try:
                store.release(lease)
            except PermissionError:
                pass
    return passed


# COST_VALIDATED gate: the edge must SURVIVE a stressed cost assumption. The
# walk-forward expectancy already nets a base cost; this gate additionally
# subtracts COST_STRESS_TICKS of extra cost from the model's per-trade expectancy
# and requires it to stay non-negative - i.e. the edge does not evaporate if real
# costs run higher than assumed. Reads the carried-forward expectancy; no retrain.
COST_STRESS_TICKS = 2.0
COST_REQUIREMENT: tuple[GateRequirement, ...] = (
    GateRequirement("expectancy_after_stress_ticks", minimum=0.0),
)


def advance_cost_validation(
    store: AutonomousStore,
    *,
    now_ns: int,
    limit: int | None = None,
    owner: str = "autonomous-backend",
) -> int:
    """Advance STABILITY_VALIDATED candidates one step to COST_VALIDATED.

    Stress-tests the carried-forward per-trade expectancy against COST_STRESS_TICKS
    of extra cost (no re-training) and requires it to stay non-negative. Like the
    earlier evidence gates: robust -> advances; evaluated-but-fragile -> terminal
    REJECTED; metric absent -> deferred. Returns how many were newly cost-validated.
    """
    passed = 0
    processed = 0
    for record in store.list_candidates():
        if record.state is not CandidateState.STABILITY_VALIDATED:
            continue
        if next_gate(record) is not CandidateState.COST_VALIDATED:
            continue
        if limit is not None and processed >= limit:
            break
        prior = _passed_gate_metrics(record, CandidateState.OFFLINE_TRAINED)
        if prior is None or "expectancy_ticks" not in prior:
            store.append_activity({
                "event": "gate_deferred", "candidate_id": record.candidate_id,
                "gate": CandidateState.COST_VALIDATED.value,
                "reason": "cost metrics unavailable; retrain to populate",
                "recorded_at_ns": now_ns})
            continue
        lease = store.try_acquire(record.candidate_id, owner, now_ns=now_ns)
        if lease is None:
            continue
        try:
            processed += 1
            stressed = float(prior["expectancy_ticks"]) - COST_STRESS_TICKS
            evidence = evaluate_requirements(
                gate=CandidateState.COST_VALIDATED, recorded_at_ns=now_ns,
                metrics={"expectancy_after_stress_ticks": stressed},
                requirements=COST_REQUIREMENT,
                evidence_refs=(f"cost_stress:{stressed:+.4f}_ticks_after_+{COST_STRESS_TICKS}",))
            if evidence.passed:
                store.publish(apply_gate(record, evidence), lease=lease)
                store.append_activity({
                    "event": "gate_passed", "candidate_id": record.candidate_id,
                    "gate": CandidateState.COST_VALIDATED.value,
                    "expectancy_after_stress_ticks": stressed, "recorded_at_ns": now_ns})
                passed += 1
            else:
                # Beats baseline at the assumed cost but not under stress: the edge
                # is too thin to trust against real-world costs. Honest terminal REJECT.
                store.publish(
                    apply_gate(record, evidence, failure_state=CandidateState.REJECTED),
                    lease=lease)
                store.append_activity({
                    "event": "gate_rejected", "candidate_id": record.candidate_id,
                    "gate": CandidateState.COST_VALIDATED.value,
                    "reason": evidence.failure_reason,
                    "expectancy_after_stress_ticks": stressed, "recorded_at_ns": now_ns})
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
    # Walk-forward and stability validation only read carried-forward metrics (no
    # ML), so they are cheap and always run - advancing any candidate left at the
    # prior state by this or an earlier cycle.
    walk_forward = advance_walk_forward_validation(store, now_ns=now_ns)
    stability = advance_stability_validation(store, now_ns=now_ns)
    cost = advance_cost_validation(store, now_ns=now_ns)
    store.append_activity({
        "event": "autonomous_cycle_ran",
        "software_revision": software_revision,
        "newly_proposed": proposed,
        "data_validated": validated,
        "offline_trained": trained,
        "walk_forward_validated": walk_forward,
        "stability_validated": stability,
        "cost_validated": cost,
        "recorded_at_ns": now_ns,
    })
    return {"proposed": proposed, "data_validated": validated,
            "offline_trained": trained, "walk_forward_validated": walk_forward,
            "stability_validated": stability, "cost_validated": cost}
