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

from app.research.autonomous_models import CandidateKind, propose_candidate
from app.research.autonomous_service import AutonomousIntelligenceService
from app.research.autonomous_store import AutonomousStore

DEFAULT_STORE_ROOT = Path("data/autonomous")

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
