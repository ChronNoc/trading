"""The evaluation simulator: deterministic, and it shows the properties the governor is for."""

from __future__ import annotations

import random
from decimal import Decimal as D

import pytest

from app.risk.account_profile import lucid_flex_25k
from app.risk.lucid_fit import FitScenario, estimate, simulate_governed, simulate_unprotected
from app.risk.lucid_governor import GovernorPolicy

PROFILE = lucid_flex_25k()
RUNS = 25


def test_the_win_rate_is_the_one_that_gives_exactly_the_requested_expectancy() -> None:
    for edge in ("-0.2", "0", "0.1", "0.3"):
        scenario = FitScenario(edge_r=D(edge))
        probability = D(str(scenario.win_probability))
        payoff = scenario.reward_per_contract / scenario.risk_per_contract
        assert abs(probability * payoff - (1 - probability) - D(edge)) < D("0.0001")


def test_an_edge_the_stop_and_target_cannot_produce_is_refused() -> None:
    with pytest.raises(ValueError, match="not reachable"):
        FitScenario(edge_r=D("5")).win_probability  # noqa: B018 - evaluating the property is the test


def test_the_same_seed_gives_the_same_answer() -> None:
    scenario = FitScenario(edge_r=D("0.1"))
    first = simulate_governed(PROFILE, GovernorPolicy(), scenario, random.Random(11))
    second = simulate_governed(PROFILE, GovernorPolicy(), scenario, random.Random(11))
    assert first == second
    assert estimate(PROFILE, scenario, runs=8, seed=3) == estimate(PROFILE, scenario, runs=8, seed=3)


def test_a_governed_account_never_fails_whatever_the_edge() -> None:
    for edge in ("-0.3", "-0.15", "0", "0.2"):
        summary = estimate(PROFILE, FitScenario(edge_r=D(edge)), runs=RUNS)
        assert summary.failed == 0, f"edge {edge}R"


def test_a_losing_strategy_is_retired_by_the_governor_but_busted_by_the_old_rules() -> None:
    scenario = FitScenario(edge_r=D("-0.15"))
    governed = estimate(PROFILE, scenario, runs=RUNS)
    old = estimate(PROFILE, scenario, runs=RUNS, governed=False)
    assert governed.retired == RUNS and governed.passed == 0
    assert old.rate(old.failed) > 0.9


def test_no_governed_account_is_left_active_but_unable_to_trade() -> None:
    for edge in ("-0.15", "-0.05", "0.05"):
        assert estimate(PROFILE, FitScenario(edge_r=D(edge)), runs=RUNS).stalled == 0


def test_a_real_edge_passes_more_often_under_the_governor_than_under_the_old_rules() -> None:
    scenario = FitScenario(edge_r=D("0.2"))
    governed = estimate(PROFILE, scenario, runs=RUNS)
    old = estimate(PROFILE, scenario, runs=RUNS, governed=False)
    assert governed.rate(governed.passed) > old.rate(old.passed)
    assert governed.rate(governed.passed) >= 0.9


def test_the_consistency_cap_means_a_pass_always_takes_at_least_four_days() -> None:
    summary = estimate(PROFILE, FitScenario(edge_r=D("0.35")), runs=RUNS)
    assert summary.passed == RUNS and summary.median_days_to_pass is not None
    assert summary.median_days_to_pass >= 4     # $1,250 at no more than $375 a day


def test_a_better_edge_never_passes_less_often() -> None:
    rates = [estimate(PROFILE, FitScenario(edge_r=D(edge)), runs=RUNS).passed for edge in ("-0.05", "0.05", "0.2")]
    assert rates == sorted(rates)


def test_the_unprotected_baseline_is_the_old_fixed_four_contracts() -> None:
    outcome = simulate_unprotected(PROFILE, FitScenario(edge_r=D("-0.15")), random.Random(5))
    assert outcome.status == "failed" and outcome.days <= 20
