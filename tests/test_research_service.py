"""Tests for the persistent automatic research service (real process parallelism)."""

from __future__ import annotations

import ast
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.database.recorder import MarketSessionRecorder
from app.research.auto_research import ReceiverHealth, ResearchRuntimeConfig, canonical_candidate
from app.research.research_service import (
    STATE_PAUSED,
    STATE_THROTTLED,
    ClaimRegistry,
    ResearchService,
    run_research_job,
)


def _session(raw_root: Path, minute: int) -> None:
    rec = MarketSessionRecorder(root_dir=raw_root, session_start_utc=datetime(2026, 7, 15, 0, minute, tzinfo=UTC))
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


def _service(tmp_path: Path, **kwargs: object) -> ResearchService:
    return ResearchService(
        tmp_path / "raw", tmp_path / "processed", state_dir=tmp_path / "state",
        candidates=[canonical_candidate()], **kwargs,
    )


def test_jobs_actually_execute_in_multiple_worker_processes(tmp_path: Path) -> None:
    """Real proof: research jobs run in child processes, not the parent."""
    for minute in (10, 20, 30, 40):
        _session(tmp_path / "raw", minute)
    service = _service(tmp_path, runtime_config=ResearchRuntimeConfig(worker_count=4))
    service.run_batch(use_processes=True)

    result_dir = tmp_path / "state" / "job_results"
    pids = []
    for path in result_dir.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        pids.append(data["worker_pid"])
    assert len(pids) == 4, "all four (session, candidate) jobs must complete"
    assert all(pid != os.getpid() for pid in pids), "jobs must run out-of-process"
    assert len(set(pids)) >= 2, "work must be spread across multiple worker processes"


def test_serial_and_parallel_results_are_identical(tmp_path: Path) -> None:
    """Determinism: serial and parallel runs produce the same aggregate."""
    for minute in (10, 20, 30):
        _session(tmp_path / "raw", minute)
    serial = _service(tmp_path / "a", runtime_config=ResearchRuntimeConfig(worker_count=1))
    # Both services see the same raw data; point them at the same raw root.
    parallel = ResearchService(tmp_path / "raw", tmp_path / "p_processed", state_dir=tmp_path / "p_state",
                               candidates=[canonical_candidate()], runtime_config=ResearchRuntimeConfig(worker_count=3))
    serial_svc = ResearchService(tmp_path / "raw", tmp_path / "s_processed", state_dir=tmp_path / "s_state",
                                 candidates=[canonical_candidate()], runtime_config=ResearchRuntimeConfig(worker_count=1))
    r_serial = serial_svc.run_batch(use_processes=False)
    r_parallel = parallel.run_batch(use_processes=True)
    assert r_serial.raw_candidate_trades == r_parallel.raw_candidate_trades
    assert r_serial.unique_underlying_setups == r_parallel.unique_underlying_setups
    assert r_serial.canonical_trades == r_parallel.canonical_trades


def test_checkpoint_prevents_duplicate_work_and_resumes_after_restart(tmp_path: Path) -> None:
    """Completed jobs are checkpointed; a fresh service instance skips them."""
    for minute in (10, 20):
        _session(tmp_path / "raw", minute)
    service = _service(tmp_path, runtime_config=ResearchRuntimeConfig(worker_count=2))
    checkpoint_before = service.checkpoint_path
    service.run_batch(use_processes=False)
    assert checkpoint_before.exists()

    # Simulate an app restart: a brand-new service over the same state dir.
    restarted = _service(tmp_path, runtime_config=ResearchRuntimeConfig(worker_count=2))
    from app.research.auto_research import ResearchCheckpoint

    remaining = restarted.discover_jobs(ResearchCheckpoint.load(restarted.checkpoint_path))
    assert remaining == [], "a resumed service must not re-run completed jobs"


def test_atomic_claim_is_exclusive(tmp_path: Path) -> None:
    """A job key can be claimed once; a second claim fails (no duplicate work)."""
    registry = ClaimRegistry(tmp_path / "claims")
    assert registry.try_claim("job-1") is True
    assert registry.try_claim("job-1") is False
    registry.release("job-1")
    assert registry.try_claim("job-1") is True


def test_pause_stops_new_work(tmp_path: Path) -> None:
    """Paused service claims no new jobs and reports the paused state."""
    _session(tmp_path / "raw", 10)
    service = _service(tmp_path, runtime_config=ResearchRuntimeConfig(worker_count=2))
    service.request_pause()
    service.run_batch(use_processes=False)
    assert service.status().state == STATE_PAUSED
    # Nothing was checkpointed while paused.
    assert not service.checkpoint_path.exists() or json.loads(service.checkpoint_path.read_text())["completed"] == []


def test_new_drops_collapse_research_to_zero_workers(tmp_path: Path) -> None:
    """Any new current-session drop stops research immediately (capture priority)."""
    _session(tmp_path / "raw", 10)
    unhealthy = lambda: ReceiverHealth(queue_occupancy=0.1, current_session_drops_delta=3, lag_ms=0)  # noqa: E731
    service = ResearchService(
        tmp_path / "raw", tmp_path / "processed", state_dir=tmp_path / "state",
        candidates=[canonical_candidate()], runtime_config=ResearchRuntimeConfig(worker_count=4),
        health_provider=unhealthy,
    )
    service.run_batch(use_processes=False)
    assert service.status().state == STATE_THROTTLED
    # No job ran, so no checkpoint was written.
    assert not service.checkpoint_path.exists() or json.loads(service.checkpoint_path.read_text())["completed"] == []


def test_run_research_job_is_out_of_process_safe_and_honest_zero(tmp_path: Path) -> None:
    """A single job runs the real builder and honestly returns zero setups here."""
    _session(tmp_path / "raw", 10)
    from app.research.research_service import ResearchJob
    from app.research.session_catalog import build_catalog

    entry = next(e for e in build_catalog(tmp_path / "raw") if e.eligible_for_order_flow_replay)
    job = ResearchJob(entry.session_id, str(entry.manifest_path.parent), entry.provenance, canonical_candidate(), "sig")
    result = run_research_job(job)
    assert result.error == ""
    assert result.setups == ()  # honest zero
    assert result.worker_pid == os.getpid()  # in-process call runs here


def test_service_imports_no_execution_module() -> None:
    """Structural proof: the service reaches no execution/broker/tradovate module."""
    tree = ast.parse(Path("app/research/research_service.py").read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    banned = ("execution", "tradovate", "broker", "live_execution")
    assert not any(any(b in n.lower() for b in banned) for n in names), names
