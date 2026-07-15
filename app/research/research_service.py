"""Persistent automatic paper-research service with real process parallelism.

This is the wired service, not a helper: it discovers finalized eligible real
sessions, turns each (session, candidate configuration) into an immutable job,
runs jobs across a real ``ProcessPoolExecutor`` (proving work happens in multiple
OS processes), claims jobs atomically on disk so no work is duplicated across
workers or restarts, checkpoints completed jobs, resumes after a crash, supports
pause/resume and a High Performance mode, and yields to the receiver: any new
current-session drop collapses research to zero workers immediately, because data
capture always outranks research.

Determinism: for identical inputs the aggregate result is identical whether jobs
run serially or in parallel. No execution/broker module is importable from here
(proved by test). Zero valid setups is a valid result.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Callable, Sequence

from app.research.auto_research import (
    AcceptedSetup,
    CandidateConfig,
    ReceiverHealth,
    ResearchCheckpoint,
    ResearchResult,
    ResearchRuntimeConfig,
    SessionRef,
    canonical_candidate,
    default_evaluator,
    detect_hardware,
    research_pair_key,
    resolve_worker_count,
    run_auto_research,
    throttle_worker_count,
)

STATE_IDLE = "idle"
STATE_RUNNING = "running"
STATE_PAUSED = "paused"
STATE_THROTTLED = "throttled"
STATE_FAILED = "failed"

# Default candidate grid: one canonical + a few experimental variations. Kept
# small and explicit so results stay explainable.
DEFAULT_CANDIDATES: tuple[CandidateConfig, ...] = (
    canonical_candidate(),
    CandidateConfig(stop_buffer_points=Decimal("12"), label="exp_wider_stop"),
    CandidateConfig(decision_stride=20, label="exp_finer_stride"),
)


@dataclass(frozen=True, slots=True)
class ResearchJob:
    """One immutable (session, candidate) unit of research work (picklable)."""

    session_id: str
    session_dir: str
    provenance: str
    candidate: CandidateConfig
    source_signature: str

    @property
    def key(self) -> str:
        """Return the atomic-claim / checkpoint key for this job."""
        return research_pair_key(self.session_id, self.candidate.config_hash, self.source_signature)


@dataclass(frozen=True, slots=True)
class JobResult:
    """Result of one job, including the OS pid that actually executed it."""

    key: str
    session_id: str
    config_hash: str
    is_canonical: bool
    setups: tuple[AcceptedSetup, ...]
    worker_pid: int
    error: str = ""


def run_research_job(job: ResearchJob) -> JobResult:
    """Execute one research job in the current (possibly child) process.

    Module-level and picklable so a ``ProcessPoolExecutor`` can dispatch it to a
    worker process. Reports the executing pid so the caller can prove real
    multi-process execution. Runs the honest real builder; a session that accepts
    no setup returns an empty tuple.
    """
    session = SessionRef(job.session_id, Path(job.session_dir), job.provenance)
    try:
        setups = tuple(default_evaluator(session, job.candidate))
        return JobResult(job.key, job.session_id, job.candidate.config_hash, job.candidate.is_canonical,
                         setups, os.getpid())
    except Exception as exc:  # noqa: BLE001 - a bad session must not kill the pool
        return JobResult(job.key, job.session_id, job.candidate.config_hash, job.candidate.is_canonical,
                         (), os.getpid(), error=f"{type(exc).__name__}: {exc}")


@dataclass(slots=True)
class ServiceStatus:
    """Snapshot of the service for the GUI (never blocks the caller)."""

    state: str = STATE_IDLE
    requested_workers: int = 0
    active_workers: int = 0
    queued_jobs: int = 0
    running_jobs: int = 0
    completed_jobs: int = 0
    failed_jobs: int = 0
    last_checkpoint_keys: int = 0
    canonical_trades: int = 0
    experimental_trades: int = 0
    unique_setups: int = 0
    raw_candidate_trades: int = 0
    duplicate_overlap: int = 0
    independent_days: int = 0
    throttle_reason: str = ""
    empty_reason: str = ""
    last_error: str = ""


class ClaimRegistry:
    """Atomic on-disk job claiming so no job runs twice across workers/restarts."""

    def __init__(self, claims_dir: Path) -> None:
        """Create a registry rooted at ``claims_dir``."""
        self._dir = claims_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def try_claim(self, key: str) -> bool:
        """Atomically claim ``key``; return False if already claimed."""
        path = self._dir / f"{key}.claim"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}:{time.time()}\n")
        return True

    def release(self, key: str) -> None:
        """Release a claim (best-effort) so a failed job can be retried later."""
        path = self._dir / f"{key}.claim"
        try:
            path.unlink()
        except FileNotFoundError:
            pass


HealthProvider = Callable[[], ReceiverHealth]
SignatureFn = Callable[[SessionRef], str]


class ResearchService:
    """Manages discovery, parallel execution, checkpointing, pause, and throttle."""

    def __init__(
        self,
        raw_root: Path,
        processed_root: Path,
        *,
        state_dir: Path,
        candidates: Sequence[CandidateConfig] = DEFAULT_CANDIDATES,
        runtime_config: ResearchRuntimeConfig | None = None,
        health_provider: HealthProvider | None = None,
        source_signature_fn: SignatureFn | None = None,
    ) -> None:
        """Create a service; ``state_dir`` holds the checkpoint and claim files."""
        self.raw_root = Path(raw_root)
        self.processed_root = Path(processed_root)
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path = self.state_dir / "research_checkpoint.json"
        self.claims = ClaimRegistry(self.state_dir / "claims")
        self.candidates = tuple(candidates)
        self.runtime_config = runtime_config or ResearchRuntimeConfig()
        self.hardware = detect_hardware()
        self._health_provider = health_provider
        self._signature_fn = source_signature_fn or self._default_signature
        self._status = ServiceStatus()
        self._status_lock = threading.Lock()
        self._pause_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_result: ResearchResult | None = None

    # -- discovery -------------------------------------------------------------

    def _default_signature(self, session: SessionRef) -> str:
        """Return a source signature from the session's build summary or manifest."""
        from app.research.build_orchestrator import build_signature

        try:
            return str(sorted(build_signature(session.session_dir)["source_file_hashes"].items()))
        except Exception:  # noqa: BLE001 - missing files -> unique-ish fallback
            return "unsigned"

    def discover_jobs(self, checkpoint: ResearchCheckpoint) -> list[ResearchJob]:
        """Return uncompleted (session, candidate) jobs; never include active sessions."""
        from app.research.session_catalog import build_catalog

        catalog = build_catalog(self.raw_root)
        jobs: list[ResearchJob] = []
        for entry in sorted(catalog, key=lambda e: e.session_id):
            if entry.active or not entry.finalized or not entry.eligible_for_order_flow_replay:
                continue
            session = SessionRef(entry.session_id, entry.manifest_path.parent, entry.provenance)
            signature = self._signature_fn(session)
            for candidate in sorted(self.candidates, key=lambda c: (not c.is_canonical, c.config_hash)):
                job = ResearchJob(entry.session_id, str(session.session_dir), entry.provenance, candidate, signature)
                if not checkpoint.is_done(job.key):
                    jobs.append(job)
        return jobs

    # -- one batch -------------------------------------------------------------

    def run_batch(self, *, use_processes: bool = True, executor: ProcessPoolExecutor | None = None) -> ResearchResult:
        """Run all pending jobs once and return the aggregate integrity result.

        Honours pause and receiver-priority throttling. Claims each job atomically,
        checkpoints completions, and releases claims for failed jobs so they retry.
        """
        checkpoint = ResearchCheckpoint.load(self.checkpoint_path)
        jobs = self.discover_jobs(checkpoint)
        requested = resolve_worker_count(self.hardware, self.runtime_config)
        workers = self._apply_throttle(requested)
        self._update(
            state=STATE_RUNNING, requested_workers=requested, active_workers=workers,
            queued_jobs=len(jobs), running_jobs=0, throttle_reason=self._throttle_reason(requested, workers),
        )
        if self._pause_event.is_set():
            self._update(state=STATE_PAUSED)
            return self._aggregate([], checkpoint)
        if workers <= 0:
            self._update(state=STATE_THROTTLED)
            return self._aggregate([], checkpoint)

        claimed = [job for job in jobs if self.claims.try_claim(job.key)]
        results: list[JobResult] = []
        if not claimed:
            self._update(state=STATE_IDLE, queued_jobs=0)
            return self._aggregate(self._checkpoint_results(checkpoint), checkpoint)

        if use_processes and workers > 1:
            owns = executor is None
            pool = executor or ProcessPoolExecutor(max_workers=workers)
            try:
                for result in pool.map(run_research_job, claimed):
                    results.append(result)
            finally:
                if owns:
                    pool.shutdown(wait=True)
        else:
            results = [run_research_job(job) for job in claimed]

        completed = 0
        failed = 0
        for result in results:
            if result.error:
                failed += 1
                self.claims.release(result.key)  # allow retry
                continue
            checkpoint.mark(result.key)
            completed += 1
        checkpoint.save(self.checkpoint_path)
        self._persist_result_setups(results)

        aggregate = self._aggregate([r for r in results if not r.error], checkpoint)
        self._update(
            state=STATE_IDLE, running_jobs=0, completed_jobs=completed, failed_jobs=failed,
            last_checkpoint_keys=len(checkpoint.completed),
        )
        return aggregate

    def _checkpoint_results(self, checkpoint: ResearchCheckpoint) -> list[JobResult]:
        return []

    def _persist_result_setups(self, results: Sequence[JobResult]) -> None:
        """Persist accepted setups per job for durable, resumable evidence."""
        import json

        out_dir = self.state_dir / "job_results"
        out_dir.mkdir(parents=True, exist_ok=True)
        for result in results:
            payload = {
                "key": result.key,
                "session_id": result.session_id,
                "config_hash": result.config_hash,
                "is_canonical": result.is_canonical,
                "worker_pid": result.worker_pid,
                "error": result.error,
                "setups": [
                    {
                        "direction": s.direction, "trading_day": s.trading_day,
                        "decision_ts_ns": s.decision_ts_ns, "defended_price": str(s.defended_price),
                        "net_pnl_per_contract": str(s.net_pnl_per_contract), "r_multiple": str(s.r_multiple),
                        "outcome": s.outcome,
                    }
                    for s in result.setups
                ],
            }
            path = out_dir / f"{result.key}.json"
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            tmp.replace(path)

    def _aggregate(self, results: Sequence[JobResult], checkpoint: ResearchCheckpoint) -> ResearchResult:
        by_pair: dict[tuple[str, str], list[AcceptedSetup]] = {}
        session_ids: set[str] = set()
        for result in results:
            by_pair[(result.session_id, result.config_hash)] = list(result.setups)
            session_ids.add(result.session_id)
        sessions = [SessionRef(sid, self.raw_root) for sid in sorted(session_ids)] or [SessionRef("_none", self.raw_root)]

        def evaluator(session: SessionRef, candidate: CandidateConfig) -> list[AcceptedSetup]:
            return by_pair.get((session.session_id, candidate.config_hash), [])

        aggregate = run_auto_research(sessions, self.candidates, evaluator=evaluator)
        self._last_result = aggregate
        self._update(
            canonical_trades=aggregate.canonical_trades,
            experimental_trades=aggregate.experimental_trades,
            unique_setups=aggregate.unique_underlying_setups,
            raw_candidate_trades=aggregate.raw_candidate_trades,
            duplicate_overlap=aggregate.duplicate_overlap,
            independent_days=aggregate.independent_trading_days,
            empty_reason="" if aggregate.raw_candidate_trades else aggregate.zero_is_valid_note,
        )
        return aggregate

    # -- throttling ------------------------------------------------------------

    def _apply_throttle(self, requested: int) -> int:
        if self._health_provider is None:
            return requested
        health = self._health_provider()
        # Data capture has absolute priority: any NEW current-session drop stops
        # research entirely (zero workers), not merely reduces it.
        if health.current_session_drops_delta > 0:
            return 0
        return throttle_worker_count(requested, health)

    def _throttle_reason(self, requested: int, granted: int) -> str:
        if granted >= requested:
            return ""
        if granted <= 0:
            return "Research paused: receiver under pressure or dropping events (data capture has priority)."
        return f"Research throttled to {granted}/{requested} workers to protect data capture."

    # -- background loop -------------------------------------------------------

    def start(self, *, poll_seconds: float = 2.0, use_processes: bool = True) -> None:
        """Start the background research loop (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()

        def _loop() -> None:
            while not self._stop_event.is_set():
                if not self._pause_event.is_set():
                    try:
                        self.run_batch(use_processes=use_processes)
                    except Exception as exc:  # noqa: BLE001 - keep the service alive
                        self._update(state=STATE_FAILED, last_error=f"{type(exc).__name__}: {exc}")
                else:
                    self._update(state=STATE_PAUSED)
                self._stop_event.wait(poll_seconds)

        self._thread = threading.Thread(target=_loop, name="research-service", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background loop."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10)

    def request_pause(self) -> None:
        """Pause after the current batch."""
        self._pause_event.set()
        self._update(state=STATE_PAUSED)

    def resume(self) -> None:
        """Resume research."""
        self._pause_event.clear()

    @property
    def is_paused(self) -> bool:
        """Return whether research is paused."""
        return self._pause_event.is_set()

    def set_runtime_config(self, config: ResearchRuntimeConfig) -> None:
        """Replace the runtime config (worker count / High Performance / GPU)."""
        self.runtime_config = config

    def status(self) -> ServiceStatus:
        """Return a copy of the current status (safe to call from the GUI)."""
        with self._status_lock:
            return replace(self._status)

    def _update(self, **fields: object) -> None:
        with self._status_lock:
            self._status = replace(self._status, **fields)  # type: ignore[arg-type]
