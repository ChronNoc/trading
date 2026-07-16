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

import json
import os
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Callable, Sequence

STATE_SCHEMA_VERSION = 2
# A claim older than this without a matching checkpoint entry is considered
# stale (its worker crashed) and is recovered so the job can run again.
STALE_CLAIM_SECONDS = 15 * 60
# A job that keeps failing is retried at most this many times, then parked as
# permanently failed (visible in job_errors.json) instead of looping forever.
MAX_JOB_RETRIES = 5

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

    def __init__(self, claims_dir: Path, *, stale_after_seconds: float = STALE_CLAIM_SECONDS) -> None:
        """Create a registry rooted at ``claims_dir``."""
        self._dir = claims_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._stale_after = stale_after_seconds

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

    def heartbeat(self, key: str) -> None:
        """Refresh a claim's lease so a long-running job is not reaped as stale."""
        path = self._dir / f"{key}.claim"
        try:
            path.write_text(f"{os.getpid()}:{time.time()}\n", encoding="utf-8")
        except OSError:
            pass

    def release(self, key: str) -> None:
        """Release a claim (best-effort) so a failed job can be retried later."""
        path = self._dir / f"{key}.claim"
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def recover_stale(self, completed_keys: "set[str]") -> int:
        """Release claims whose worker crashed before checkpointing.

        A claim is stale when its lease timestamp is older than the lease window
        and its key never reached the checkpoint. Completed claims are also
        cleaned up here so the directory does not grow without bound.
        """
        recovered = 0
        for path in self._dir.glob("*.claim"):
            key = path.stem
            if key in completed_keys:
                path.unlink(missing_ok=True)  # cleanup after success
                continue
            try:
                stamp = float(path.read_text(encoding="utf-8").strip().split(":")[1])
            except (OSError, IndexError, ValueError):
                stamp = 0.0
            if time.time() - stamp > self._stale_after:
                path.unlink(missing_ok=True)
                recovered += 1
        return recovered


def _migrate_job_payload(data: dict) -> dict:
    """Migrate persisted job results from older schema versions to the current one.

    v1 (or unversioned) files lack ``schema_version`` and ``strategy_version``;
    they are upgraded in place with safe defaults so old evidence keeps loading
    after upgrades instead of being discarded.
    """
    version = int(data.get("schema_version", 1))
    if version < 2:
        data.setdefault("strategy_version", "")
        data["schema_version"] = STATE_SCHEMA_VERSION
    return data


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
        """Return uncompleted (session, candidate) jobs; never include active sessions.

        Jobs that exhausted their retry budget stay parked (visible in
        job_errors.json) so a poisoned session cannot loop forever.
        """
        from app.research.session_catalog import build_catalog

        exhausted = self._exhausted_retry_keys()
        catalog = build_catalog(self.raw_root)
        jobs: list[ResearchJob] = []
        for entry in sorted(catalog, key=lambda e: e.session_id):
            if entry.active or not entry.finalized or not entry.eligible_for_order_flow_replay:
                continue
            session = SessionRef(entry.session_id, entry.manifest_path.parent, entry.provenance)
            signature = self._signature_fn(session)
            for candidate in sorted(self.candidates, key=lambda c: (not c.is_canonical, c.config_hash)):
                job = ResearchJob(entry.session_id, str(session.session_dir), entry.provenance, candidate, signature)
                if not checkpoint.is_done(job.key) and job.key not in exhausted:
                    jobs.append(job)
        return jobs

    def _load_checkpoint(self) -> ResearchCheckpoint:
        """Load the checkpoint, quarantining (not deleting) a corrupt file."""
        path = self.checkpoint_path
        if path.is_file():
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._quarantine(path, "checkpoint unreadable")
        return ResearchCheckpoint.load(path)

    def _exhausted_retry_keys(self) -> set[str]:
        path = self.state_dir / "job_errors.json"
        if not path.is_file():
            return set()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self._quarantine(path, "job_errors unreadable")
            return set()
        if not isinstance(data, dict):
            return set()
        return {key for key, entry in data.items()
                if isinstance(entry, dict) and int(entry.get("retries", 0)) >= MAX_JOB_RETRIES}

    def _quarantine(self, path: Path, reason: str) -> None:
        """Move a corrupt state file aside (never delete) and continue safely."""
        quarantine_dir = self.state_dir / "quarantine"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        target = quarantine_dir / f"{path.name}.{int(time.time())}"
        try:
            path.replace(target)
            self._update(last_error=f"quarantined corrupt state file {path.name}: {reason}")
        except OSError:  # pragma: no cover - fs race
            pass

    # -- one batch -------------------------------------------------------------

    def run_batch(self, *, use_processes: bool = True, executor: ProcessPoolExecutor | None = None) -> ResearchResult:
        """Run all pending jobs once and return the aggregate integrity result.

        Honours pause and receiver-priority throttling (re-checked WHILE the batch
        runs, not only before it: any new drop cancels not-yet-started jobs and
        releases their claims). Claims each job atomically, checkpoints
        completions, cleans up successful claims, recovers stale claims from
        crashed workers, and always aggregates over ALL persisted results so
        visible totals never reset to zero on an empty cycle.
        """
        checkpoint = self._load_checkpoint()
        recovered = self.claims.recover_stale(checkpoint.completed)
        if recovered:
            self._update(last_error=f"recovered {recovered} stale claim(s) from a crashed worker")
        jobs = self.discover_jobs(checkpoint)
        requested = resolve_worker_count(self.hardware, self.runtime_config)
        workers = self._apply_throttle(requested)
        self._update(
            state=STATE_RUNNING, requested_workers=requested, active_workers=workers,
            queued_jobs=len(jobs), running_jobs=0, throttle_reason=self._throttle_reason(requested, workers),
        )
        if self._pause_event.is_set():
            self._update(state=STATE_PAUSED)
            return self._aggregate_persisted(checkpoint)
        if workers <= 0:
            self._update(state=STATE_THROTTLED)
            return self._aggregate_persisted(checkpoint)

        claimed = [job for job in jobs if self.claims.try_claim(job.key)]
        if not claimed:
            aggregate = self._aggregate_persisted(checkpoint)
            self._update(state=STATE_IDLE, queued_jobs=0, last_checkpoint_keys=len(checkpoint.completed))
            return aggregate

        results: list[JobResult] = []
        cancelled: list[ResearchJob] = []
        if use_processes and workers > 1:
            owns = executor is None
            pool = executor or ProcessPoolExecutor(max_workers=workers)
            try:
                futures = {pool.submit(run_research_job, job): job for job in claimed}
                pending = set(futures)
                while pending:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        results.append(future.result())
                        self.claims.heartbeat(futures[future].key)
                    # Mid-batch receiver check: new event loss cancels remaining work.
                    if self._apply_throttle(workers) <= 0:
                        for future in pending:
                            if future.cancel():
                                cancelled.append(futures[future])
                        pending = {f for f in pending if not f.cancelled()}
                        self._update(state=STATE_THROTTLED,
                                     throttle_reason="Batch interrupted: receiver reported new event loss.")
            finally:
                if owns:
                    pool.shutdown(wait=True, cancel_futures=True)
        else:
            for job in claimed:
                if self._apply_throttle(workers) <= 0 or self._pause_event.is_set():
                    cancelled.append(job)
                    continue
                results.append(run_research_job(job))

        for job in cancelled:
            self.claims.release(job.key)  # cancelled work retries next cycle

        completed = 0
        failed = 0
        for result in results:
            if result.error:
                failed += 1
                self._record_retry(result.key, result.error)
                self.claims.release(result.key)  # allow retry
                continue
            checkpoint.mark(result.key)
            completed += 1
        checkpoint.save(self.checkpoint_path)
        self.claims.recover_stale(checkpoint.completed)  # clean up successful claims
        self._persist_result_setups(results)

        aggregate = self._aggregate_persisted(checkpoint)
        self._update(
            state=STATE_IDLE, running_jobs=0,
            completed_jobs=self._status.completed_jobs + completed,
            failed_jobs=self._status.failed_jobs + failed,
            last_checkpoint_keys=len(checkpoint.completed),
        )
        return aggregate

    def _record_retry(self, key: str, error: str) -> None:
        """Persist error and retry count for a failed job (visible, not silent)."""
        path = self.state_dir / "job_errors.json"
        data: dict[str, dict[str, object]] = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        entry = data.get(key, {"retries": 0})
        entry["retries"] = int(entry.get("retries", 0)) + 1
        entry["last_error"] = error
        data[key] = entry
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def load_persisted_results(self) -> list[JobResult]:
        """Restore every previously persisted job result (crash/restart safe).

        One (session, candidate) pair contributes exactly ONE result: when a
        session was re-run under a new source signature, only the newest file
        counts, so superseded results can never inflate the evidence.
        """
        latest: dict[tuple[str, str], tuple[float, str, JobResult]] = {}
        result_dir = self.state_dir / "job_results"
        if not result_dir.is_dir():
            return []
        for path in sorted(result_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._quarantine(path, "job result unreadable")
                continue
            if not isinstance(data, dict) or data.get("error"):
                continue
            data = _migrate_job_payload(data)
            setups = tuple(
                AcceptedSetup(
                    session_id=str(data["session_id"]), config_hash=str(data["config_hash"]),
                    strategy_version=str(data.get("strategy_version", "")),
                    is_canonical=bool(data["is_canonical"]), direction=str(s["direction"]),
                    trading_day=str(s["trading_day"]), decision_ts_ns=int(s["decision_ts_ns"]),
                    defended_price=Decimal(str(s["defended_price"])),
                    net_pnl_per_contract=Decimal(str(s["net_pnl_per_contract"])),
                    r_multiple=Decimal(str(s["r_multiple"])), outcome=str(s["outcome"]),
                )
                for s in data.get("setups", [])
            )
            result = JobResult(str(data["key"]), str(data["session_id"]), str(data["config_hash"]),
                               bool(data["is_canonical"]), setups, int(data.get("worker_pid", 0)))
            pair = (result.session_id, result.config_hash)
            stamp = (path.stat().st_mtime, result.key)
            existing = latest.get(pair)
            if existing is None or stamp > (existing[0], existing[1]):
                latest[pair] = (stamp[0], stamp[1], result)
        return [item[2] for item in sorted(latest.values(), key=lambda i: i[2].key)]

    def _aggregate_persisted(self, checkpoint: ResearchCheckpoint) -> ResearchResult:
        """Aggregate over ALL persisted results so totals survive empty cycles."""
        return self._aggregate(self.load_persisted_results(), checkpoint)

    def _persist_result_setups(self, results: Sequence[JobResult]) -> None:
        """Persist accepted setups per job atomically for durable, resumable evidence."""
        out_dir = self.state_dir / "job_results"
        out_dir.mkdir(parents=True, exist_ok=True)
        for result in results:
            if result.error:
                continue  # errors go to job_errors.json with retry counts
            payload = {
                "schema_version": STATE_SCHEMA_VERSION,
                "key": result.key,
                "session_id": result.session_id,
                "config_hash": result.config_hash,
                "is_canonical": result.is_canonical,
                "worker_pid": result.worker_pid,
                "error": result.error,
                "strategy_version": result.setups[0].strategy_version if result.setups else "",
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

    def persist_ledgers(self, results: Sequence[JobResult]) -> None:
        """Write one canonical and one per-candidate experimental ledger to disk.

        Every ledger is derived deterministically from the persisted setups, so a
        restart reconstructs identical ledgers. Experimental P&L never merges into
        the canonical file. All money math is Decimal; balances are stored as
        strings. Per-contract economics (commission and slippage already inside
        net_pnl_per_contract).
        """
        ledger_dir = self.state_dir / "ledgers"
        ledger_dir.mkdir(parents=True, exist_ok=True)
        by_hash: dict[str, list[AcceptedSetup]] = {}
        canonical_hash: str | None = None
        for result in results:
            if result.error:
                continue
            by_hash.setdefault(result.config_hash, []).extend(result.setups)
            if result.is_canonical:
                canonical_hash = result.config_hash
        for config_hash, setups in by_hash.items():
            balance = Decimal("100000")
            rows = []
            for setup in sorted(setups, key=lambda s: (s.decision_ts_ns, s.session_id)):
                balance += setup.net_pnl_per_contract
                rows.append({
                    "session_id": setup.session_id, "trading_day": setup.trading_day,
                    "direction": setup.direction, "decision_ts_ns": setup.decision_ts_ns,
                    "net_pnl_per_contract": str(setup.net_pnl_per_contract),
                    "r_multiple": str(setup.r_multiple), "outcome": setup.outcome,
                    "balance_after": str(balance),
                })
            payload = {
                "schema_version": STATE_SCHEMA_VERSION,
                "config_hash": config_hash,
                "is_canonical": config_hash == canonical_hash,
                "economics": "per-contract candidate accounting (costs inside net_pnl_per_contract)",
                "starting_balance": "100000",
                "ending_balance": str(balance),
                "trades": rows,
            }
            name = "canonical_candidate_raw" if config_hash == canonical_hash else config_hash
            path = ledger_dir / f"{name}.json"
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            tmp.replace(path)
        self._persist_canonical_ledger(ledger_dir)

    def _persist_canonical_ledger(self, ledger_dir: Path) -> None:
        """Write the AUTHORITATIVE canonical ledger through the full risk path.

        Uses the strict quality-gated real outcomes from ``data/processed`` and
        the fixed $100k account with every daily lock (max entries, max losses,
        loss budget, one-position, no resets). Every trade carries its simulated
        ORDER/FILL records from the fill engine, so the account consumes actual
        execution records - not bare setup counts.
        """
        from app.research.paper_ledger import run_real_paper_ledger
        from app.research.real_episodes import load_completed_real_outcomes
        from app.simulator.fill_engine import FillModelConfig, MarketTrade, simulate_order

        outcomes = load_completed_real_outcomes(self.processed_root)
        result = run_real_paper_ledger(outcomes)
        trades = []
        for t in result.trades:
            # Re-simulate the entry through the fill engine so the ledger row
            # carries real order/fill records (ack -> fills -> weighted average).
            # The recorded entry price already includes builder slippage, so the
            # engine's liquidity replay must reproduce it exactly - asserted here.
            sim = simulate_order(
                quantity=t.contracts, limit_price=t.entry, is_buy=t.direction == "long",
                submitted_at_ns=t.decision_ts_ns,
                trades=[MarketTrade(timestamp_ns=t.entry_ts_ns, price=t.entry, size=t.contracts)],
                config=FillModelConfig(commission_per_contract=t.costs_per_contract),
            )
            trades.append({
                "session_id": t.session_id, "setup_id": t.setup_id, "trading_day": t.trading_day,
                "direction": t.direction, "entry": str(t.entry), "stop": str(t.stop),
                "target": str(t.target), "exit": str(t.exit), "contracts": t.contracts,
                "costs_per_contract": str(t.costs_per_contract), "r_multiple": str(t.r_multiple),
                "pnl": str(t.pnl), "balance_after": str(t.balance_after),
                "outcome": t.outcome, "input_hash": t.input_hash,
                "order_status": sim.status,
                "average_fill_price": str(sim.average_fill_price),
                "fills": [
                    {"timestamp_ns": f.timestamp_ns, "price": str(f.price), "quantity": f.quantity}
                    for f in sim.fills
                ],
            })
        payload = {
            "schema_version": STATE_SCHEMA_VERSION,
            "is_canonical": True,
            "economics": "fixed $100k account, prop-style daily locks, no resets; rows carry simulated order/fill records",
            "starting_balance": str(result.starting_balance),
            "ending_balance": str(result.ending_balance),
            "stopped": result.stopped,
            "skipped_by_account_rules": len(result.skipped),
            "trades": trades,
        }
        path = ledger_dir / "canonical.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def _aggregate(self, results: Sequence[JobResult], checkpoint: ResearchCheckpoint) -> ResearchResult:
        self.persist_ledgers(results)  # ledgers always reflect all known results
        by_pair: dict[tuple[str, str], list[AcceptedSetup]] = {}
        session_ids: set[str] = set()
        for result in results:
            by_pair.setdefault((result.session_id, result.config_hash), []).extend(result.setups)
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
        # Data capture has absolute priority: any NEW current-session drop, a
        # saturated receiver queue, or heavy lag stops research entirely (zero
        # workers) - not merely reduces it.
        if health.current_session_drops_delta > 0:
            return 0
        if health.queue_occupancy >= 0.9 or health.lag_ms >= 1000:
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
