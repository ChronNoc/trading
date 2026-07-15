"""Tests for the STAGE 6A automatic high-throughput research engine."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.database.recorder import MarketSessionRecorder
from app.research.auto_research import (
    AcceptedSetup,
    CandidateConfig,
    HardwareProfile,
    ReceiverHealth,
    ResearchCheckpoint,
    ResearchRuntimeConfig,
    SessionRef,
    canonical_candidate,
    default_evaluator,
    detect_hardware,
    discover_pending,
    research_pair_key,
    resolve_worker_count,
    run_auto_research,
    throttle_worker_count,
)


def _setup(config_hash: str, *, canonical: bool, ts: int, day: str = "2026-07-15", won: bool = True) -> AcceptedSetup:
    return AcceptedSetup(
        session_id="session_A", config_hash=config_hash, strategy_version="order-flow-plan-v1",
        is_canonical=canonical, direction="long", trading_day=day, decision_ts_ns=ts,
        defended_price=Decimal("29450.00"), net_pnl_per_contract=Decimal("38") if won else Decimal("-21"),
        r_multiple=Decimal("2.0") if won else Decimal("-1.0"), outcome="target_first" if won else "stop_first",
    )


def test_config_hash_is_immutable_and_discriminating() -> None:
    """Identical config -> identical hash; any change -> different hash."""
    a = CandidateConfig(stop_buffer_points=Decimal("10"), decision_stride=25)
    b = CandidateConfig(stop_buffer_points=Decimal("10"), decision_stride=25)
    c = CandidateConfig(stop_buffer_points=Decimal("12"), decision_stride=25)
    assert a.config_hash == b.config_hash
    assert a.config_hash != c.config_hash
    assert len(a.config_hash) == 16


def test_one_setup_evaluated_by_many_candidates_counts_once() -> None:
    """Three candidates trading the same underlying setup = 3 raw trades, 1 unique."""
    canon = canonical_candidate()
    exp1 = CandidateConfig(stop_buffer_points=Decimal("12"), label="exp1")
    exp2 = CandidateConfig(decision_stride=20, label="exp2")
    ts = 1_752_537_751_000_000_000

    def evaluator(session: SessionRef, candidate: CandidateConfig) -> list[AcceptedSetup]:
        # Every candidate "finds" the same underlying setup (same session/dir/time/price).
        return [_setup(candidate.config_hash, canonical=candidate.is_canonical, ts=ts + 1_000_000)]

    result = run_auto_research([SessionRef("session_A", Path("x"))], [canon, exp1, exp2], evaluator=evaluator)
    assert result.raw_candidate_trades == 3
    assert result.unique_underlying_setups == 1
    assert result.duplicate_overlap == 2
    # Canonical stays separate from experimental.
    assert result.canonical_trades == 1
    assert result.experimental_trades == 2
    # Overlap map shows all three candidates traded the one setup.
    assert any(len(hashes) == 3 for hashes in result.setup_overlap.values())


def test_distinct_setups_are_not_merged() -> None:
    """Two genuinely different setups (far apart in time) count as two unique."""
    canon = canonical_candidate()

    def evaluator(session: SessionRef, candidate: CandidateConfig) -> list[AcceptedSetup]:
        base = 1_752_537_751_000_000_000
        return [
            _setup(candidate.config_hash, canonical=True, ts=base),
            _setup(candidate.config_hash, canonical=True, ts=base + 10 * 60 * 1_000_000_000),  # +10 min
        ]

    result = run_auto_research([SessionRef("session_A", Path("x"))], [canon], evaluator=evaluator)
    assert result.raw_candidate_trades == 2
    assert result.unique_underlying_setups == 2
    assert result.duplicate_overlap == 0


def test_zero_valid_setups_is_a_valid_result() -> None:
    """An evaluator that accepts nothing yields an honest zero, not an error."""
    result = run_auto_research(
        [SessionRef("session_A", Path("x"))],
        [canonical_candidate()],
        evaluator=lambda s, c: [],
    )
    assert result.raw_candidate_trades == 0
    assert result.unique_underlying_setups == 0
    assert "valid, honest result" in result.zero_is_valid_note


def test_run_is_deterministic_for_identical_inputs() -> None:
    """Identical sessions/candidates/evaluator -> identical aggregate result."""
    candidates = [canonical_candidate(), CandidateConfig(decision_stride=20, label="exp")]
    sessions = [SessionRef("session_A", Path("x")), SessionRef("session_B", Path("y"))]

    def evaluator(session: SessionRef, candidate: CandidateConfig) -> list[AcceptedSetup]:
        return [_setup(candidate.config_hash, canonical=candidate.is_canonical, ts=1_752_537_751_000_000_000)]

    first = run_auto_research(sessions, candidates, evaluator=evaluator)
    second = run_auto_research(sessions, candidates, evaluator=evaluator)
    assert first == second


def test_throttle_yields_to_data_capture() -> None:
    """Receiver pressure reduces workers; new drops collapse research to the minimum."""
    healthy = ReceiverHealth(queue_occupancy=0.2, current_session_drops_delta=0, lag_ms=10)
    assert throttle_worker_count(8, healthy) == 8
    busy = ReceiverHealth(queue_occupancy=0.75, current_session_drops_delta=0, lag_ms=10)
    assert throttle_worker_count(8, busy) == 4
    dropping = ReceiverHealth(queue_occupancy=0.3, current_session_drops_delta=5, lag_ms=10)
    assert throttle_worker_count(8, dropping) == 1
    saturated = ReceiverHealth(queue_occupancy=0.95, current_session_drops_delta=0, lag_ms=10)
    assert throttle_worker_count(8, saturated) == 1


def test_resolve_worker_count_respects_mode_and_cores() -> None:
    """High-performance uses all cores; default leaves one free; explicit clamps."""
    hw = HardwareProfile(cpu_cores=8, total_memory_gb=32.0, gpu_name=None, gpu_available=False)
    assert resolve_worker_count(hw, ResearchRuntimeConfig()) == 7
    assert resolve_worker_count(hw, ResearchRuntimeConfig(high_performance=True)) == 8
    assert resolve_worker_count(hw, ResearchRuntimeConfig(worker_count=100)) == 8
    assert resolve_worker_count(hw, ResearchRuntimeConfig(worker_count=2)) == 2


def test_detect_hardware_reports_at_least_one_core() -> None:
    """Detection never crashes and always reports a usable core count."""
    hw = detect_hardware()
    assert hw.cpu_cores >= 1
    assert isinstance(hw.gpu_available, bool)


def test_scheduler_skips_completed_pairs_and_resumes() -> None:
    """Checkpointed pairs are not re-queued; only new (session, config) work remains."""
    sessions = [SessionRef("session_A", Path("x")), SessionRef("session_B", Path("y"))]
    candidates = [canonical_candidate(), CandidateConfig(decision_stride=20, label="exp")]
    checkpoint = ResearchCheckpoint()

    def signature(_session: SessionRef) -> str:
        return "sig-v1"

    pending = discover_pending(sessions, candidates, checkpoint, signature)
    assert len(pending) == 4  # 2 sessions x 2 candidates

    # Complete one pair; it must not come back.
    session, candidate = pending[0]
    checkpoint.mark(research_pair_key(session.session_id, candidate.config_hash, "sig-v1"))
    remaining = discover_pending(sessions, candidates, checkpoint, signature)
    assert len(remaining) == 3
    assert (session, candidate) not in remaining


def test_changed_source_signature_reopens_work() -> None:
    """A new source signature yields a new key, so the pair is scheduled again."""
    sessions = [SessionRef("session_A", Path("x"))]
    candidates = [canonical_candidate()]
    checkpoint = ResearchCheckpoint()
    checkpoint.mark(research_pair_key("session_A", candidates[0].config_hash, "sig-v1"))
    still_pending = discover_pending(sessions, candidates, checkpoint, lambda s: "sig-v2")
    assert len(still_pending) == 1


def test_checkpoint_round_trips(tmp_path: Path) -> None:
    """A saved checkpoint reloads with the same completed keys."""
    checkpoint = ResearchCheckpoint()
    checkpoint.mark("abc")
    checkpoint.mark("def")
    path = tmp_path / "checkpoint.json"
    checkpoint.save(path)
    reloaded = ResearchCheckpoint.load(path)
    assert reloaded.completed == {"abc", "def"}


def test_no_execution_or_broker_module_is_reachable() -> None:
    """The research engine must not import any execution/broker/tradovate module."""
    source = Path("app/research/auto_research.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    banned = ("execution", "tradovate", "broker", "live_execution")
    assert not any(any(b in name.lower() for b in banned) for name in imported), imported


def _finalized_session(raw_root: Path) -> SessionRef:
    rec = MarketSessionRecorder(root_dir=raw_root, session_start_utc=datetime(2026, 7, 15, 0, 22, tzinfo=UTC))
    rec.record_control_event({"type": "delayed_mode", "timestamp_ns": 1, "delay_minutes": 15})
    base = 1_752_537_751_000_000_000
    price = Decimal("29500.00")
    for i in range(200):
        price += Decimal("0.25") if i % 2 == 0 else Decimal("-0.25")
        rec.record({"type": "depth_update", "timestamp": base + i * 1_000_000, "symbol": "MNQ",
                    "side": "bid" if i % 2 else "ask", "price": f"{price:.2f}",
                    "previous_size": "0", "new_size": str(i % 40 + 1)})
        rec.record({"timestamp_ns": base + i * 1_000_000 + 1, "price": f"{price:.2f}", "size": "1",
                    "aggressor_side": "buy" if i % 2 else "sell", "instrument": "MNQ", "sequence_id": i + 1})
    rec.finalize(clean_shutdown=True)
    return SessionRef(rec.session_dir.name, rec.session_dir)


def test_real_evaluator_runs_and_reports_honest_zero(tmp_path: Path) -> None:
    """The default evaluator runs the real builder and honestly returns zero setups."""
    session = _finalized_session(tmp_path / "raw")
    setups = default_evaluator(session, canonical_candidate())
    assert setups == []  # strategy accepts nothing here - a valid result
    result = run_auto_research([session], [canonical_candidate()])
    assert result.raw_candidate_trades == 0
