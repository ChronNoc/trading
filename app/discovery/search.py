"""Parallel parameter search with a ranking coordinator - Part 3.

Workers each evaluate one deduplicated parameter set with a deterministic
per-worker seed and a full decision trace. The coordinator ranks by the
composite score (never raw win rate), applies every Part 2 gate, logs
every accepted AND rejected candidate with reasons, and ends at the
validation gate: a recommendation report and a hard stop. Nothing in this
module can arm LIVE mode or reach a broker.
"""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.discovery.backtest import CostModel, ParameterSet, run_backtest
from app.discovery.episodes import SetupEpisode
from app.discovery.gates import (
    FeatureSpec,
    GateResult,
    leakage_gate,
    monte_carlo_gate,
    regime_breakdown,
    regime_robustness_gate,
    sample_size_gate,
    sensitivity_gate,
)
from app.discovery.metrics import CompositeScore, composite_score
from app.discovery.splits import HoldoutVault, walk_forward_windows

DECISION_TIME_FEATURES = (
    FeatureSpec("reload_count", "decision_time"),
    FeatureSpec("aggressive_volume", "decision_time"),
    FeatureSpec("ask_pull_ratio", "decision_time"),
)


@dataclass(frozen=True, slots=True)
class SearchLimits:
    """Resource caps - item 29. No unbounded background compute."""

    max_workers: int = 4
    max_seconds_per_worker: float = 30.0
    max_total_seconds: float = 300.0
    min_seconds_between_runs: float = 3600.0


@dataclass(frozen=True, slots=True)
class CandidateResult:
    """One evaluated parameter set with its full verdict."""

    parameters: ParameterSet
    seed: int
    validation_score: CompositeScore
    worst_case_score: CompositeScore
    regime_expectancy: dict[str, Decimal]
    gates: tuple[GateResult, ...]
    accepted: bool
    rejection_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SearchRunResult:
    """Everything one search run produced, ending at the validation gate."""

    candidates: tuple[CandidateResult, ...]
    leader: CandidateResult | None
    holdout_score: CompositeScore | None
    beat_previous_best: bool | None
    recommendation_path: Path | None
    candidates_log_path: Path


def worker_seed(base_seed: int, parameters: ParameterSet) -> int:
    """Deterministic per-worker seed - item 31."""
    digest = hashlib.sha256(f"{base_seed}:{parameters.key()}".encode("utf-8")).hexdigest()
    return int(digest[:12], 16)


def deduplicate(parameter_sets: tuple[ParameterSet, ...]) -> tuple[ParameterSet, ...]:
    """Drop near-identical parameter sets before spending compute - item 30."""
    seen: dict[str, ParameterSet] = {}
    for parameters in parameter_sets:
        seen.setdefault(parameters.key(), parameters)
    return tuple(seen.values())


def default_parameter_grid() -> tuple[ParameterSet, ...]:
    """A small honest grid over the setup's thresholds."""
    grid: list[ParameterSet] = []
    for reload_count in (1, 2):
        for volume in (Decimal("300"), Decimal("450")):
            for pull in (Decimal("0.40"), Decimal("0.55")):
                for target in (Decimal("1.5"), Decimal("2.0")):
                    grid.append(
                        ParameterSet(
                            min_reload_count=reload_count,
                            min_aggressive_volume=volume,
                            min_ask_pull_ratio=pull,
                            target_r=target,
                        ),
                    )
    return tuple(grid)


class SearchRateLimiter:
    """Persisted rate limit on full search re-runs - item 33."""

    def __init__(self, state_path: Path, *, min_seconds_between_runs: float) -> None:
        """Create a limiter persisting its last-run time at ``state_path``."""
        self._state_path = state_path
        self._min_interval = min_seconds_between_runs

    def check_and_mark(self, *, now: float | None = None) -> None:
        """Raise if a run happened too recently; otherwise record this run."""
        moment = time.time() if now is None else now
        if self._state_path.is_file():
            last = float(self._state_path.read_text(encoding="utf-8").strip() or 0)
            elapsed = moment - last
            if elapsed < self._min_interval:
                raise RuntimeError(
                    f"search ran {elapsed:.0f}s ago; minimum interval is {self._min_interval:.0f}s - "
                    "continuous re-searching overfits to short-term noise",
                )
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(str(moment), encoding="utf-8")


def evaluate_candidate(
    parameters: ParameterSet,
    windows: tuple,
    *,
    base_seed: int,
    costs: CostModel,
    minimum_trades: int,
    trace_dir: Path | None = None,
) -> CandidateResult:
    """Walk-forward evaluate one parameter set and run every gate."""
    seed = worker_seed(base_seed, parameters)
    validation_trades = []
    for window in windows:
        validation_trades.extend(run_backtest(parameters, window.validate, costs=costs))
    validation_trades = tuple(validation_trades)
    worst_case_trades = tuple(
        trade
        for window in windows
        for trade in run_backtest(parameters, window.validate, costs=costs, worst_case=True)
    )
    score = composite_score(validation_trades)
    worst_score = composite_score(worst_case_trades)

    gates: list[GateResult] = [leakage_gate(DECISION_TIME_FEATURES)]
    gates.append(sample_size_gate(validation_trades, minimum_trades=minimum_trades))
    gates.append(regime_robustness_gate(validation_trades))
    monte_carlo, _ = monte_carlo_gate(validation_trades, seed=seed)
    gates.append(monte_carlo)
    gates.append(
        sensitivity_gate(
            parameters,
            lambda candidate: tuple(
                trade
                for window in windows
                for trade in run_backtest(candidate, window.validate, costs=costs)
            ),
        ),
    )
    if worst_score.expectancy_r <= 0:
        gates.append(
            GateResult("worst_case_costs", False, f"expectancy {worst_score.expectancy_r}R under worst-case costs"),
        )
    else:
        gates.append(
            GateResult("worst_case_costs", True, f"expectancy {worst_score.expectancy_r}R survives worst-case costs"),
        )

    rejections = tuple(f"{gate.gate}: {gate.reason}" for gate in gates if not gate.passed)
    result = CandidateResult(
        parameters=parameters,
        seed=seed,
        validation_score=score,
        worst_case_score=worst_score,
        regime_expectancy=regime_breakdown(validation_trades),
        gates=tuple(gates),
        accepted=not rejections,
        rejection_reasons=rejections,
    )
    if trace_dir is not None:
        _write_worker_trace(trace_dir, result)
    return result


def run_search(
    episodes: tuple[SetupEpisode, ...],
    holdout: HoldoutVault,
    *,
    base_seed: int,
    output_root: Path,
    grid: tuple[ParameterSet, ...] | None = None,
    limits: SearchLimits | None = None,
    costs: CostModel | None = None,
    minimum_trades: int = 100,
    train_days: int = 20,
    validate_days: int = 10,
    previous_best_holdout_composite: Decimal | None = None,
    rate_limiter: SearchRateLimiter | None = None,
) -> SearchRunResult:
    """Run the full parallel search and stop at the validation gate."""
    if rate_limiter is not None:
        rate_limiter.check_and_mark()
    caps = limits or SearchLimits()
    cost_model = costs or CostModel()
    windows = walk_forward_windows(episodes, train_days=train_days, validate_days=validate_days)
    parameter_sets = deduplicate(grid or default_parameter_grid())
    trace_dir = output_root / "worker_traces"
    started = time.monotonic()

    results: list[CandidateResult] = []
    with ThreadPoolExecutor(max_workers=caps.max_workers) as executor:
        futures = {
            executor.submit(
                evaluate_candidate,
                parameters,
                windows,
                base_seed=base_seed,
                costs=cost_model,
                minimum_trades=minimum_trades,
                trace_dir=trace_dir,
            ): parameters
            for parameters in parameter_sets
        }
        for future in as_completed(futures, timeout=caps.max_total_seconds):
            results.append(future.result(timeout=caps.max_seconds_per_worker))
            if time.monotonic() - started > caps.max_total_seconds:
                break

    ranked = sorted(results, key=lambda item: item.validation_score.composite, reverse=True)
    candidates_log = _write_candidates_log(output_root, ranked)

    accepted = [candidate for candidate in ranked if candidate.accepted]
    if not accepted:
        return SearchRunResult(
            candidates=tuple(ranked),
            leader=None,
            holdout_score=None,
            beat_previous_best=None,
            recommendation_path=None,
            candidates_log_path=candidates_log,
        )

    leader = accepted[0]
    holdout_trades = run_backtest(leader.parameters, holdout.consume(), costs=cost_model)
    holdout_score = composite_score(holdout_trades)
    beat_previous = (
        None
        if previous_best_holdout_composite is None
        else holdout_score.composite > previous_best_holdout_composite
    )
    recommendation = None
    if beat_previous is not False:
        recommendation = _write_recommendation(output_root, leader, holdout_score, beat_previous)
    return SearchRunResult(
        candidates=tuple(ranked),
        leader=leader,
        holdout_score=holdout_score,
        beat_previous_best=beat_previous,
        recommendation_path=recommendation,
        candidates_log_path=candidates_log,
    )


def save_versioned_artifact(
    artifacts_root: Path,
    *,
    candidate: CandidateResult,
    training_window: str,
    feature_names: tuple[str, ...],
) -> Path:
    """Version a candidate artifact with metadata - never overwrite (item 22)."""
    artifacts_root.mkdir(parents=True, exist_ok=True)
    existing = sorted(int(path.name[1:]) for path in artifacts_root.glob("v*") if path.name[1:].isdigit())
    version = (existing[-1] + 1) if existing else 1
    version_dir = artifacts_root / f"v{version:03d}"
    if version_dir.exists():
        raise FileExistsError(f"artifact version directory already exists: {version_dir}")
    version_dir.mkdir()
    feature_hash = hashlib.sha256("|".join(sorted(feature_names)).encode("utf-8")).hexdigest()
    metadata = {
        "version": version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "training_window": training_window,
        "feature_set_hash": feature_hash,
        "feature_names": list(feature_names),
        "parameters": candidate.parameters.key(),
        "metrics": {line.split(": ")[0]: line.split(": ")[1] for line in candidate.validation_score.component_lines()},
        "synthetic_data": True,
    }
    (version_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return version_dir


def explainability_report(importances: dict[str, Decimal]) -> str:
    """Render a feature-importance report - item 21, no black boxes."""
    lines = ["# Feature importance", ""]
    total = sum(importances.values(), Decimal("0"))
    for name, value in sorted(importances.items(), key=lambda item: item[1], reverse=True):
        share = (value / total * 100).quantize(Decimal("0.1")) if total > 0 else Decimal("0")
        lines.append(f"- {name}: {value} ({share}%)")
    lines.append("")
    lines.append("Every listed feature is computed strictly from decision-time data.")
    return "\n".join(lines)


def _write_worker_trace(trace_dir: Path, result: CandidateResult) -> None:
    trace_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "parameters": result.parameters.key(),
        "seed": result.seed,
        "validation": {line.split(": ")[0]: line.split(": ")[1] for line in result.validation_score.component_lines()},
        "worst_case_expectancy_r": str(result.worst_case_score.expectancy_r),
        "regime_expectancy": {tag: str(value) for tag, value in result.regime_expectancy.items()},
        "gates": [{"gate": gate.gate, "passed": gate.passed, "reason": gate.reason} for gate in result.gates],
        "accepted": result.accepted,
    }
    path = trace_dir / f"worker_{result.seed}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_candidates_log(output_root: Path, ranked: list[CandidateResult]) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    path = output_root / "candidates.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for candidate in ranked:
            handle.write(
                json.dumps(
                    {
                        "parameters": candidate.parameters.key(),
                        "accepted": candidate.accepted,
                        "rejection_reasons": list(candidate.rejection_reasons),
                        "composite": str(candidate.validation_score.composite),
                        "win_rate": str(candidate.validation_score.win_rate),
                        "profit_factor": str(candidate.validation_score.profit_factor),
                        "max_drawdown_r": str(candidate.validation_score.max_drawdown_r),
                        "sortino": str(candidate.validation_score.sortino),
                        "expectancy_r": str(candidate.validation_score.expectancy_r),
                        "trade_count": candidate.validation_score.trade_count,
                        "regime_expectancy": {tag: str(v) for tag, v in candidate.regime_expectancy.items()},
                    },
                )
                + "\n",
            )
    return path


def _write_recommendation(
    output_root: Path,
    leader: CandidateResult,
    holdout_score: CompositeScore,
    beat_previous: bool | None,
) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Strategy candidate recommendation",
        "",
        "**THE SYSTEM STOPS HERE.** This report is the pipeline's final output.",
        "No config flag was changed. No broker connection was opened. Going live",
        "is a separate, manual, human decision - see AGENTS.md and Part 7.",
        "",
        f"## Candidate: {leader.parameters.key()}",
        "",
        "### Validation (walk-forward) metrics",
        *(f"- {line}" for line in leader.validation_score.component_lines()),
        "",
        "### Holdout metrics (evaluated exactly once)",
        *(f"- {line}" for line in holdout_score.component_lines()),
        "",
        "### Worst-case cost pass",
        f"- expectancy (R): {leader.worst_case_score.expectancy_r}",
        "",
        "### Per-regime expectancy",
        *(f"- {tag}: {value}R" for tag, value in leader.regime_expectancy.items()),
        "",
        "### Gates",
        *(f"- [{'pass' if gate.passed else 'FAIL'}] {gate.gate}: {gate.reason}" for gate in leader.gates),
        "",
    ]
    if beat_previous is True:
        lines.append("Shadow comparison: beat the previous best on the same holdout.")
    elif beat_previous is None:
        lines.append("Shadow comparison: no previous best exists; this is the first leader.")
    lines.append("")
    lines.append("Data source: SYNTHETIC episodes - not valid for live decisions until real Stage C data replaces them.")
    path = output_root / "recommendation.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
