"""Automatic high-throughput paper research engine (STAGE 6A) - integrity core.

This module evaluates the deterministic strategy across many versioned candidate
configurations over the SAME real sessions, while refusing to let parallelism
manufacture evidence. Its guarantees:

- One canonical fixed-$100k account uses only the selected, versioned strategy.
- Every experimental candidate has an immutable configuration hash and its own
  ledger; experimental results never merge into the canonical account.
- One real setup is one underlying observation even if 100 candidates evaluate
  it: raw candidate-trade count and unique underlying-setup count are reported
  separately, and duplicate overlap is made explicit.
- Data capture always outranks research: :func:`throttle_worker_count` reduces
  workers as receiver queue occupancy / drops / lag rise.
- Zero valid setups is a valid result.
- Nothing here imports or reaches any execution/broker module (proved by test).

The engine is deterministic for identical data, configuration, and candidate
set. Heavy parallelism (process pool, GPU) is layered on top of this pure core;
the core stays importable and testable without either.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Callable, Mapping, Sequence

from app.research.episode_builder import BUILDER_VERSION, STRATEGY_VERSION, EpisodeConfig, build_episodes

# Default coarse buckets for deciding whether two candidate trades are the SAME
# underlying setup: same session + direction, within one minute, within a small
# price band. Deliberately coarse so near-identical candidates cannot be counted
# as independent evidence.
DEFAULT_SETUP_BUCKET_NS = 60 * 1_000_000_000
DEFAULT_SETUP_PRICE_BUCKET = Decimal("2")


@dataclass(frozen=True, slots=True)
class CandidateConfig:
    """One versioned strategy/threshold candidate with an immutable identity."""

    strategy_version: str = STRATEGY_VERSION
    stop_buffer_points: Decimal = Decimal("10")
    fixed_slippage_ticks: Decimal = Decimal("1")
    large_block_minimum: Decimal = Decimal("90")
    absorption_volume_minimum: Decimal = Decimal("400")
    decision_stride: int = 25
    timeout_seconds: int = 900
    label: str = "canonical"
    is_canonical: bool = False

    @property
    def config_hash(self) -> str:
        """Return a stable 16-char hash over the immutable configuration."""
        payload = json.dumps(
            {
                "strategy_version": self.strategy_version,
                "stop_buffer_points": str(self.stop_buffer_points),
                "fixed_slippage_ticks": str(self.fixed_slippage_ticks),
                "large_block_minimum": str(self.large_block_minimum),
                "absorption_volume_minimum": str(self.absorption_volume_minimum),
                "decision_stride": self.decision_stride,
                "timeout_seconds": self.timeout_seconds,
                "builder_version": BUILDER_VERSION,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_episode_config(self) -> EpisodeConfig:
        """Return the EpisodeConfig this candidate replays with."""
        return EpisodeConfig(
            stop_buffer_points=self.stop_buffer_points,
            fixed_slippage_ticks=self.fixed_slippage_ticks,
            decision_stride=self.decision_stride,
            timeout_seconds=self.timeout_seconds,
            large_block_minimum=self.large_block_minimum,
            absorption_volume_minimum=self.absorption_volume_minimum,
        )


def canonical_candidate() -> CandidateConfig:
    """Return the single canonical candidate (default strategy parameters)."""
    return CandidateConfig(label="canonical", is_canonical=True)


@dataclass(frozen=True, slots=True)
class SessionRef:
    """A finalized session to evaluate (never an active recording)."""

    session_id: str
    session_dir: Path
    provenance: str = "REAL_DELAYED"


@dataclass(frozen=True, slots=True)
class AcceptedSetup:
    """One accepted, completed setup produced by a candidate on a session."""

    session_id: str
    config_hash: str
    strategy_version: str
    is_canonical: bool
    direction: str
    trading_day: str
    decision_ts_ns: int
    defended_price: Decimal
    net_pnl_per_contract: Decimal
    r_multiple: Decimal
    outcome: str

    def setup_identity(
        self,
        *,
        bucket_ns: int = DEFAULT_SETUP_BUCKET_NS,
        price_bucket: Decimal = DEFAULT_SETUP_PRICE_BUCKET,
    ) -> tuple[str, str, int, int]:
        """Return the underlying-setup identity shared across candidates.

        Two candidate trades on the same session, same direction, within the
        same time and price bucket describe the SAME market event - one
        observation, not two.
        """
        ts_bucket = self.decision_ts_ns // bucket_ns
        price_index = int((self.defended_price / price_bucket).to_integral_value())
        return (self.session_id, self.direction, ts_bucket, price_index)


@dataclass(frozen=True, slots=True)
class CandidateResult:
    """Per-candidate, kept strictly separate from every other candidate."""

    config_hash: str
    label: str
    strategy_version: str
    is_canonical: bool
    trades: int
    wins: int
    net_r: Decimal
    unique_setups: int


@dataclass(frozen=True, slots=True)
class ResearchResult:
    """Aggregate research outcome with explicit result-integrity accounting."""

    candidates: tuple[CandidateResult, ...]
    canonical_trades: int
    experimental_trades: int
    raw_candidate_trades: int
    unique_underlying_setups: int
    duplicate_overlap: int
    independent_trading_days: int
    setup_overlap: Mapping[tuple[str, str, int, int], tuple[str, ...]] = field(default_factory=dict)

    @property
    def zero_is_valid_note(self) -> str:
        """Return the honest note that zero valid setups is a valid result."""
        if self.raw_candidate_trades == 0:
            return "Zero valid setups across all candidates - a valid, honest result. No evidence was manufactured."
        return f"{self.raw_candidate_trades} raw candidate trades map to {self.unique_underlying_setups} unique setups."


# --- Hardware detection --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HardwareProfile:
    """Detected hardware, honestly reported (GPU is best-effort, never assumed)."""

    cpu_cores: int
    total_memory_gb: float | None
    gpu_name: str | None
    gpu_available: bool
    gpu_compute_library: str | None = None  # e.g. cupy/xgboost-gpu if importable


@lru_cache(maxsize=1)
def detect_hardware() -> HardwareProfile:
    """Detect CPU cores, memory, and a supported GPU without hard dependencies.

    GPU *presence* (an ``nvidia-smi`` on PATH) is reported honestly, but it is
    never treated as GPU *usage*. A GPU only counts as usable for a workload when
    a real compute library that can target it is importable.

    Cached: the PATH scan and the cupy/cudf import probes are expensive (a failed
    import rescans sys.path), and hardware does not change while the app runs.
    Calling this on a GUI timer without the cache froze the window.
    """
    cores = os.cpu_count() or 1
    memory_gb: float | None = None
    try:  # psutil is optional; absence is not an error
        import psutil

        memory_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except Exception:  # noqa: BLE001 - optional dependency / platform differences
        memory_gb = None
    gpu_name: str | None = None
    if shutil.which("nvidia-smi") is not None:
        gpu_name = "NVIDIA GPU (nvidia-smi on PATH)"
    compute_lib: str | None = None
    for module_name in ("cupy", "cudf"):
        try:
            __import__(module_name)
            compute_lib = module_name
            break
        except Exception:  # noqa: BLE001 - optional GPU stack
            continue
    return HardwareProfile(
        cpu_cores=cores,
        total_memory_gb=memory_gb,
        gpu_name=gpu_name,
        gpu_available=gpu_name is not None,
        gpu_compute_library=compute_lib,
    )


def gpu_workload_note(hardware: HardwareProfile, *, gpu_enabled: bool) -> str:
    """Return an honest statement about GPU use for the deterministic replay workload.

    Deterministic Parquet replay and rule evaluation are CPU/I-O bound, so the GPU
    is not used for that even when present. GPU acceleration is only meaningful for
    a supported ML workload (e.g. XGBoost training/inference) and only when a GPU
    compute library is actually importable.
    """
    if not hardware.gpu_available:
        return "No supported GPU detected; CPU-only research."
    if not gpu_enabled:
        return f"{hardware.gpu_name} present but GPU disabled by config; CPU-only research."
    if hardware.gpu_compute_library is None:
        return (
            f"{hardware.gpu_name} present, but no GPU compute library (cupy/cudf) is installed and "
            "deterministic replay is CPU/I/O bound; GPU not used for this workload."
        )
    return (
        f"{hardware.gpu_name} available via {hardware.gpu_compute_library}, but deterministic replay is "
        "CPU/I/O bound; GPU would only be used for a supported ML workload (e.g. XGBoost), not replay."
    )


@dataclass(frozen=True, slots=True)
class ResearchRuntimeConfig:
    """User-tunable research knobs, including a HIGH PERFORMANCE mode."""

    worker_count: int | None = None
    memory_ceiling_gb: float | None = None
    gpu_enabled: bool = False
    batch_rows: int = 8192
    high_performance: bool = False

    def __post_init__(self) -> None:
        """Validate knobs."""
        if self.worker_count is not None and self.worker_count < 1:
            raise ValueError("worker_count must be >= 1 when set")
        if self.batch_rows <= 0:
            raise ValueError("batch_rows must be positive")


def resolve_worker_count(hardware: HardwareProfile, config: ResearchRuntimeConfig) -> int:
    """Resolve the desired worker count, leaving a core free unless high-perf."""
    if config.worker_count is not None:
        return max(1, min(config.worker_count, hardware.cpu_cores))
    if config.high_performance:
        return max(1, hardware.cpu_cores)
    return max(1, hardware.cpu_cores - 1)


# --- Receiver-priority throttling ---------------------------------------------


@dataclass(frozen=True, slots=True)
class ReceiverHealth:
    """Signals that determine whether research must yield to data capture."""

    queue_occupancy: float = 0.0  # 0..1 fraction of the receiver queue in use
    current_session_drops_delta: int = 0  # new drops since last check
    lag_ms: int = 0  # receiver processing lag


def throttle_worker_count(requested: int, health: ReceiverHealth, *, min_workers: int = 1) -> int:
    """Reduce research workers as the receiver comes under pressure.

    Data capture always wins: any *new* dropped events collapse research to the
    minimum immediately; heavy queue occupancy or lag halves or minimises the
    pool. The result never exceeds ``requested``.
    """
    requested = max(min_workers, requested)
    if health.current_session_drops_delta > 0:
        return min_workers
    if health.queue_occupancy >= 0.9 or health.lag_ms >= 1000:
        return min_workers
    if health.queue_occupancy >= 0.7 or health.lag_ms >= 500:
        return max(min_workers, requested // 2)
    return requested


# --- The evaluator and runner --------------------------------------------------

Evaluator = Callable[[SessionRef, CandidateConfig], Sequence[AcceptedSetup]]


def default_evaluator(session: SessionRef, candidate: CandidateConfig) -> list[AcceptedSetup]:
    """Evaluate one candidate on one session with the real episode builder.

    Honest by construction: it returns only ledger-eligible completed episodes,
    so a session that accepts no setup yields an empty list (the current real
    state). Never fabricates trades to fill the result.
    """
    result = build_episodes(
        session.session_dir,
        session_id=session.session_id,
        provenance=session.provenance,
        config=candidate.to_episode_config(),
    )
    setups: list[AcceptedSetup] = []
    for episode in result.episodes:
        if not episode.eligible_for_ledger:
            continue
        assert episode.net_pnl_per_contract is not None and episode.r_multiple is not None
        setups.append(
            AcceptedSetup(
                session_id=episode.session_id,
                config_hash=candidate.config_hash,
                strategy_version=candidate.strategy_version,
                is_canonical=candidate.is_canonical,
                direction=episode.direction,
                trading_day=episode.trading_day,
                decision_ts_ns=episode.decision_ts_ns,
                defended_price=episode.defended_price,
                net_pnl_per_contract=episode.net_pnl_per_contract,
                r_multiple=episode.r_multiple,
                outcome=episode.outcome,
            ),
        )
    return setups


def run_auto_research(
    sessions: Sequence[SessionRef],
    candidates: Sequence[CandidateConfig],
    *,
    evaluator: Evaluator = default_evaluator,
    setup_bucket_ns: int = DEFAULT_SETUP_BUCKET_NS,
    setup_price_bucket: Decimal = DEFAULT_SETUP_PRICE_BUCKET,
) -> ResearchResult:
    """Evaluate every candidate over every session and aggregate with integrity.

    Deterministic: sessions and candidates are processed in a stable order and
    the result depends only on the inputs. Canonical and experimental trades are
    kept separate; unique underlying setups are counted once regardless of how
    many candidates traded them.
    """
    ordered_sessions = sorted(sessions, key=lambda s: s.session_id)
    ordered_candidates = sorted(candidates, key=lambda c: (not c.is_canonical, c.config_hash))

    per_candidate: dict[str, list[AcceptedSetup]] = defaultdict(list)
    all_setups: list[AcceptedSetup] = []
    for session in ordered_sessions:
        for candidate in ordered_candidates:
            for setup in evaluator(session, candidate):
                per_candidate[candidate.config_hash].append(setup)
                all_setups.append(setup)

    candidate_results: list[CandidateResult] = []
    for candidate in ordered_candidates:
        setups = per_candidate.get(candidate.config_hash, [])
        identities = {s.setup_identity(bucket_ns=setup_bucket_ns, price_bucket=setup_price_bucket) for s in setups}
        candidate_results.append(
            CandidateResult(
                config_hash=candidate.config_hash,
                label=candidate.label,
                strategy_version=candidate.strategy_version,
                is_canonical=candidate.is_canonical,
                trades=len(setups),
                wins=sum(1 for s in setups if s.net_pnl_per_contract > 0),
                net_r=sum((s.r_multiple for s in setups), Decimal("0")),
                unique_setups=len(identities),
            ),
        )

    canonical_trades = sum(cr.trades for cr in candidate_results if cr.is_canonical)
    experimental_trades = sum(cr.trades for cr in candidate_results if not cr.is_canonical)
    overlap: dict[tuple[str, str, int, int], set[str]] = defaultdict(set)
    for setup in all_setups:
        overlap[setup.setup_identity(bucket_ns=setup_bucket_ns, price_bucket=setup_price_bucket)].add(setup.config_hash)
    unique_setups = len(overlap)
    independent_days = len({s.trading_day for s in all_setups})
    setup_overlap = {key: tuple(sorted(hashes)) for key, hashes in overlap.items() if len(hashes) > 1}

    return ResearchResult(
        candidates=tuple(candidate_results),
        canonical_trades=canonical_trades,
        experimental_trades=experimental_trades,
        raw_candidate_trades=len(all_setups),
        unique_underlying_setups=unique_setups,
        duplicate_overlap=len(all_setups) - unique_setups,
        independent_trading_days=independent_days,
        setup_overlap=setup_overlap,
    )


# --- Automatic replay scheduler with checkpointing ----------------------------


def research_pair_key(session_id: str, config_hash: str, source_signature: str) -> str:
    """Return the idempotency key for a (session, candidate, source) triple."""
    raw = f"{session_id}|{config_hash}|{source_signature}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@dataclass(slots=True)
class ResearchCheckpoint:
    """Persisted set of completed (session, candidate, source) keys.

    Lets the scheduler resume after a crash and skip unchanged session/config
    pairs. A changed source signature (new hash/version) produces a new key, so
    re-running only happens when something that affects the result changed.
    """

    completed: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, path: Path) -> "ResearchCheckpoint":
        """Load a checkpoint, tolerating a missing or corrupt file."""
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            keys = data.get("completed", []) if isinstance(data, dict) else []
            return cls(completed={str(k) for k in keys})
        except (json.JSONDecodeError, OSError):
            return cls()

    def is_done(self, key: str) -> bool:
        """Return whether this key has already been processed."""
        return key in self.completed

    def mark(self, key: str) -> None:
        """Record a key as processed."""
        self.completed.add(key)

    def save(self, path: Path) -> None:
        """Atomically persist the checkpoint."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"completed": sorted(self.completed)}, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)


def discover_pending(
    sessions: Sequence[SessionRef],
    candidates: Sequence[CandidateConfig],
    checkpoint: ResearchCheckpoint,
    source_signature_fn: Callable[[SessionRef], str],
) -> list[tuple[SessionRef, CandidateConfig]]:
    """Return (session, candidate) pairs not yet processed for the current source.

    Preserves a stable order and never yields a pair whose key is already in the
    checkpoint, so parallel workers can each claim distinct pairs and a resumed
    run does not duplicate trades.
    """
    pending: list[tuple[SessionRef, CandidateConfig]] = []
    for session in sorted(sessions, key=lambda s: s.session_id):
        signature = source_signature_fn(session)
        for candidate in sorted(candidates, key=lambda c: (not c.is_canonical, c.config_hash)):
            key = research_pair_key(session.session_id, candidate.config_hash, signature)
            if not checkpoint.is_done(key):
                pending.append((session, candidate))
    return pending
