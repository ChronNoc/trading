"""Tests for the strategy-discovery pipeline: metrics, splits, gates, search."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.discovery.backtest import CostModel, ParameterSet, run_backtest
from app.discovery.decay import DecayMonitor
from app.discovery.episodes import generate_synthetic_episodes
from app.discovery.gates import (
    FeatureSpec,
    leakage_gate,
    monte_carlo_gate,
    regime_robustness_gate,
    sample_size_gate,
    sensitivity_gate,
)
from app.discovery.metrics import (
    TradeResult,
    composite_score,
    expectancy_r,
    max_drawdown_r,
    profit_factor,
    sortino,
    win_rate,
)
from app.discovery.search import (
    SearchLimits,
    SearchRateLimiter,
    deduplicate,
    explainability_report,
    run_search,
    save_versioned_artifact,
    worker_seed,
)
from app.discovery.splits import (
    HoldoutAlreadyConsumedError,
    split_holdout,
    walk_forward_windows,
)
from app.discovery.supervisor import ModeSupervisor


def _trade(r: str, *, session: str = "new_york_open", regime: str = "trending/high_vol", day: str = "2026-01-05") -> TradeResult:
    return TradeResult(r_multiple=Decimal(r), session=session, regime_tag=regime, trade_date=day)


BALANCED_TRADES = tuple(
    _trade(r, session=session, regime=regime)
    for r in ("1.5", "-1", "2", "-1", "1.5", "-1")
    for session, regime in (("new_york_open", "trending/high_vol"), ("london", "ranging/low_vol"))
)


def test_metrics_report_every_component() -> None:
    """Win rate, profit factor, drawdown, Sortino, and expectancy all compute."""
    r_values = [Decimal("1"), Decimal("-1"), Decimal("2"), Decimal("-1")]

    assert win_rate(r_values) == Decimal("0.5000")
    assert profit_factor(r_values) == Decimal("1.5000")
    assert max_drawdown_r(r_values) == Decimal("1.0000")
    assert sortino(r_values) > 0
    assert expectancy_r(r_values) == Decimal("0.2500")

    score = composite_score([_trade("1"), _trade("-1"), _trade("2"), _trade("-1")])
    lines = score.component_lines()
    assert len(lines) == 7
    assert any("win rate" in line for line in lines)
    assert any("composite" in line for line in lines)


def test_metrics_handle_empty_and_no_loss_sequences() -> None:
    """Degenerate inputs return safe values instead of dividing by zero."""
    assert win_rate([]) == Decimal("0")
    assert profit_factor([Decimal("1")]) == Decimal("99.9999")
    assert sortino([]) == Decimal("0")


def test_holdout_vault_allows_exactly_one_read() -> None:
    """The holdout set is consumable once; a second read raises."""
    episodes = generate_synthetic_episodes(seed=1, start_date=date(2026, 1, 5), trading_days=10)
    _, vault = split_holdout(episodes)

    first = vault.consume()
    assert len(first) > 0
    with pytest.raises(HoldoutAlreadyConsumedError):
        vault.consume()


def test_walk_forward_windows_roll_forward_by_date() -> None:
    """Windows advance through time and never overlap train with validate."""
    episodes = generate_synthetic_episodes(seed=2, start_date=date(2026, 1, 5), trading_days=40)

    windows = walk_forward_windows(episodes, train_days=10, validate_days=5)

    assert len(windows) >= 2
    for window in windows:
        train_max = max(episode.episode_date for episode in window.train)
        validate_min = min(episode.episode_date for episode in window.validate)
        assert train_max < validate_min


def test_sample_size_gate_rejects_small_samples() -> None:
    """Twelve good trades are noise, not a strategy."""
    trades = tuple(_trade("2") for _ in range(12))

    result = sample_size_gate(trades, minimum_trades=100)

    assert result.passed is False
    assert "noise" in result.reason
    assert sample_size_gate(tuple(_trade("1") for _ in range(100))).passed is True


def test_regime_robustness_gate_rejects_narrow_performance() -> None:
    """Profit confined to one regime bucket fails the gate."""
    narrow = tuple(_trade("2", regime="trending/high_vol") for _ in range(20)) + tuple(
        _trade("-1", regime="ranging/low_vol", session="london") for _ in range(20)
    )

    result = regime_robustness_gate(narrow)

    assert result.passed is False
    assert "regime bucket" in result.reason
    assert regime_robustness_gate(BALANCED_TRADES).passed is True


def test_monte_carlo_gate_estimates_ruin_deterministically() -> None:
    """The same seed produces the same drawdown distribution and verdict."""
    trades = tuple(_trade(r) for r in ("1", "-1", "2", "-1", "1.5", "-1") * 20)

    first_gate, first_result = monte_carlo_gate(trades, seed=42)
    second_gate, second_result = monte_carlo_gate(trades, seed=42)

    assert first_result == second_result
    assert first_gate.passed is second_gate.passed
    ruinous = tuple(_trade("-1") for _ in range(50))
    ruin_gate, ruin_result = monte_carlo_gate(ruinous, seed=42)
    assert ruin_gate.passed is False
    assert ruin_result.risk_of_ruin == Decimal("1.0000")


def test_leakage_gate_rejects_post_outcome_features() -> None:
    """Features computed after the outcome are named and rejected."""
    leaky = (
        FeatureSpec("reload_count", "decision_time"),
        FeatureSpec("exit_price", "after_outcome"),
    )

    result = leakage_gate(leaky)

    assert result.passed is False
    assert "exit_price" in result.reason
    assert leakage_gate((FeatureSpec("reload_count", "decision_time"),)).passed is True


def test_sensitivity_gate_rejects_fragile_parameters() -> None:
    """A parameter set whose neighbors collapse is flagged as overfit."""
    base = ParameterSet(
        min_reload_count=1,
        min_aggressive_volume=Decimal("300"),
        min_ask_pull_ratio=Decimal("0.40"),
        target_r=Decimal("2"),
    )

    def fragile_evaluate(parameters: ParameterSet):
        if parameters == base:
            return tuple(_trade("2") for _ in range(50))
        return tuple(_trade("-1") for _ in range(50))

    def stable_evaluate(parameters: ParameterSet):
        return tuple(_trade("1") for _ in range(50))

    assert sensitivity_gate(base, fragile_evaluate).passed is False
    assert sensitivity_gate(base, stable_evaluate).passed is True


def test_backtest_applies_costs_and_worst_case() -> None:
    """Every simulated trade pays costs; the worst-case pass pays more."""
    episodes = generate_synthetic_episodes(seed=3, start_date=date(2026, 1, 5), trading_days=30)
    parameters = ParameterSet(
        min_reload_count=1,
        min_aggressive_volume=Decimal("300"),
        min_ask_pull_ratio=Decimal("0.40"),
        target_r=Decimal("2"),
    )

    normal = run_backtest(parameters, episodes)
    worst = run_backtest(parameters, episodes, worst_case=True)

    assert len(normal) == len(worst) > 0
    assert expectancy_r([t.r_multiple for t in worst]) < expectancy_r([t.r_multiple for t in normal])
    cost = CostModel()
    assert cost.round_trip_cost(worst_case=True) == cost.round_trip_cost(worst_case=False) * cost.worst_case_multiplier


def test_dedupe_and_deterministic_worker_seeds() -> None:
    """Near-identical parameter sets collapse; seeds are reproducible."""
    parameters = ParameterSet(
        min_reload_count=1,
        min_aggressive_volume=Decimal("300"),
        min_ask_pull_ratio=Decimal("0.40"),
        target_r=Decimal("2"),
    )
    duplicate = ParameterSet(
        min_reload_count=1,
        min_aggressive_volume=Decimal("300.0"),
        min_ask_pull_ratio=Decimal("0.400"),
        target_r=Decimal("2.0"),
    )

    assert len(deduplicate((parameters, duplicate))) == 1
    assert worker_seed(7, parameters) == worker_seed(7, duplicate)
    assert worker_seed(7, parameters) != worker_seed(8, parameters)


def test_full_search_ranks_logs_and_stops_at_the_gate(tmp_path: Path) -> None:
    """The search logs every candidate with reasons and never arms LIVE."""
    episodes = generate_synthetic_episodes(seed=11, start_date=date(2026, 1, 5), trading_days=120)
    search_episodes, holdout = split_holdout(episodes)
    from app.discovery.search import default_parameter_grid

    doomed = ParameterSet(
        min_reload_count=3,
        min_aggressive_volume=Decimal("880"),
        min_ask_pull_ratio=Decimal("0.90"),
        target_r=Decimal("2"),
    )

    result = run_search(
        search_episodes,
        holdout,
        base_seed=11,
        output_root=tmp_path,
        grid=default_parameter_grid() + (doomed,),
        limits=SearchLimits(max_workers=2),
        minimum_trades=50,
    )

    assert len(result.candidates) > 1
    log_lines = result.candidates_log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(log_lines) == len(result.candidates)
    parsed = [json.loads(line) for line in log_lines]
    assert all("rejection_reasons" in entry for entry in parsed)
    assert any(not entry["accepted"] for entry in parsed)
    composites = [Decimal(entry["composite"]) for entry in parsed]
    assert composites == sorted(composites, reverse=True)

    traces = list((tmp_path / "worker_traces").glob("worker_*.json"))
    assert len(traces) == len(result.candidates)

    if result.leader is not None:
        assert result.holdout_score is not None
        assert holdout.consumed is True
        report = result.recommendation_path.read_text(encoding="utf-8")
        assert "THE SYSTEM STOPS HERE" in report
        assert "SYNTHETIC" in report
        assert "live_mode" not in report.lower().replace(" ", "_")


def test_search_rate_limiter_blocks_rapid_reruns(tmp_path: Path) -> None:
    """A second run inside the interval is refused."""
    limiter = SearchRateLimiter(tmp_path / "last_run", min_seconds_between_runs=3600)

    limiter.check_and_mark(now=1000.0)
    with pytest.raises(RuntimeError, match="minimum interval"):
        limiter.check_and_mark(now=1500.0)
    limiter.check_and_mark(now=1000.0 + 3600.0)


def test_versioned_artifacts_never_overwrite(tmp_path: Path) -> None:
    """Artifacts get sequential versions with metadata; no silent overwrite."""
    episodes = generate_synthetic_episodes(seed=5, start_date=date(2026, 1, 5), trading_days=60)
    search_episodes, holdout = split_holdout(episodes)
    result = run_search(
        search_episodes,
        holdout,
        base_seed=5,
        output_root=tmp_path / "run",
        limits=SearchLimits(max_workers=2),
        minimum_trades=10,
    )
    candidate = result.candidates[0]

    first = save_versioned_artifact(
        tmp_path / "models",
        candidate=candidate,
        training_window="2026-01-05..2026-03-05",
        feature_names=("reload_count", "aggressive_volume", "ask_pull_ratio"),
    )
    second = save_versioned_artifact(
        tmp_path / "models",
        candidate=candidate,
        training_window="2026-01-05..2026-03-05",
        feature_names=("reload_count", "aggressive_volume", "ask_pull_ratio"),
    )

    assert first.name == "v001"
    assert second.name == "v002"
    metadata = json.loads((first / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["feature_set_hash"]
    assert metadata["training_window"] == "2026-01-05..2026-03-05"
    assert metadata["synthetic_data"] is True


def test_shadow_comparison_blocks_non_improving_leader(tmp_path: Path) -> None:
    """A leader that does not beat the previous best gets no recommendation."""
    episodes = generate_synthetic_episodes(seed=13, start_date=date(2026, 1, 5), trading_days=120)
    search_episodes, holdout = split_holdout(episodes)

    result = run_search(
        search_episodes,
        holdout,
        base_seed=13,
        output_root=tmp_path,
        limits=SearchLimits(max_workers=2),
        minimum_trades=50,
        previous_best_holdout_composite=Decimal("9999"),
    )

    if result.leader is not None:
        assert result.beat_previous_best is False
        assert result.recommendation_path is None


def test_decay_monitor_flags_drift_after_warmup() -> None:
    """Sustained underperformance versus baseline raises the drift flag."""
    monitor = DecayMonitor(baseline_expectancy_r=Decimal("0.5"), window=30, min_observations=10)

    report = None
    for _ in range(30):
        report = monitor.observe(Decimal("-0.5"))
    assert report is not None
    assert report.drifted is True
    assert "retirement or retraining" in report.reason

    healthy = DecayMonitor(baseline_expectancy_r=Decimal("0.5"), window=30, min_observations=10)
    for _ in range(30):
        report = healthy.observe(Decimal("0.5"))
    assert report.drifted is False


def test_supervisor_reads_mode_and_never_writes(tmp_path: Path) -> None:
    """Missing config means OBSERVE; live_mode true is only read, never set."""
    missing = ModeSupervisor(tmp_path / "production_config.yaml")
    assert missing.view().mode == "OBSERVE"
    assert missing.view().live_armed is False

    config = tmp_path / "production_config.yaml"
    config.write_text("live_mode: false\n", encoding="utf-8")
    assert ModeSupervisor(config).view().mode == "OBSERVE"

    config.write_text("live_mode: true\n", encoding="utf-8")
    view = ModeSupervisor(config).view()
    assert view.mode == "LIVE"
    import inspect

    from app.discovery import supervisor as supervisor_module

    source = inspect.getsource(supervisor_module)
    assert "write_text" not in source
    assert "yaml.dump" not in source


def test_explainability_report_lists_feature_shares() -> None:
    """The report names every feature with its importance share."""
    report = explainability_report(
        {"reload_count": Decimal("0.6"), "aggressive_volume": Decimal("0.3"), "ask_pull_ratio": Decimal("0.1")},
    )

    assert "reload_count: 0.6 (60.0%)" in report
    assert "decision-time data" in report


def test_discovery_never_imports_live_execution() -> None:
    """No discovery module may reference the live execution path."""
    discovery_dir = Path(__file__).resolve().parents[1] / "app" / "discovery"
    for module in discovery_dir.glob("*.py"):
        text = module.read_text(encoding="utf-8")
        assert "live_execution" not in text, module.name
        assert "tradovate" not in text.lower(), module.name
