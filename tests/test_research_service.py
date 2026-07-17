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


def _fake_job_result(state_dir: Path, *, key: str, mtime_offset: float = 0.0) -> None:
    """Write a persisted job result with ONE canonical winning setup."""
    canonical = canonical_candidate()
    out_dir = state_dir / "job_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2, "key": key, "session_id": "session_R",
        "config_hash": canonical.config_hash, "is_canonical": True, "worker_pid": 1234,
        "error": "", "strategy_version": canonical.strategy_version,
        "setups": [{"direction": "long", "trading_day": "2026-07-15",
                    "decision_ts_ns": 1_752_537_751_000_000_000, "defended_price": "29450.00",
                    "net_pnl_per_contract": "38.00", "r_multiple": "2.0", "outcome": "target_first"}],
    }
    path = out_dir / f"{key}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    if mtime_offset:
        stat = path.stat()
        os.utime(path, (stat.st_atime, stat.st_mtime + mtime_offset))


def test_restart_restores_results_and_empty_cycle_never_zeroes_totals(tmp_path: Path) -> None:
    """Defects fixed: results restore after restart; empty cycles keep totals."""
    _fake_job_result(tmp_path / "state", key="k1")
    service = _service(tmp_path)  # a brand-new instance == app restart
    service.run_batch(use_processes=False)  # no raw sessions -> zero NEW jobs
    status = service.status()
    assert status.raw_candidate_trades == 1, "persisted results must be restored"
    assert status.canonical_trades == 1
    # A second empty cycle must not zero the totals.
    service.run_batch(use_processes=False)
    assert service.status().raw_candidate_trades == 1


def test_superseded_results_never_double_count(tmp_path: Path) -> None:
    """Two persisted files for the same (session, candidate) count once."""
    _fake_job_result(tmp_path / "state", key="old")
    _fake_job_result(tmp_path / "state", key="new", mtime_offset=100.0)
    service = _service(tmp_path)
    results = service.load_persisted_results()
    assert len(results) == 1 and results[0].key == "new"


def test_stale_claim_is_recovered_and_successful_claims_cleaned(tmp_path: Path) -> None:
    """A crashed worker's claim is reaped; completed claims do not accumulate."""
    registry = ClaimRegistry(tmp_path / "claims", stale_after_seconds=0.0)
    assert registry.try_claim("crashed-job")
    assert registry.try_claim("done-job")
    recovered = registry.recover_stale(completed_keys={"done-job"})
    assert recovered == 1  # crashed-job reaped
    assert registry.try_claim("crashed-job"), "reaped job must be claimable again"
    assert not (tmp_path / "claims" / "done-job.claim").exists(), "successful claim cleaned up"


def test_full_run_leaves_no_claim_files_and_writes_ledgers(tmp_path: Path) -> None:
    """After a successful batch: zero leftover claims; ledgers persisted."""
    _session(tmp_path / "raw", 10)
    service = _service(tmp_path, runtime_config=ResearchRuntimeConfig(worker_count=1))
    service.run_batch(use_processes=False)
    assert list((tmp_path / "state" / "claims").glob("*.claim")) == []
    ledgers = tmp_path / "state" / "ledgers"
    assert (ledgers / "canonical.json").exists(), "authoritative canonical ledger must persist"
    canonical = json.loads((ledgers / "canonical.json").read_text(encoding="utf-8"))
    assert canonical["is_canonical"] is True
    assert canonical["starting_balance"] == "100000"
    assert "daily locks" in canonical["economics"]


def test_failed_job_records_retry_count_and_is_retryable(tmp_path: Path, monkeypatch) -> None:
    """A failing job persists its error + retry count and stays claimable."""
    _session(tmp_path / "raw", 10)
    import app.research.research_service as svc_module

    def _boom(job):
        return svc_module.JobResult(job.key, job.session_id, job.candidate.config_hash,
                                    job.candidate.is_canonical, (), os.getpid(),
                                    error="RuntimeError: simulated crash")

    monkeypatch.setattr(svc_module, "run_research_job", _boom)
    service = _service(tmp_path, runtime_config=ResearchRuntimeConfig(worker_count=1))
    service.run_batch(use_processes=False)
    errors = json.loads((tmp_path / "state" / "job_errors.json").read_text(encoding="utf-8"))
    assert len(errors) == 1
    entry = next(iter(errors.values()))
    assert entry["retries"] == 1 and "simulated crash" in entry["last_error"]
    # Claim was released -> the same job is discovered again next cycle.
    from app.research.auto_research import ResearchCheckpoint

    remaining = service.discover_jobs(ResearchCheckpoint.load(service.checkpoint_path))
    assert len(remaining) == 1


def test_mid_batch_data_loss_cancels_remaining_jobs(tmp_path: Path) -> None:
    """New drops DURING a batch cancel not-yet-run jobs and release their claims."""
    for minute in (10, 20, 30):
        _session(tmp_path / "raw", minute)
    calls = {"count": 0}

    def health() -> ReceiverHealth:
        # Healthy for discovery + first job, then drops appear.
        calls["count"] += 1
        return ReceiverHealth(current_session_drops_delta=0 if calls["count"] <= 2 else 2)

    service = ResearchService(tmp_path / "raw", tmp_path / "processed", state_dir=tmp_path / "state",
                              candidates=[canonical_candidate()],
                              runtime_config=ResearchRuntimeConfig(worker_count=1),
                              health_provider=health)
    service.run_batch(use_processes=False)
    from app.research.auto_research import ResearchCheckpoint

    checkpoint = ResearchCheckpoint.load(service.checkpoint_path)
    remaining = service.discover_jobs(checkpoint)
    assert 1 <= len(remaining) <= 2, "cancelled jobs must remain pending, not lost or duplicated"
    assert len(checkpoint.completed) + len(remaining) == 3


def test_production_health_provider_reports_real_drop_deltas() -> None:
    """The launcher's provider turns controller health into throttle signals."""
    from app.runtime.controller import AutomaticRuntimeController
    from tools.start_assistant import make_receiver_health_provider

    controller = AutomaticRuntimeController.from_config("config/session_profiles.yaml")
    provider = make_receiver_health_provider(controller)
    controller.handle_control_event({"type": "heartbeat", "timestamp_ns": 1, "dropped_message_count": 86211})
    controller.handle_control_event({"type": "connected", "timestamp_ns": 2, "dropped_message_count": 86211})
    assert provider().current_session_drops_delta == 0  # stale lifetime count is not current loss
    controller.handle_control_event({"type": "data_gap", "timestamp_ns": 3,
                                     "dropped_message_count": 86214, "reason": "overflow"})
    assert provider().current_session_drops_delta == 3  # real new loss reaches research throttling
    assert provider().current_session_drops_delta == 0  # delta, not cumulative


def test_throttled_and_paused_cycles_do_zero_disk_io(tmp_path: Path, monkeypatch) -> None:
    """Capture-priority: a throttled/paused cycle must not touch the disk at all.

    Discovery reads every session manifest and hashes parquet sources. Doing that
    on every poll while recording starved the receiver's event loop (shared GIL)
    and the Bookmap bridge queue overflowed by ~1M events. When research must
    yield, it has to yield completely - no catalog build, no hashing, no ledger
    rewrite.
    """
    _session(tmp_path / "raw", 10)
    import app.research.research_service as svc_module

    calls = {"catalog": 0}
    real_build = svc_module.ResearchService.discover_jobs

    def counting_discover(self, checkpoint):
        calls["catalog"] += 1
        return real_build(self, checkpoint)

    monkeypatch.setattr(svc_module.ResearchService, "discover_jobs", counting_discover)

    dropping = lambda: ReceiverHealth(current_session_drops_delta=5)  # noqa: E731
    service = ResearchService(
        tmp_path / "raw", tmp_path / "processed", state_dir=tmp_path / "state",
        candidates=[canonical_candidate()], runtime_config=ResearchRuntimeConfig(worker_count=4),
        health_provider=dropping,
    )
    service.run_batch(use_processes=False)
    assert service.status().state == STATE_THROTTLED
    assert calls["catalog"] == 0, "a throttled cycle must not build the catalog"

    service.request_pause()
    service.run_batch(use_processes=False)
    assert calls["catalog"] == 0, "a paused cycle must not build the catalog"


def test_parquet_signature_is_hashed_once_not_every_poll(tmp_path: Path, monkeypatch) -> None:
    """Finalized sources are hashed once and cached; polling must not re-hash MBs."""
    _session(tmp_path / "raw", 10)
    import app.research.build_orchestrator as orch

    calls = {"n": 0}
    real_signature = orch.build_signature

    def counting_signature(session_dir):
        calls["n"] += 1
        return real_signature(session_dir)

    monkeypatch.setattr(orch, "build_signature", counting_signature)
    service = _service(tmp_path, runtime_config=ResearchRuntimeConfig(worker_count=1))
    from app.research.auto_research import ResearchCheckpoint

    empty = ResearchCheckpoint()
    service.discover_jobs(empty)
    service.discover_jobs(empty)
    service.discover_jobs(empty)
    assert calls["n"] == 1, "a finalized session's parquets must be hashed once, then cached"


def test_idle_cycle_keeps_totals_without_rereading_disk(tmp_path: Path) -> None:
    """An idle/throttled cycle returns cached totals - never a zeroed result."""
    _fake_job_result(tmp_path / "state", key="k1")
    service = _service(tmp_path)
    first = service.run_batch(use_processes=False)
    assert first.raw_candidate_trades == 1

    # Now make the service throttle; totals must persist from cache, no I/O.
    service._health_provider = lambda: ReceiverHealth(current_session_drops_delta=1)
    cached = service.run_batch(use_processes=False)
    assert service.status().state == STATE_THROTTLED
    assert cached.raw_candidate_trades == 1, "throttling must not zero visible totals"


def test_detect_hardware_is_cached() -> None:
    """Hardware probing (PATH scan + import attempts) must not repeat on GUI timers."""
    from app.research.auto_research import detect_hardware

    first = detect_hardware()
    second = detect_hardware()
    assert first is second  # lru_cache: the expensive probe ran once


def test_delayed_feed_is_not_falsely_marked_stale_by_wall_clock() -> None:
    """A 15-min delayed feed must not read as 'stale' and pin research at 0 workers.

    Bookmap's delayed events carry timestamps ~15 minutes behind wall clock, so a
    naive wall-clock staleness check reported lag forever and research never ran.
    """
    from app.runtime.controller import AutomaticRuntimeController
    from tools.start_assistant import make_receiver_health_provider

    controller = AutomaticRuntimeController.from_config("config/session_profiles.yaml")
    controller.handle_control_event({"type": "delayed_mode", "timestamp_ns": 1, "delay_minutes": 15})
    controller.handle_control_event({"type": "connected", "timestamp_ns": 2, "dropped_message_count": 0})
    provider = make_receiver_health_provider(controller)
    # No fresh event timestamps at all, yet a delayed feed must not report lag.
    assert provider().lag_ms == 0
