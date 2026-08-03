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
    assert "run_proposer" in source
    assert "mnq-autonomous-proposer" in source and "daemon=True" in source
    # Capture priority: the proposer waits for capture to initialise first.
    assert "let capture initialise first" in source
