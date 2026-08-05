"""Backend autonomous proposer: bounded, deduplicated, shadow-only wiring."""

from __future__ import annotations

import json
from pathlib import Path

from app.research.autonomous_proposer import (
    RESEARCH_GRID,
    build_service,
    build_store,
    run_proposer,
)


def test_proposes_the_grid_once_and_deduplicates(tmp_path: Path) -> None:
    store_root = tmp_path / "autonomous"
    first = run_proposer(store_root=store_root, now_ns=100, software_revision="rev-a")
    second = run_proposer(store_root=store_root, now_ns=999, software_revision="rev-a")

    assert first == len(RESEARCH_GRID)  # every grid entry proposed
    assert second == 0, "a restart re-proposes nothing (content-addressed dedup)"
    candidates = build_store(store_root).list_candidates()
    assert len(candidates) == len(RESEARCH_GRID)
    assert all(c.state.value == "PROPOSED" for c in candidates), "propose-only: never advanced"


def test_identity_is_stable_across_time_but_varies_by_revision(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    run_proposer(store_root=root_a, now_ns=1, software_revision="rev-a")
    run_proposer(store_root=root_b, now_ns=2, software_revision="rev-b")
    ids_a = {c.candidate_id for c in build_store(root_a).list_candidates()}
    ids_b = {c.candidate_id for c in build_store(root_b).list_candidates()}
    assert ids_a and ids_b and ids_a.isdisjoint(ids_b), "a new software revision = new candidates"


def test_records_an_activity_event(tmp_path: Path) -> None:
    store_root = tmp_path / "autonomous"
    run_proposer(store_root=store_root, now_ns=100, software_revision="rev-a")
    events = [json.loads(line) for line in
              (store_root / "activity.jsonl").read_text(encoding="utf-8").splitlines()]
    kinds = {event["event"] for event in events}
    assert "candidate_proposed" in kinds
    assert "autonomous_proposer_ran" in kinds


def test_service_is_propose_only_with_no_gate_runners(tmp_path: Path) -> None:
    service = build_service(build_store(tmp_path / "autonomous"))
    # No gate runners/requirements: run_once would fail candidates, so the
    # proposer never calls it. This is the guard that keeps candidates PROPOSED.
    assert service.gate_runners == {}
    assert service.requirements == {}


def test_proposer_is_shadow_only_imports_no_execution() -> None:
    import ast

    tree = ast.parse(Path("app/research/autonomous_proposer.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    # It must not import any execution/broker/gateway/order-routing module.
    for module in imported:
        lowered = module.lower()
        assert "execution" not in lowered, f"proposer imports execution module: {module}"
        assert "gateway" not in lowered and "broker" not in lowered, module
    assert "app.paper.execution" not in imported


def test_config_reader_defaults_false(tmp_path: Path) -> None:
    from app.paper.options import read_autonomous_enabled

    assert read_autonomous_enabled(tmp_path / "missing.yaml") is False
    on = tmp_path / "on.yaml"
    on.write_text("autonomous_enabled: true\n", encoding="utf-8")
    assert read_autonomous_enabled(on) is True
    off = tmp_path / "off.yaml"
    off.write_text("autonomous_enabled: false\n", encoding="utf-8")
    assert read_autonomous_enabled(off) is False


def test_backend_wires_the_proposer_gated_by_config_and_capture_priority() -> None:
    source = Path("tools/start_backend.py").read_text(encoding="utf-8")
    assert "read_autonomous_enabled" in source, "proposer must be config-gated"
    assert "run_autonomous_cycle" in source
    assert "mnq-autonomous-proposer" in source and "daemon=True" in source
    # Capture priority: the proposer waits for capture to initialise first.
    assert "let capture initialise first" in source


# -- DATA_VALIDATED gate -------------------------------------------------------------


def _raw_with_sessions(root: Path, count: int) -> Path:
    for i in range(count):
        session = root / "2026-08-01" / f"session_{i}"
        session.mkdir(parents=True)
        (session / "trades.parquet").write_bytes(b"parquet")
    return root


def test_count_eligible_sessions(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import count_eligible_sessions

    assert count_eligible_sessions(tmp_path / "missing") == 0
    assert count_eligible_sessions(_raw_with_sessions(tmp_path / "raw", 3)) == 3


def test_data_validation_advances_proposed_to_data_validated(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import (
        advance_data_validation,
        build_store,
        run_proposer,
    )

    store_root = tmp_path / "autonomous"
    raw = _raw_with_sessions(tmp_path / "raw", 2)
    run_proposer(store_root=store_root, now_ns=100, software_revision="rev-a")
    store = build_store(store_root)

    passed = advance_data_validation(store, raw_root=raw, now_ns=200)
    assert passed == 3, "each proposed candidate passes data validation"
    states = {c.state.value for c in store.list_candidates()}
    assert states == {"DATA_VALIDATED"}
    # Idempotent: re-running does not re-advance or fail already-validated candidates.
    assert advance_data_validation(store, raw_root=raw, now_ns=300) == 0
    assert {c.state.value for c in store.list_candidates()} == {"DATA_VALIDATED"}


def test_data_validation_never_touches_a_later_unwired_gate(tmp_path: Path) -> None:
    """A DATA_VALIDATED candidate must NOT be failed at the unwired OFFLINE_TRAINED."""
    from app.research.autonomous_proposer import advance_data_validation, build_store, run_proposer

    store_root = tmp_path / "autonomous"
    raw = _raw_with_sessions(tmp_path / "raw", 1)
    run_proposer(store_root=store_root, now_ns=100, software_revision="rev-a")
    store = build_store(store_root)
    advance_data_validation(store, raw_root=raw, now_ns=200)
    advance_data_validation(store, raw_root=raw, now_ns=300)  # second pass

    states = {c.state.value for c in store.list_candidates()}
    assert "FAILED_REQUIRES_REWORK" not in states, "must not fail at an unwired later gate"
    assert states == {"DATA_VALIDATED"}


def test_data_validation_fails_closed_with_no_recorded_data(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import advance_data_validation, build_store, run_proposer

    store_root = tmp_path / "autonomous"
    run_proposer(store_root=store_root, now_ns=100, software_revision="rev-a")
    store = build_store(store_root)
    passed = advance_data_validation(store, raw_root=tmp_path / "empty_raw", now_ns=200)
    assert passed == 0
    # No usable data -> candidates honestly fail data validation (governed terminal).
    assert {c.state.value for c in store.list_candidates()} == {"FAILED_REQUIRES_REWORK"}


def test_run_autonomous_cycle_proposes_and_validates(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import RESEARCH_GRID, run_autonomous_cycle

    result = run_autonomous_cycle(
        store_root=tmp_path / "autonomous", raw_root=_raw_with_sessions(tmp_path / "raw", 2),
        now_ns=100, software_revision="rev-a")
    grid = len(RESEARCH_GRID)
    assert result == {"proposed": grid, "data_validated": grid, "offline_trained": 0,
                      "walk_forward_validated": 0, "stability_validated": 0, "cost_validated": 0,
                      "shadow_candidate": 0, "shadow_observing": 0, "shadow_eligible": 0,
                      "shadow_approved": 0}


# -- OFFLINE_TRAINED gate (injected trainer: no heavy ML in tests) --------------------


def _validated_store(tmp_path: Path):
    from app.research.autonomous_proposer import advance_data_validation, build_store, run_proposer

    store_root = tmp_path / "autonomous"
    run_proposer(store_root=store_root, now_ns=100, software_revision="rev-a")
    store = build_store(store_root)
    advance_data_validation(store, raw_root=_raw_with_sessions(tmp_path / "raw", 2), now_ns=200)
    return store


def test_offline_training_advances_data_validated_to_offline_trained(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import advance_offline_training

    store = _validated_store(tmp_path)
    passed = advance_offline_training(
        store, trainer=lambda c: {"oos_predictions": 120, "evaluated_days": 5}, now_ns=300)
    assert passed == 3
    assert {c.state.value for c in store.list_candidates()} == {"OFFLINE_TRAINED"}
    # Idempotent: already-trained candidates are not re-processed.
    assert advance_offline_training(store, trainer=lambda c: {"oos_predictions": 120}, now_ns=400) == 0


def test_offline_training_defers_without_oos_predictions(tmp_path: Path) -> None:
    import json

    from app.research.autonomous_proposer import advance_offline_training

    store = _validated_store(tmp_path)
    passed = advance_offline_training(store, trainer=lambda c: {"oos_predictions": 0}, now_ns=300)
    assert passed == 0
    # Insufficient eligible data is NOT a candidate failure - defer, stay DATA_VALIDATED.
    assert {c.state.value for c in store.list_candidates()} == {"DATA_VALIDATED"}
    events = {json.loads(line)["event"] for line in
              (store.activity_path).read_text(encoding="utf-8").splitlines()}
    assert "gate_deferred" in events and "gate_failed" not in events


def test_offline_training_error_is_transient_not_terminal(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import advance_offline_training

    store = _validated_store(tmp_path)

    def boom(_candidate):
        raise RuntimeError("dataset build failed")

    passed = advance_offline_training(store, trainer=boom, now_ns=300)
    assert passed == 0
    # A training error must NOT fail the candidate - it stays DATA_VALIDATED to retry.
    assert {c.state.value for c in store.list_candidates()} == {"DATA_VALIDATED"}


def test_offline_training_is_bounded_by_limit(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import advance_offline_training

    store = _validated_store(tmp_path)
    passed = advance_offline_training(
        store, trainer=lambda c: {"oos_predictions": 120}, now_ns=300, limit=1)
    assert passed == 1
    states = [c.state.value for c in store.list_candidates()]
    assert states.count("OFFLINE_TRAINED") == 1 and states.count("DATA_VALIDATED") == 2


def test_offline_training_ignores_candidates_not_yet_data_validated(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import advance_offline_training, build_store, run_proposer

    store_root = tmp_path / "autonomous"
    run_proposer(store_root=store_root, now_ns=100, software_revision="rev-a")
    store = build_store(store_root)
    passed = advance_offline_training(store, trainer=lambda c: {"oos_predictions": 120}, now_ns=200)
    assert passed == 0
    assert {c.state.value for c in store.list_candidates()} == {"PROPOSED"}


def test_offline_training_uses_the_candidate_geometry(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import advance_offline_training

    store = _validated_store(tmp_path)
    seen: list[tuple[int, int]] = []

    def trainer(candidate):
        d = candidate.definition
        seen.append((int(d["target_ticks"]), int(d["stop_ticks"])))
        return {"oos_predictions": 100}

    advance_offline_training(store, trainer=trainer, now_ns=300)
    assert set(seen) == {(12, 8), (8, 8), (6, 4)}, "trainer receives each grid geometry"


# -- WALK_FORWARD_VALIDATED gate (reads carried-forward metrics: no ML) ---------------


def _offline_trained_store(tmp_path: Path, trainer):
    from app.research.autonomous_proposer import advance_offline_training

    store = _validated_store(tmp_path)
    advance_offline_training(store, trainer=trainer, now_ns=300)
    return store


def _beats(edge: float) -> dict:
    return {"oos_predictions": 120.0, "beats_baseline_after_costs": 1.0 if edge > 0 else 0.0,
            "expectancy_edge_ticks": edge, "taken_trades": 40.0}


def test_walk_forward_advances_when_the_model_beats_baseline(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import advance_walk_forward_validation

    store = _offline_trained_store(tmp_path, lambda c: _beats(0.8))
    passed = advance_walk_forward_validation(store, now_ns=400)
    assert passed == 3
    assert {c.state.value for c in store.list_candidates()} == {"WALK_FORWARD_VALIDATED"}
    # Idempotent: already-validated candidates are not re-processed.
    assert advance_walk_forward_validation(store, now_ns=500) == 0


def test_walk_forward_rejects_when_the_model_does_not_beat_baseline(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import advance_walk_forward_validation

    store = _offline_trained_store(tmp_path, lambda c: _beats(-0.5))
    passed = advance_walk_forward_validation(store, now_ns=400)
    assert passed == 0
    # A fully evaluated model that does not beat baseline is an honest terminal REJECT.
    assert {c.state.value for c in store.list_candidates()} == {"REJECTED"}
    events = {json.loads(line)["event"] for line in
              store.activity_path.read_text(encoding="utf-8").splitlines()}
    assert "gate_rejected" in events


def test_walk_forward_defers_when_the_metric_is_absent(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import advance_walk_forward_validation

    # OFFLINE_TRAINED by a trainer that never recorded walk-forward performance.
    store = _offline_trained_store(tmp_path, lambda c: {"oos_predictions": 120.0})
    passed = advance_walk_forward_validation(store, now_ns=400)
    assert passed == 0
    # Missing evidence is a defer, never a reject: candidate stays OFFLINE_TRAINED.
    assert {c.state.value for c in store.list_candidates()} == {"OFFLINE_TRAINED"}
    events = {json.loads(line)["event"] for line in
              store.activity_path.read_text(encoding="utf-8").splitlines()}
    assert "gate_deferred" in events and "gate_rejected" not in events


def test_walk_forward_ignores_candidates_not_yet_offline_trained(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import advance_walk_forward_validation

    store = _validated_store(tmp_path)  # DATA_VALIDATED, not yet OFFLINE_TRAINED
    assert advance_walk_forward_validation(store, now_ns=400) == 0
    assert {c.state.value for c in store.list_candidates()} == {"DATA_VALIDATED"}


def test_walk_forward_is_bounded_by_limit(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import advance_walk_forward_validation

    store = _offline_trained_store(tmp_path, lambda c: _beats(0.8))
    passed = advance_walk_forward_validation(store, now_ns=400, limit=1)
    assert passed == 1
    states = [c.state.value for c in store.list_candidates()]
    assert states.count("WALK_FORWARD_VALIDATED") == 1 and states.count("OFFLINE_TRAINED") == 2


# -- STABILITY_VALIDATED gate (reads carried-forward per-fold consistency) ------------


def _walk_forward_validated_store(tmp_path: Path, *, stable: float, with_stability: bool = True):
    from app.research.autonomous_proposer import advance_walk_forward_validation

    def trainer(_candidate):
        metrics = {"oos_predictions": 120.0, "beats_baseline_after_costs": 1.0,
                   "expectancy_edge_ticks": 0.8, "taken_trades": 40.0}
        if with_stability:
            metrics["stable_fold_fraction"] = stable
        return metrics

    store = _offline_trained_store(tmp_path, trainer)
    advance_walk_forward_validation(store, now_ns=350)
    return store


def test_stability_advances_when_edge_is_consistent_across_days(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import advance_stability_validation

    store = _walk_forward_validated_store(tmp_path, stable=0.8)
    passed = advance_stability_validation(store, now_ns=400)
    assert passed == 3
    assert {c.state.value for c in store.list_candidates()} == {"STABILITY_VALIDATED"}
    assert advance_stability_validation(store, now_ns=500) == 0  # idempotent


def test_stability_rejects_when_edge_is_not_consistent(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import advance_stability_validation

    store = _walk_forward_validated_store(tmp_path, stable=0.2)  # below the 0.6 floor
    passed = advance_stability_validation(store, now_ns=400)
    assert passed == 0
    assert {c.state.value for c in store.list_candidates()} == {"REJECTED"}
    events = {json.loads(line)["event"] for line in
              store.activity_path.read_text(encoding="utf-8").splitlines()}
    assert "gate_rejected" in events


def test_stability_defers_when_the_metric_is_absent(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import advance_stability_validation

    store = _walk_forward_validated_store(tmp_path, stable=0.0, with_stability=False)
    passed = advance_stability_validation(store, now_ns=400)
    assert passed == 0
    # Missing evidence defers, never rejects: candidate stays WALK_FORWARD_VALIDATED.
    assert {c.state.value for c in store.list_candidates()} == {"WALK_FORWARD_VALIDATED"}
    events = {json.loads(line)["event"] for line in
              store.activity_path.read_text(encoding="utf-8").splitlines()}
    assert "gate_deferred" in events and "gate_rejected" not in events


def test_stability_ignores_candidates_not_yet_walk_forward_validated(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import advance_stability_validation

    store = _offline_trained_store(tmp_path, lambda c: _beats(0.8))  # OFFLINE_TRAINED only
    assert advance_stability_validation(store, now_ns=400) == 0
    assert {c.state.value for c in store.list_candidates()} == {"OFFLINE_TRAINED"}


# -- COST_VALIDATED gate (stress the carried-forward expectancy: no ML) ---------------


def _stability_validated_store(tmp_path: Path, *, expectancy: float = 3.0,
                               with_expectancy: bool = True):
    from app.research.autonomous_proposer import (
        advance_stability_validation,
        advance_walk_forward_validation,
    )

    def trainer(_candidate):
        metrics = {"oos_predictions": 120.0, "beats_baseline_after_costs": 1.0,
                   "expectancy_edge_ticks": 0.8, "taken_trades": 40.0, "stable_fold_fraction": 0.8}
        if with_expectancy:
            metrics["expectancy_ticks"] = expectancy
        return metrics

    store = _offline_trained_store(tmp_path, trainer)
    advance_walk_forward_validation(store, now_ns=350)
    advance_stability_validation(store, now_ns=360)
    return store


def test_cost_advances_when_the_edge_survives_stress(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import advance_cost_validation

    store = _stability_validated_store(tmp_path, expectancy=3.0)  # 3.0 - 2.0 stress = +1.0
    passed = advance_cost_validation(store, now_ns=400)
    assert passed == 3
    assert {c.state.value for c in store.list_candidates()} == {"COST_VALIDATED"}
    assert advance_cost_validation(store, now_ns=500) == 0  # idempotent


def test_cost_rejects_when_the_edge_vanishes_under_stress(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import advance_cost_validation

    store = _stability_validated_store(tmp_path, expectancy=1.0)  # 1.0 - 2.0 stress = -1.0
    passed = advance_cost_validation(store, now_ns=400)
    assert passed == 0
    assert {c.state.value for c in store.list_candidates()} == {"REJECTED"}
    events = {json.loads(line)["event"] for line in
              store.activity_path.read_text(encoding="utf-8").splitlines()}
    assert "gate_rejected" in events


def test_cost_defers_when_the_metric_is_absent(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import advance_cost_validation

    store = _stability_validated_store(tmp_path, with_expectancy=False)
    passed = advance_cost_validation(store, now_ns=400)
    assert passed == 0
    assert {c.state.value for c in store.list_candidates()} == {"STABILITY_VALIDATED"}
    events = {json.loads(line)["event"] for line in
              store.activity_path.read_text(encoding="utf-8").splitlines()}
    assert "gate_deferred" in events and "gate_rejected" not in events


def test_cost_ignores_candidates_not_yet_stability_validated(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import advance_cost_validation

    store = _walk_forward_validated_store(tmp_path, stable=0.8)  # WALK_FORWARD_VALIDATED only
    assert advance_cost_validation(store, now_ns=400) == 0
    assert {c.state.value for c in store.list_candidates()} == {"WALK_FORWARD_VALIDATED"}


# -- Shadow stages (injected shadow_observer: no live pipeline in tests) --------------


def _cost_validated_store(tmp_path: Path):
    from app.research.autonomous_proposer import advance_cost_validation

    store = _stability_validated_store(tmp_path, expectancy=3.0)
    advance_cost_validation(store, now_ns=370)
    return store


def _strong_shadow(_candidate):
    return {"shadow_decisions": 150.0, "shadow_days": 7.0, "shadow_beats_baseline": 1.0}


def test_shadow_candidate_admits_cost_validated(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import advance_shadow_candidate

    store = _cost_validated_store(tmp_path)
    assert advance_shadow_candidate(store, now_ns=400) == 3
    assert {c.state.value for c in store.list_candidates()} == {"SHADOW_CANDIDATE"}


def test_shadow_observation_defers_without_a_pipeline(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import (
        advance_shadow_candidate,
        advance_shadow_observation,
    )

    store = _cost_validated_store(tmp_path)
    advance_shadow_candidate(store, now_ns=400)
    # The default observer yields None (no shadow pipeline): defer, never advance.
    assert advance_shadow_observation(store, now_ns=410) == 0
    assert {c.state.value for c in store.list_candidates()} == {"SHADOW_CANDIDATE"}
    events = {json.loads(line)["event"] for line in
              store.activity_path.read_text(encoding="utf-8").splitlines()}
    assert "gate_deferred" in events


def test_shadow_full_chain_reaches_approved_with_a_strong_record(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import (
        advance_shadow_approval,
        advance_shadow_candidate,
        advance_shadow_eligibility,
        advance_shadow_observation,
    )

    store = _cost_validated_store(tmp_path)
    assert advance_shadow_candidate(store, now_ns=400) == 3
    assert advance_shadow_observation(store, shadow_observer=_strong_shadow, now_ns=410) == 3
    assert advance_shadow_eligibility(store, shadow_observer=_strong_shadow, now_ns=420) == 3
    assert advance_shadow_approval(store, shadow_observer=_strong_shadow, now_ns=430) == 3
    assert {c.state.value for c in store.list_candidates()} == {"SHADOW_APPROVED"}


def test_shadow_eligibility_defers_on_thin_observation(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import (
        advance_shadow_candidate,
        advance_shadow_eligibility,
        advance_shadow_observation,
    )

    def thin(_candidate):
        return {"shadow_decisions": 5.0, "shadow_days": 2.0, "shadow_beats_baseline": 1.0}

    store = _cost_validated_store(tmp_path)
    advance_shadow_candidate(store, now_ns=400)
    advance_shadow_observation(store, shadow_observer=thin, now_ns=410)  # 5 >= 1 -> OBSERVING
    assert advance_shadow_eligibility(store, shadow_observer=thin, now_ns=420) == 0  # 5 < 100
    assert {c.state.value for c in store.list_candidates()} == {"SHADOW_OBSERVING"}


def test_shadow_approval_rejects_a_failing_shadow_record(tmp_path: Path) -> None:
    from app.research.autonomous_proposer import (
        advance_shadow_approval,
        advance_shadow_candidate,
        advance_shadow_eligibility,
        advance_shadow_observation,
    )

    def loses(_candidate):
        return {"shadow_decisions": 150.0, "shadow_days": 7.0, "shadow_beats_baseline": 0.0}

    store = _cost_validated_store(tmp_path)
    advance_shadow_candidate(store, now_ns=400)
    advance_shadow_observation(store, shadow_observer=loses, now_ns=410)
    advance_shadow_eligibility(store, shadow_observer=loses, now_ns=420)
    assert advance_shadow_approval(store, shadow_observer=loses, now_ns=430) == 0
    assert {c.state.value for c in store.list_candidates()} == {"REJECTED"}


def test_config_reader_training_defaults_false(tmp_path: Path) -> None:
    from app.paper.options import read_autonomous_training_enabled

    assert read_autonomous_training_enabled(tmp_path / "missing.yaml") is False
    on = tmp_path / "on.yaml"
    on.write_text("autonomous_training_enabled: true\n", encoding="utf-8")
    assert read_autonomous_training_enabled(on) is True


def test_backend_wires_the_opt_in_training_flag() -> None:
    source = Path("tools/start_backend.py").read_text(encoding="utf-8")
    assert "read_autonomous_training_enabled" in source
    assert "train=_autonomous_train" in source
